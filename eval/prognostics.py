"""Survival prognostics: P(failure within 24, 72 and 168 h) with intervals (T13, T14; PRG-01 to PRG-06, EVL-06, G2).

    python eval/prognostics.py --processed data/processed_v4              # leave-one-event-out report
    python eval/prognostics.py --processed data/processed_v4 --save-bundle eval/frozen/metropt3_apu/survival

The planner's question is how likely a failure is inside a day, three days and
a week. This module answers it on an hourly table built from the window table
and the frozen detector's scores, and holds every model to the PRD's bar: a
Brier skill score of at least 0.15 against a constant hazard. It reports the
number whether the bar is met or not; nothing is tuned on the four events.

The hourly table has one row per campaign hour whose window (ending on the
hour) is complete. Its inputs, all looking back from the hour:

* the detector over the last 24 h: maximum score, mean score and the share of
  windows above the detector's threshold (PCA's alarm history first, as the
  detector-selection notebook's Section 9 recommends);
* from the window ending on the hour: the 6-h loaded and stopped shares and the
  hour's oil-temperature rise.

The target is the time to the next repair-forcing failure start. A row is
censored when the campaign ends or a long outage (12 h or more of recording
gap, not a logger freeze) begins before the next failure; overnight stops do
not censor. Rows between a failure's start and its repair are not at risk.

Models, each per horizon h (a row is used for h only when its outcome within
h is known):

* constant hazard (the baseline): lambda = failures / hours at risk on the
  training rows, P(h) = 1 - exp(-lambda * h);
* logistic: L2-regularised logistic regression on the standardized inputs;
* forest: a random forest classifier per horizon (the tree model the plan
  calls for; ``--model rsf`` uses scikit-survival's random survival forest
  where it is installed).

Probabilities are made monotone in the horizon (P24 <= P72 <= P168).
Evaluation is leave-one-event-out over the test blocks of the window table:
train on hourly rows outside the held-out block, test on the block. Brier
score, Brier skill and a 10-bin reliability curve per horizon, per event and
pooled (n = 4 beside every pooled figure), with 90 % intervals from
resampling held-out days. The frozen bundle carries 200 bootstrap refits over
whole days, whose 5th and 95th percentiles are every prognosis's interval
(PRG-02), and the logistic model's exact per-input contributions on the
log-odds scale (linear SHAP) for the attribution tool (PRG-06, T14).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "eval"))

from pdm_common.frozen import SurvivalModel  # noqa: E402

HORIZONS = (24, 72, 168)
INPUTS = ["score_max_24h", "score_mean_24h", "alert_share_24h", "load_share_6h", "rest_share_6h", "oil_rise_1h"]
WINDOW_INPUTS = {"load_share_6h": "load_share_6h", "rest_share_6h": "rest_share_6h", "oil_rise_1h": "oil_rise_1h"}
HOUR = pd.Timedelta(hours=1)
SEED = 0
BAR = 0.15                    # PRD G2: Brier skill against constant hazard


# --- the hourly table -----------------------------------------------------------------------

def outages_from_grid(processed: Path, events: dict, min_hours: float = 12.0) -> list[tuple]:
    """Recording gaps of ``min_hours`` or more (raw gaps, not logger freezes):
    missing runs on the saved grid that do not overlap a freeze mask."""
    grid = pd.read_csv(processed / "grid_10s.csv.gz", usecols=["timestamp", "observed", "bridged"],
                       parse_dates=["timestamp"])
    valid = (grid.observed | grid.bridged).to_numpy()
    t = grid.timestamp.to_numpy()
    masks = [(pd.Timestamp(str(m["start"])), pd.Timestamp(str(m["end"]))) for m in events.get("data_quality_masks", [])]
    edges = np.diff(np.r_[0, (~valid).astype(np.int8), 0])
    first, end = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    out = []
    for a, b in zip(first, end):
        if a == 0 or b >= len(valid):
            continue
        start, stop = pd.Timestamp(t[a - 1]), pd.Timestamp(t[b])
        if (stop - start) < pd.Timedelta(hours=min_hours):
            continue
        if any(ms < stop and me > start for ms, me in masks):
            continue
        out.append((start, stop))
    return out


def failure_times(events: dict) -> list[tuple[str, pd.Timestamp, pd.Timestamp]]:
    """(id, start, back in service) for each repair-forcing failure; without a
    repair the asset is back in service at the report's end."""
    out = []
    for f in events["failures"]:
        start = pd.Timestamp(str(f["start"]))
        back = pd.Timestamp(str(f["maintenance"] if f.get("maintenance") is not None else f["end"]))
        out.append((f["id"], start, back))
    return out


