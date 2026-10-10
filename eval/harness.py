"""The evaluation harness (T27; EVL-01 to EVL-04, PR-06, G1).

Turns a detector's window scores into the G1 numbers: lead time per event at
the false-alarm budget, with intervals, and false alarms per week on weeks
neither the model nor its threshold has seen, with and without the uncertain
periods. Every detector is judged the same way, so the same functions serve
the comparison of candidates, the frozen detector and the 2022 transfer test.

Moved from notebooks/detector_selection (v7.2) Sections 1-2 and 5.1 with the
logic unchanged; the gate comes from pdm_common.gate. The rule, fixed before
any results:

1. Every detector is fitted on the same healthy windows (role ``fit``) and
   gets one threshold, set on separate healthy weeks (``calibrate``) so that
   gated incidents stay at or below the budget (two a week).
2. It is eligible if its false-alarm rate on a third set of weeks
   (``check``) is consistent with the budget: the 90 % interval reaches it.
3. A deep model stays in the running only if it beats both z-score and PCA
   by more than 1 h on at least one event and is no more than 1 h worse on
   any (EVL-05).
4. Lead time decides: the median over thresholds re-drawn from resampled
   calibration days; within 1 h of the best on every event is a tie.
5. Ties break toward per-signal attribution, then a multivariate model, then
   the lower held-out false-alarm rate, then the simpler model.

All windows are read from a processed folder (tools/build_windows.py or the
EDA notebook): windows_10min.csv.gz, feature_dictionary.csv and manifest.json.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from pdm_common.gate import GateParams, gate_passes, incidents, lead_at_report, ns

SEED = 0
BUDGET = 2.0                  # PRD G1: false alarms per week
TOLERANCE_H = 1.0             # leads within an hour of each other are a tie
HOUR_NS, DAY_NS, WEEK_MIN = 3_600 * 10**9, 86_400 * 10**9, 7 * 1_440
EARLY = pd.Timedelta(hours=72)  # early alarms are read back this far before a report
ROLES = ["fit", "calibrate", "check"]
UNCERTAIN_TAGS = ["probable_leak", "load_held", "lps_heavy"]   # reported even when absent ("0 of 0")


# --- data ---------------------------------------------------------------------------

@dataclass
class Data:
    processed: Path
    windows: pd.DataFrame
    manifest: dict
    feature_dict: pd.DataFrame
    windows_key: str
    failures: dict          # id -> (start, end), naive UTC
    uncertain: list         # (start, end, tag)
    catalogue: dict | None = None   # the folder's data-catalogue entry (ING-13)


def windows_key(window_end) -> str:
    """SHA-256 of the window end times in whole seconds, first 16 hex digits:
    ties saved scores and frozen models to this window table."""
    seconds = pd.DatetimeIndex(window_end).to_numpy().astype("datetime64[s]").astype("int64")
    return hashlib.sha256(seconds.tobytes()).hexdigest()[:16]


def load(processed) -> Data:
    """Read a processed folder and check its window table against its manifest."""
    processed = Path(processed)
    windows = pd.read_csv(processed / "windows_10min.csv.gz", parse_dates=["window_start", "window_end"])
    manifest = json.loads((processed / "manifest.json").read_text())
    expected = manifest["expected_counts"]
    if len(windows) != expected["complete_windows"]:
        raise ValueError(f"{processed}: {len(windows)} windows, manifest says {expected['complete_windows']}")
    if int((windows.zone == "negative").sum()) != expected["negative_windows"]:
        raise ValueError(f"{processed}: negative window count does not match manifest.json")
    feature_dict = pd.read_csv(processed / "feature_dictionary.csv")
    failures = {f["id"]: (pd.Timestamp(f["start"]), pd.Timestamp(f["end"])) for f in manifest["zones"]["failures"]}
    uncertain = [(pd.Timestamp(s), pd.Timestamp(e), tag) for s, e, tag in manifest["zones"]["uncertain_periods"]]
    data = Data(processed, windows, manifest, feature_dict, windows_key(windows.window_end), failures, uncertain)
    data.catalogue = catalogue_entry(processed)
    return data


def catalogue_entry(processed: Path) -> dict | None:
    """The folder's data-catalogue entry (ING-13), matched by its manifest's hash;
    None, with a warning, when the folder was never recorded."""
    from pdm_common import catalogue
    from pdm_common.hashing import sha256_file
    found = catalogue.find(manifest_sha256=sha256_file(Path(processed) / "manifest.json"))
    if not found:
        print(f"warning: {processed} is not in the data catalogue ({catalogue.default_path()}); "
              f"record it with tools/build_windows.py --catalogue-only {processed}", file=sys.stderr)
        return None
    return found[-1]


def assign_roles(windows: pd.DataFrame) -> np.ndarray:
    """fit / calibrate / check for negative windows that read no test block, else 'other'.

    All three leave out every window whose look-back reaches a test block, not
    only the current fold's, so one fitted model serves every fold."""
    negative = (windows.zone == "negative").to_numpy()
    reads_a_block = windows.filter(like="lookback_").any(axis=1).to_numpy()
    if "neg_role" in windows:
        return np.where(negative & ~reads_a_block, windows.neg_role, "other")
    week = windows.window_start.dt.isocalendar().week.astype(int).to_numpy()
    fit_half = negative & (windows.neg_split == "fit").to_numpy() & ~reads_a_block
    cal_half = negative & (windows.neg_split == "calibrate").to_numpy() & ~reads_a_block
    return np.select([fit_half, cal_half & (week % 4 == 1), cal_half & (week % 4 == 3)],
                     ["fit", "calibrate", "check"], default="other")