def hourly_table(windows: pd.DataFrame, scores: np.ndarray, threshold: float, events: dict,
                 outages: list, campaign_end: pd.Timestamp, history: pd.Timedelta = pd.Timedelta(hours=24)) -> pd.DataFrame:
    """One row per hour with a complete window ending on it: inputs, time to the
    next failure start (h), the event flag and the at-risk flag."""
    end = pd.DatetimeIndex(windows.window_end)
    on_hour = (end.minute == 0) & (end.second == 0)
    hours = end[on_hour]
    rows = np.flatnonzero(on_hour)
    end_ns = end.asi8
    scores = np.asarray(scores, dtype=float)
    lo = np.searchsorted(end_ns, (hours - history).asi8, side="right")
    hi = rows + 1
    csum = np.r_[0.0, np.cumsum(scores)]
    csum_alert = np.r_[0, np.cumsum(scores > threshold)]
    n = hi - lo
    smax = np.array([scores[a:b].max() for a, b in zip(lo, hi)])
    table = pd.DataFrame({"hour": hours, "score_max_24h": smax, "score_mean_24h": (csum[hi] - csum[lo]) / n,
                          "alert_share_24h": (csum_alert[hi] - csum_alert[lo]) / n, "windows_24h": n})
    for name, column in WINDOW_INPUTS.items():
        table[name] = windows[column].to_numpy()[rows]

    fails = failure_times(events)
    starts = np.array([s.value for _, s, _ in fails])
    t = table["hour"].to_numpy().astype("datetime64[ns]").astype("int64")
    at_risk = np.ones(len(t), bool)
    for _, s, back in fails:
        at_risk &= ~((t >= s.value) & (t < back.value))
    nxt = np.searchsorted(starts, t, side="right")          # first failure starting after the hour
    next_start = np.where(nxt < len(starts), starts[np.minimum(nxt, len(starts) - 1)], np.iinfo(np.int64).max)
    never = np.iinfo(np.int64).max
    out_starts = np.array(sorted(s.value for s, _ in outages), dtype=np.int64)
    if len(out_starts):
        k = np.searchsorted(out_starts, t, side="right")
        next_outage = np.where(k < len(out_starts), out_starts[np.minimum(k, len(out_starts) - 1)], never)
    else:
        next_outage = np.full(len(t), never)
    stop = np.minimum.reduce([next_start, next_outage, np.full(len(t), campaign_end.value)])
    table["event"] = (next_start <= stop) & (next_start < never)
    table["time_h"] = (stop - t) / 3.6e12
    table["censor"] = np.where(table["event"], "failure",
                               np.where(next_outage <= stop, "long_outage", "campaign_end"))
    table["next_failure"] = [fails[i][0] if i < len(fails) else None for i in nxt]
    table["at_risk"] = at_risk
    return table[at_risk].reset_index(drop=True)


def labels(table: pd.DataFrame, h: float) -> tuple[np.ndarray, np.ndarray]:
    """(y, known) for horizon h: a failure within h, and whether the row's outcome
    within h is known (censored before h means unknown)."""
    t, ev = table["time_h"].to_numpy(), table["event"].to_numpy()
    y = ev & (t <= h)
    known = y | (t > h)
    return y, known


# --- models ---------------------------------------------------------------------------------------

@dataclass
class Prep:
    median: np.ndarray
    mean: np.ndarray
    std: np.ndarray

    @staticmethod
    def fit(X: np.ndarray) -> "Prep":
        median = np.nanmedian(X, axis=0)
        filled = np.where(np.isnan(X), median, X)
        std = filled.std(axis=0)
        return Prep(median, filled.mean(axis=0), np.where(std > 0, std, 1.0))

    def __call__(self, X: np.ndarray) -> np.ndarray:
        X = np.asarray(X, dtype=float)
        return (np.where(np.isnan(X), self.median, X) - self.mean) / self.std

    def as_dict(self):
        return {"median": self.median.tolist(), "mean": self.mean.tolist(), "std": self.std.tolist()}


def constant_hazard(train: pd.DataFrame, horizon: float = max(HORIZONS)) -> float:
    """Failures per hour at risk; each hourly row is one hour at risk. A failure
    counts when the training rows hold its lead-up (a row with the failure within
    the longest horizon), so a held-out event does not leak in through rows
    weeks before it."""
    seen = train[train["event"] & (train["time_h"] <= horizon)]["next_failure"].nunique()
    return float(seen / max(len(train), 1))


def hazard_probability(lam: float, h: float) -> float:
    return float(1 - np.exp(-lam * h))


class HorizonModel:
    """One classifier per horizon on the standardized inputs, made monotone in h."""

    def __init__(self, kind: str = "logistic", seed: int = SEED):
        self.kind, self.seed = kind, seed
        self.models: dict[int, object] = {}
        self.prep: Prep | None = None
        self.prior: dict[int, float] = {}

    def _make(self):
        if self.kind == "logistic":
            return LogisticRegression(C=1.0, max_iter=2000)
        if self.kind == "forest":
            return RandomForestClassifier(n_estimators=200, min_samples_leaf=50, max_depth=6,
                                          random_state=self.seed, n_jobs=-1)
        raise ValueError(f"unknown model {self.kind!r}")

    def fit(self, table: pd.DataFrame, horizons=HORIZONS) -> "HorizonModel":
        X = table[INPUTS].to_numpy(float)
        self.prep = Prep.fit(X)
        Z = self.prep(X)
        for h in horizons:
            y, known = labels(table, h)
            self.prior[h] = float(y[known].mean()) if known.any() else 0.0
            if known.sum() == 0 or y[known].all() or not y[known].any():
                self.models[h] = None            # one class only: fall back to the base rate
                continue
            self.models[h] = self._make().fit(Z[known], y[known])
        return self

    def predict(self, X) -> np.ndarray:
        """(n, len(horizons)) probabilities, monotone along the horizons."""
        Z = self.prep(np.atleast_2d(X))
        cols = []
        for h in sorted(self.models):
            m = self.models[h]
            cols.append(np.full(len(Z), self.prior[h]) if m is None else m.predict_proba(Z)[:, 1])
        return np.maximum.accumulate(np.column_stack(cols), axis=1)


def brier(y, p) -> float:
    y, p = np.asarray(y, float), np.asarray(p, float)
    return float(np.mean((p - y) ** 2)) if len(y) else float("nan")


def reliability(y, p, bins: int = 10) -> pd.DataFrame:
    edges = np.linspace(0, 1, bins + 1)
    which = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    rows = []
    for b in range(bins):
        m = which == b
        rows.append({"bin": f"{edges[b]:.1f}-{edges[b + 1]:.1f}", "n": int(m.sum()),
                     "predicted": float(np.mean(p[m])) if m.any() else np.nan,
                     "observed": float(np.mean(y[m])) if m.any() else np.nan})
    return pd.DataFrame(rows)