def detector_features(feature_dict: pd.DataFrame, flag: str = "detector_v1") -> list[str]:
    """The frozen detector's inputs: the dictionary rows flagged ``detector_v1``."""
    if flag in feature_dict:
        return list(feature_dict.feature[feature_dict[flag].astype(bool)])
    drop = set(feature_dict.feature[feature_dict.get("drop_for_tabular", False).astype(bool)]) \
        if "drop_for_tabular" in feature_dict else set()
    flags = set(feature_dict.feature[feature_dict.role == "data-quality flag"])
    return [f for f in feature_dict.feature if f not in drop | flags]


@dataclass
class Scaled:
    features: list
    X: np.ndarray
    healthy_median: np.ndarray
    mean: np.ndarray
    std: np.ndarray
    is_fit: np.ndarray
    fit_rows: np.ndarray


def scale(windows: pd.DataFrame, features: list, roles: np.ndarray) -> Scaled:
    """Standardize on the fit windows that have every feature; a missing value
    (a share or deficit without enough history) takes its healthy median."""
    X_raw = windows[features].to_numpy(float)
    is_fit = roles == "fit"
    fit_rows = is_fit & ~np.isnan(X_raw).any(axis=1)
    median = np.nanmedian(X_raw[is_fit], axis=0)
    filled = np.where(np.isnan(X_raw), median, X_raw)
    mean, std = filled[fit_rows].mean(axis=0), filled[fit_rows].std(axis=0)
    return Scaled(list(features), (filled - mean) / std, median, mean, std, is_fit, fit_rows)


def apply_scaler(X_raw: np.ndarray, median, mean, std) -> np.ndarray:
    """The same scaling for windows scored later (the detector service)."""
    X_raw = np.asarray(X_raw, dtype=float)
    return (np.where(np.isnan(X_raw), median, X_raw) - mean) / std


# --- the judging context --------------------------------------------------------------

@dataclass
class Context:
    """Everything evaluate() reads besides the scores, computed once per window table."""
    end_ns: np.ndarray
    is_cal: np.ndarray
    is_check: np.ndarray
    period_of: np.ndarray
    uncertain: list
    failures: dict
    block: dict
    start_ns: dict
    stop_ns: dict
    during: dict
    before: dict
    gate: GateParams
    budget: float = BUDGET
    extras: dict = field(default_factory=dict)


def context(data: Data, roles: np.ndarray, gate: GateParams, budget: float = BUDGET) -> Context:
    w = data.windows
    w_start, w_end = w.window_start.to_numpy(), w.window_end.to_numpy()
    zone_detail = w.zone_detail.to_numpy()
    period_of = np.full(len(w), -1)
    for k, (s, e, tag) in enumerate(data.uncertain):
        period_of[(zone_detail == f"uncertain:{tag}") & (w_start <= e.to_datetime64())
                  & (w_end > s.to_datetime64())] = k
    fails = data.failures
    return Context(
        end_ns=ns(w_end), is_cal=roles == "calibrate", is_check=roles == "check", period_of=period_of,
        uncertain=data.uncertain, failures=fails,
        block={ev: w[f"block_{ev}"].to_numpy() for ev in fails},
        start_ns={ev: ns(np.array([s.to_datetime64()]))[0] for ev, (s, e) in fails.items()},
        stop_ns={ev: ns(np.array([e.to_datetime64()]))[0] for ev, (s, e) in fails.items()},
        during={ev: (w_end > s.to_datetime64()) & (w_end <= e.to_datetime64()) for ev, (s, e) in fails.items()},
        before={ev: (w_end > (s - pd.Timedelta(hours=24)).to_datetime64()) & (w_end <= s.to_datetime64())
                for ev, (s, e) in fails.items()},
        gate=gate, budget=budget)