def day_bootstrap(y, p_model, p_base, day, n=1000, seed=SEED) -> tuple[float, float]:
    """90 % interval of the Brier skill from resampling whole held-out days."""
    rng = np.random.default_rng(seed)
    days = np.unique(day)
    if len(days) < 2:
        return float("nan"), float("nan")
    idx = {d: np.flatnonzero(day == d) for d in days}
    skills = []
    for _ in range(n):
        pick = np.concatenate([idx[d] for d in rng.choice(days, len(days), replace=True)])
        b0 = brier(y[pick], p_base[pick])
        if b0 > 0:
            skills.append(1 - brier(y[pick], p_model[pick]) / b0)
    if not skills:
        return float("nan"), float("nan")
    lo, hi = np.percentile(skills, [5, 95])
    return float(lo), float(hi)


# --- leave-one-event-out -------------------------------------------------------------------------

def blocks_from_manifest(manifest: dict) -> dict:
    return {k: (pd.Timestamp(a), pd.Timestamp(b)) for k, (a, b) in manifest["folds"]["test_blocks"].items()}


def loeo(table: pd.DataFrame, blocks: dict, kind: str) -> pd.DataFrame:
    """Held-out predictions for every block row: model and constant-hazard baseline."""
    out = []
    hours = pd.DatetimeIndex(table["hour"])
    for ev, (a, b) in blocks.items():
        test = (hours >= a) & (hours < b)
        train = ~test
        if not test.any():
            continue
        lam = constant_hazard(table[train])
        model = HorizonModel(kind).fit(table[train])
        p = model.predict(table.loc[test, INPUTS].to_numpy(float))
        part = table[test].copy()
        part["fold"] = ev
        for j, h in enumerate(HORIZONS):
            part[f"p{h}"] = p[:, j]
            part[f"base{h}"] = hazard_probability(lam, h)
        out.append(part)
    return pd.concat(out, ignore_index=True)


def score_predictions(pred: pd.DataFrame) -> dict:
    """Brier score, skill and its interval per horizon, per event and pooled."""
    report = {}
    for h in HORIZONS:
        y_all, known_all = labels(pred, h)
        per_event = {}
        for ev, part in pred.groupby("fold"):
            y, known = labels(part, h)
            if known.sum() == 0:
                continue
            bs_m, bs_b = brier(y[known], part.loc[known, f"p{h}"]), brier(y[known], part.loc[known, f"base{h}"])
            per_event[ev] = {"n_rows": int(known.sum()), "positives": int(y[known].sum()),
                             "brier": bs_m, "brier_baseline": bs_b,
                             "skill": (1 - bs_m / bs_b) if bs_b > 0 else float("nan")}
        y, known = y_all, known_all
        pm, pb = pred.loc[known, f"p{h}"].to_numpy(), pred.loc[known, f"base{h}"].to_numpy()
        yk = y[known].astype(float)
        bs_m, bs_b = brier(yk, pm), brier(yk, pb)
        day = pd.DatetimeIndex(pred.loc[known, "hour"]).floor("D").asi8
        lo, hi = day_bootstrap(yk, pm, pb, day)
        report[h] = {"pooled": {"n_events": int(pred["fold"].nunique()), "n_rows": int(known.sum()),
                                "positives": int(yk.sum()), "brier": bs_m, "brier_baseline": bs_b,
                                "skill": (1 - bs_m / bs_b) if bs_b > 0 else float("nan"),
                                "skill_interval_90": [lo, hi], "meets_bar": bool(bs_b > 0 and 1 - bs_m / bs_b >= BAR)},
                     "per_event": per_event,
                     "reliability": reliability(yk, pm).to_dict(orient="records")}
    return report


# --- the frozen bundle ------------------------------------------------------------------------------

def fit_bundle(table: pd.DataFrame, kind: str, n_boot: int = 200, seed: int = SEED) -> dict:
    """The final model on every row, plus ``n_boot`` refits on whole days drawn with
    replacement; logistic models serialize to plain JSON."""
    if kind != "logistic":
        raise ValueError("only the logistic model is serialized to a JSON bundle; the forest stays an evaluation")
    rng = np.random.default_rng(seed)
    day = pd.DatetimeIndex(table["hour"]).floor("D").asi8
    days = np.unique(day)
    idx = {d: np.flatnonzero(day == d) for d in days}

    def export(m: HorizonModel) -> dict:
        return {"prep": m.prep.as_dict(),
                "horizons": {str(h): (None if mdl is None else {"coef": mdl.coef_[0].tolist(),
                                                                "intercept": float(mdl.intercept_[0])})
                             for h, mdl in m.models.items()},
                "prior": {str(h): p for h, p in m.prior.items()}}

    main = HorizonModel(kind).fit(table)
    boots = []
    for _ in range(n_boot):
        pick = np.concatenate([idx[d] for d in rng.choice(days, len(days), replace=True)])
        boots.append(export(HorizonModel(kind).fit(table.iloc[pick].reset_index(drop=True))))
    return {"kind": kind, "features": INPUTS, "horizons_h": list(HORIZONS), "main": export(main),
            "bootstrap": boots, "constant_hazard_per_h": constant_hazard(table)}


def predict_bundle(bundle: dict, row: dict, interval: float = 0.9) -> dict:
    """P(failure within h) with its bootstrap interval, for one hourly input row
    (pdm_common.frozen.SurvivalModel, which the prognostics service runs too)."""
    return SurvivalModel(bundle).predict(row, interval)


def attribution(bundle: dict, row: dict, horizon: int = 72) -> list[dict]:
    """Exact per-input contributions to the log-odds of P(failure within ``horizon``)
    against the training mean (linear SHAP for a logistic model), largest first."""
    return SurvivalModel(bundle).attribution(row, horizon)


# --- command line ----------------------------------------------------------------------------------

def build_table(processed: Path, profile_path: Path, threshold: float | None, scores_path: Path | None):
    import harness as h
    from pdm_common.events import load_events
    from pdm_common.profile import events_file, load_profile

    profile = load_profile(profile_path)
    events = load_events(events_file(profile, profile_path))
    data = h.load(processed)
    if scores_path:
        saved = np.load(scores_path)
        scores = saved["scores"] if "scores" in saved.files else saved["PCA"]
    else:
        import detectors as dets
        roles = h.assign_roles(data.windows)
        scaled = h.scale(data.windows, h.detector_features(data.feature_dict), roles)
        scores = dets.PCARecon().fit(scaled.X[scaled.fit_rows]).score(scaled.X)
    if threshold is None:
        sel = processed / "detector_selection" / "selection.json"
        threshold = json.loads(sel.read_text())["threshold"]
    outages = outages_from_grid(processed, events)
    campaign_end = pd.Timestamp(str(events["campaign"][1])) + pd.Timedelta(days=1)
    table = hourly_table(data.windows, scores, threshold, events, outages, campaign_end)
    return table, data, events, outages, threshold