def series(ctx: Context, mask, scores):
    """End times (ns) and scores of the windows in mask, in time order."""
    end = ctx.end_ns[mask]
    order = np.argsort(end, kind="stable")
    return end[order], np.asarray(scores)[mask][order]


def _gate(ctx: Context, end, alerts):
    g = ctx.gate
    return incidents(end, gate_passes(end, alerts, g.span_ns, g.min_fraction, g.min_present), g.hold_off_ns)


def threshold_grid(cal_scores, n: int = 160) -> np.ndarray:
    """The 50th to 99.9999th percentiles of the calibration scores, then one above the maximum."""
    grid = np.unique(np.quantile(cal_scores, 1 - np.logspace(np.log10(0.5), -6, n)))
    top = cal_scores.max()
    return np.concatenate([grid, [top + 1e-9 + abs(top) * 1e-9]])


def daily_incidents(ctx: Context, end_ns, scores, thresholds):
    """Incidents opened per calendar day (rows) at each threshold (columns), and
    the minutes of window time each day holds."""
    day = end_ns // DAY_NS
    days, which = np.unique(day, return_inverse=True)
    minutes = np.bincount(which).astype(float)
    counts = np.zeros((len(days), len(thresholds)), dtype=np.int32)
    for j, tau in enumerate(thresholds):
        opens, _, _ = _gate(ctx, end_ns, scores > tau)
        counts[:, j] = np.bincount(np.searchsorted(days, opens // DAY_NS), minlength=len(days))
    return minutes, counts


def per_week(counts, minutes):
    return counts.sum(axis=0) / (minutes.sum() / WEEK_MIN)


def pick_threshold(rates, budget: float = BUDGET) -> int:
    """Index of the lowest threshold whose rate, and every higher threshold's, is within budget."""
    over = np.flatnonzero(np.asarray(rates) > budget)
    return 0 if len(over) == 0 else min(over[-1] + 1, len(rates) - 1)


def auroc(ctx: Context, scores, positive) -> float:
    scores = np.asarray(scores)
    y = np.r_[np.ones(positive.sum()), np.zeros(ctx.is_check.sum())]
    return roc_auc_score(y, np.r_[scores[positive], scores[ctx.is_check]]) if positive.any() else np.nan


def far_with_uncertain(ctx: Context, counts_k, minutes_k, scores, tau) -> tuple[float, list]:
    """PR-06's second figure: the uncertain periods counted as false alarms too.
    Returns the rate and the incidents per uncertain period (the audit list)."""
    per_period = []
    for k in range(len(ctx.uncertain)):
        e_u, s_u = series(ctx, ctx.period_of == k, scores)
        per_period.append(len(_gate(ctx, e_u, s_u > tau)[0]))
    rate = ((counts_k.sum() + sum(per_period)) / ((minutes_k.sum() + (ctx.period_of >= 0).sum()) / WEEK_MIN))
    return rate, per_period


def evaluate(scores, ctx: Context, n_boot: int = 500, seed: int = SEED, threshold: float | None = None) -> dict:
    """The G1 numbers for one detector's window scores.

    With ``threshold`` given (SPOT, a frozen value) the point figures are taken
    at it; the lead intervals still come from re-drawing the empirical rule on
    resampled calibration days, which is how the selection rule compared detectors."""
    rng = np.random.default_rng(seed)
    scores = np.asarray(scores)
    r = {}
    end_c, s_c = series(ctx, ctx.is_cal, scores)
    thresholds = threshold_grid(s_c)
    minutes_c, counts_c = daily_incidents(ctx, end_c, s_c, thresholds)
    j = pick_threshold(per_week(counts_c, minutes_c), ctx.budget)
    if threshold is None:
        tau = r["threshold"] = float(thresholds[j])
        r["threshold kind"] = "empirical"
        r["at grid floor"] = bool(j == 0)       # the budget never bound: the grid should reach lower
        r["calibrate incidents"] = int(counts_c[:, j].sum())
        r["calibrate rate"] = float(per_week(counts_c[:, [j]], minutes_c)[0])
    else:
        tau = r["threshold"] = float(threshold)
        r["threshold kind"] = "given"
        m_c, c_c = daily_incidents(ctx, end_c, s_c, [tau])
        r["at grid floor"] = False
        r["calibrate incidents"] = int(c_c.sum())
        r["calibrate rate"] = float(per_week(c_c, m_c)[0])
    r["threshold percentile"] = float(100 * (s_c <= tau).mean())

    end_k, s_k = series(ctx, ctx.is_check, scores)
    minutes_k, counts_k = daily_incidents(ctx, end_k, s_k, [tau])
    r["far"] = float(per_week(counts_k, minutes_k)[0])
    r["check incidents"] = int(counts_k.sum())
    r["check weeks"] = float(minutes_k.sum() / WEEK_MIN)
    draws = rng.integers(0, len(minutes_k), (1000, len(minutes_k)))
    boot = counts_k[draws, 0].sum(axis=1) / (minutes_k[draws].sum(axis=1) / WEEK_MIN)
    r["far lo"], r["far hi"] = (float(x) for x in np.percentile(boot, [5, 95]))

    rate_u, per_period = far_with_uncertain(ctx, counts_k, minutes_k, scores, tau)
    for tag in UNCERTAIN_TAGS + sorted({t for _, _, t in ctx.uncertain} - set(UNCERTAIN_TAGS)):
        periods = [k for k, (_, _, t) in enumerate(ctx.uncertain) if t == tag]
        r[f"alarmed {tag}"] = f"{sum(per_period[k] > 0 for k in periods)} of {len(periods)}"
    r["far with uncertain"] = float(rate_u)
    r["uncertain audit"] = [{"start": str(s), "end": str(e), "tag": t, "incidents": int(n)}
                            for (s, e, t), n in zip(ctx.uncertain, per_period)]

    draws = rng.integers(0, len(minutes_c), (n_boot, len(minutes_c)))
    boot_j = np.array([pick_threshold(counts_c[d].sum(axis=0) / (minutes_c[d].sum() / WEEK_MIN), ctx.budget)
                       for d in draws])
    for ev in ctx.failures:
        end_b, s_b = series(ctx, ctx.block[ev], scores)
        lead = {}
        for jj in np.unique(boot_j):
            o, _, c = _gate(ctx, end_b, s_b > thresholds[jj])
            lead[jj] = lead_at_report(o, c, ctx.start_ns[ev], ctx.stop_ns[ev])
        b = np.array([lead[jj] for jj in boot_j])
        o, last, c = _gate(ctx, end_b, s_b > tau)
        r[f"{ev} lead"] = lead_at_report(o, c, ctx.start_ns[ev], ctx.stop_ns[ev])
        r[f"{ev} caught share"] = float(np.mean(~np.isnan(b)))
        # a miss ranks below any lead, so a percentile that lands on misses reads as missed (NaN)
        q = np.percentile(np.where(np.isnan(b), -np.inf, b), [5, 50, 95], method="nearest")
        r[f"{ev} lead lo"], r[f"{ev} lead median"], r[f"{ev} lead hi"] = \
            (float(x) for x in np.where(np.isinf(q), np.nan, q))
        r[f"{ev} incidents"] = [(int(a), int(z), int(cl)) for a, z, cl in zip(o, last, c)]
        r[f"{ev} early alarms"] = int(((o >= ctx.start_ns[ev] - EARLY.value) & (c <= ctx.start_ns[ev])).sum())
        r[f"{ev} AUROC during"] = float(auroc(ctx, scores, ctx.during[ev]))
        r[f"{ev} AUROC 24 h before"] = float(auroc(ctx, scores, ctx.before[ev]))
    return r


# --- the selection rule (Section 5.1) ------------------------------------------------------

def select(results: dict, info: dict, failures, budget: float = BUDGET, tolerance: float = TOLERANCE_H,
           baselines=("z-score", "PCA")) -> dict:
    """Apply the rule fixed before any results; returns its trace and the pick."""
    median_lead = pd.DataFrame({n: {ev: r[f"{ev} lead median"] for ev in failures} for n, r in results.items()}).T
    eligible = [n for n, r in results.items() if r["far lo"] <= budget]
    if not eligible:
        raise ValueError("no detector keeps to the false-alarm budget on the held-out weeks")
    reference = [b for b in baselines if b in eligible] or [b for b in baselines if b in results]
    baseline_best = median_lead.loc[reference].max()

    def gain(name):
        mine = median_lead.loc[name].fillna(-np.inf)
        base = baseline_best.fillna(-np.inf)
        return (mine - base).where(~(np.isinf(mine) & np.isinf(base)), 0.0)

    def passes_evl05(name):
        g = gain(name)
        return bool((g > tolerance).any() and not (g < -tolerance).any())

    deep_dropped = [n for n in eligible if info[n]["family"].startswith("deep") and not passes_evl05(n)]
    pool = [n for n in eligible if n not in deep_dropped]
    best = median_lead.loc[pool].max()

    def on_frontier(name):
        for ev in failures:
            if np.isnan(best[ev]):
                continue
            lead = median_lead.loc[name, ev]
            if np.isnan(lead) or lead < best[ev] - tolerance:
                return False
        return True

    frontier = [n for n in pool if on_frontier(n)] or pool
    ranked = sorted(frontier, key=lambda n: (not info[n]["attribution"], not info[n]["multivariate"],
                                             round(results[n]["far"], 2), info[n]["complexity"]))
    return {"eligible": eligible, "deep_dropped_evl05": deep_dropped,
            "best_median_lead_h": {k: (None if np.isnan(v) else round(float(v), 2)) for k, v in best.items()},
            "frontier": frontier, "ranked": ranked, "tolerance_h": tolerance, "selected": ranked[0]}


# --- tables ---------------------------------------------------------------------------------

def _fmt_lead(v):
    return "missed" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:+.1f}"


def lead_text(r, ev):
    """Median lead [90 % interval] over the threshold re-draws; 'miss' where draws miss."""
    if np.isnan(r[f"{ev} lead median"]):
        return f"missed in {1 - r[f'{ev} caught share']:.0%} of draws"
    lo = "miss" if np.isnan(r[f"{ev} lead lo"]) else f"{r[f'{ev} lead lo']:+.1f}"
    return f"{r[f'{ev} lead median']:+.1f} [{lo}, {r[f'{ev} lead hi']:+.1f}]"


def comparison_table(results: dict, info: dict, failures) -> pd.DataFrame:
    rows = {}
    for name, r in results.items():
        caught = sum(not np.isnan(r[f"{ev} lead"]) for ev in failures)
        warned = [sum(r[f"{ev} lead"] >= h for ev in failures) for h in (2, 24, 72)]
        rows[name] = {
            "family": info[name]["family"],
            "attribution": "yes" if info[name]["attribution"] else "no",
            "false alarms / week, held out [90 %]": f"{r['far']:.2f} [{r['far lo']:.2f}, {r['far hi']:.2f}]",
            "with uncertain periods": round(r["far with uncertain"], 2),
            "failures caught": f"{caught} of {len(failures)}",
            "warned 2 / 24 / 72 h ahead": " / ".join(map(str, warned)),
            # one column per uncertain-period tag in events.yaml, named from the file (CFG-03)
            **{f"{key[len('alarmed '):]} periods alarmed": value for key, value in r.items()
               if key.startswith("alarmed ")},
            "threshold percentile": round(r["threshold percentile"], 1),
        }
    table = pd.DataFrame(rows).T
    table.index.name = "detector"
    return table


def lead_table(results: dict, failures) -> pd.DataFrame:
    table = pd.DataFrame({name: {**{key: value for ev in failures for key, value in
                                    [((ev, "at the threshold"), _fmt_lead(r[f"{ev} lead"])),
                                     ((ev, "median [90 %]"), lead_text(r, ev))]},
                                 ("early alarms", ", ".join(failures)):
                                     ", ".join(str(r[f"{ev} early alarms"]) for ev in failures)}
                          for name, r in results.items()}).T
    table.index.name = "detector"
    return table


def auroc_table(results: dict, failures) -> pd.DataFrame:
    table = pd.DataFrame({name: {(when, ev): r[f"{ev} AUROC {key}"] for when, key in
                                 [("in the report", "during"), ("24 h before", "24 h before")] for ev in failures}
                          for name, r in results.items()}).T
    table.columns = pd.MultiIndex.from_tuples(table.columns)
    table.index.name = "detector"
    return table


def summary(r: dict, failures) -> dict:
    """The selection.json fields for one detector's result."""
    rnd = lambda v: None if v is None or np.isnan(v) else round(float(v), 2)  # noqa: E731
    return {
        "threshold": r["threshold"], "threshold_kind": r["threshold kind"],
        "threshold_percentile": round(r["threshold percentile"], 1),
        "held_out_far": [round(r[k], 3) for k in ("far", "far lo", "far hi")],
        "far_with_uncertain": round(r["far with uncertain"], 3),
        "lead_h": {ev: rnd(r[f"{ev} lead"]) for ev in failures},
        "lead_h_median": {ev: rnd(r[f"{ev} lead median"]) for ev in failures},
        "lead_h_interval_90": {ev: [rnd(r[f"{ev} lead lo"]), rnd(r[f"{ev} lead hi"])] for ev in failures},
        "calibrate_incidents": r["calibrate incidents"], "check_incidents": r["check incidents"],
        "check_weeks": round(r["check weeks"], 2), "uncertain_audit": r["uncertain audit"],
    }


# --- the raw sequences (Section 4.1), for the sequence autoencoder ---------------------------------

def sequence_inputs(data: Data, profile: dict, spec, is_fit: np.ndarray):
    """(sequences(rows) function, scaler dict) over the saved grid: the detector
    inputs plus the trailing-6 h loaded and stopped shares per point, scored once
    half of the 6 h has data, standardized on the points the fit windows cover."""
    inputs = list(profile["detector"]["inputs"])
    motor = spec.roles["motor_current"]
    loaded = next(f["compute"] for f in spec.features if f["compute"]["kind"] == "share_of_time"
                  and "above" in f["compute"])
    stopped = next(f["compute"] for f in spec.features if f["compute"]["kind"] == "share_of_time"
                   and "below" in f["compute"])
    step = pd.Timedelta(spec.grid_step)
    win_bins = spec.win_bins
    six_h = int(pd.Timedelta(hours=6) / step)
    grid = pd.read_csv(data.processed / "grid_10s.csv.gz", usecols=["timestamp", *inputs, "observed", "bridged"],
                       parse_dates=["timestamp"])
    valid = (grid.observed | grid.bridged).to_numpy()
    raw = grid[inputs].to_numpy(float)
    raw[~valid] = np.nan
    current = pd.Series(raw[:, inputs.index(motor)])
    have = pd.Series(valid.astype(float)).rolling(six_h, min_periods=1).sum()
    enough = (have >= six_h / 2).to_numpy()
    shares = [np.where(enough, (state.astype(float).where(valid).rolling(six_h, min_periods=1).sum() / have)
                       .to_numpy(), np.nan)
              for state in (current > float(loaded["above"]), current < float(stopped["below"]))]
    channels = np.column_stack([raw, *shares]).astype(np.float32)
    grid_times = grid.timestamp.to_numpy()
    last_point = (data.windows.window_end - step).to_numpy()
    pos = np.searchsorted(grid_times, last_point)
    if not (grid_times[pos] == last_point).all():
        raise ValueError("window ends do not sit on the grid")
    offsets = np.arange(-win_bins + 1, 1)
    covered = np.zeros(len(grid), dtype=bool)
    covered[(pos[is_fit][:, None] + offsets).ravel()] = True
    ch_mean, ch_std = np.nanmean(channels[covered], axis=0), np.nanstd(channels[covered], axis=0)
    ch_median = np.nanmedian(channels[covered], axis=0)
    Z = ((np.where(np.isnan(channels), ch_median, channels) - ch_mean) / ch_std).astype(np.float32)

    def sequences(rows):
        return Z[pos[rows][:, None] + offsets]

    names = inputs + ["loaded_share_6h", "stopped_share_6h"]
    return sequences, {"channels": names, "mean": ch_mean.tolist(), "std": ch_std.tolist(),
                       "median": ch_median.tolist()}


def fit_key(is_fit: np.ndarray) -> str:
    """Identifies the fit windows: SHA-256 of their row numbers, first 16 hex digits."""
    return hashlib.sha256(np.flatnonzero(is_fit).astype(np.int64).tobytes()).hexdigest()[:16]