def plot_reliability(results: dict, out: Path) -> list[Path]:
    """One reliability chart per model: predicted against observed share, per horizon."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    paths = []
    for kind, report in results.items():
        fig, axes = plt.subplots(1, len(HORIZONS), figsize=(11, 3.6), sharey=True)
        for ax, h in zip(axes, HORIZONS):
            rel = pd.DataFrame(report[h]["reliability"]).dropna()
            ax.plot([0, 1], [0, 1], color="#c3c2b7", lw=1)
            ax.plot(rel["predicted"], rel["observed"], "o-", color="#2a78d6", lw=1.4, ms=4)
            pooled = report[h]["pooled"]
            ax.set_title(f"{h} h: skill {pooled['skill']:+.2f} (n = {pooled['n_events']} events)", fontsize=10)
            ax.set_xlabel("predicted P(failure within h)")
            ax.set_xlim(0, 1)
            ax.set_ylim(0, 1)
        axes[0].set_ylabel("observed share")
        fig.suptitle(f"{kind}: reliability on the held-out blocks, leave-one-event-out", x=0.01, ha="left",
                     fontsize=11, fontweight="bold")
        fig.tight_layout(rect=(0, 0, 1, 0.92))
        path = out / f"reliability_{kind}.png"
        fig.savefig(path, dpi=110)
        plt.close(fig)
        paths.append(path)
    return paths


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--processed", required=True)
    p.add_argument("--profile", default=str(ROOT / "profiles/metropt3_apu/asset_profile.yaml"))
    p.add_argument("--threshold", type=float, help="the detector threshold the alert share uses "
                                                    "(default: selection.json in the processed folder)")
    p.add_argument("--scores", help="saved window scores (.npz with 'scores' or 'PCA'); default: refit PCA")
    p.add_argument("--models", default="logistic,forest")
    p.add_argument("--out", default=str(ROOT / "eval" / "out" / "prognostics"))
    p.add_argument("--save-bundle", help="folder for the frozen survival bundle (logistic)")
    p.add_argument("--n-boot", type=int, default=200)
    args = p.parse_args(argv)
    t0 = time.time()
    table, data, events, outages, threshold = build_table(Path(args.processed), Path(args.profile),
                                                          args.threshold, Path(args.scores) if args.scores else None)
    print(f"{len(table):,} hourly rows at risk; {int(table.event.groupby(table.next_failure).any().sum())} failures; "
          f"{len(outages)} long outages censor; alert share at threshold {threshold:.4f} ({time.time() - t0:.0f} s)")
    blocks = blocks_from_manifest(data.manifest)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = {}
    for kind in args.models.split(","):
        pred = loeo(table, blocks, kind)
        report = score_predictions(pred)
        results[kind] = report
        pred.to_csv(out / f"predictions_{kind}.csv", index=False)
        print(f"\n{kind}: Brier skill against constant hazard, leave-one-event-out (n = 4 events, pooled):")
        for hz in HORIZONS:
            pooled = report[hz]["pooled"]
            lo, hi = pooled["skill_interval_90"]
            events_txt = ", ".join(f"{ev} {v['skill']:+.2f}" for ev, v in report[hz]["per_event"].items())
            print(f"  {hz:>3} h: skill {pooled['skill']:+.3f} [{lo:+.3f}, {hi:+.3f}] "
                  f"(Brier {pooled['brier']:.4f} vs {pooled['brier_baseline']:.4f}; {pooled['positives']} positive rows "
                  f"of {pooled['n_rows']}); per event {events_txt}; bar {BAR}: "
                  f"{'met' if pooled['meets_bar'] else 'NOT met'}")
    summary = {"threshold": threshold, "inputs": INPUTS, "horizons_h": list(HORIZONS), "bar": BAR,
               "rows": len(table), "outages": [[str(a), str(b)] for a, b in outages],
               "models": {k: {str(hz): {kk: vv for kk, vv in v[hz].items() if kk != "reliability"}
                              for hz in HORIZONS} for k, v in results.items()},
               "reliability": {k: {str(hz): v[hz]["reliability"] for hz in HORIZONS} for k, v in results.items()}}
    (out / "survival.json").write_text(json.dumps(summary, indent=2, default=float))
    plot_reliability(results, out)
    if args.save_bundle:
        bundle = fit_bundle(table, "logistic", n_boot=args.n_boot)
        mean_skill = float(np.mean([results["logistic"][hz]["pooled"]["skill"] for hz in HORIZONS])) \
            if "logistic" in results else None
        bundle.update({"schema": "survival_bundle_v1", "threshold_used": threshold,
                       "inputs_meta": {"history": "24h", "window_inputs": list(WINDOW_INPUTS)},
                       "metrics": {f"skill_{hz}h": results["logistic"][hz]["pooled"]["skill"] for hz in HORIZONS}
                       if "logistic" in results else {}, "mean_skill": mean_skill,
                       "data": {"windows_key": data.windows_key, "source_sha256": data.manifest["source"]["sha256"],
                                "rows": len(table)}})
        folder = Path(args.save_bundle)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "bundle.json").write_text(json.dumps(bundle, sort_keys=True, separators=(",", ":")))
        print(f"\nsaved the survival bundle ({args.n_boot} bootstrap refits) to {folder / 'bundle.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
