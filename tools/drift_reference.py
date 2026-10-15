"""Healthy drift reference and thresholds for services/drift (T25; OPS-04).

    python tools/drift_reference.py --processed data/processed_v4
    python tools/drift_reference.py --processed data/processed_v4 --check   # rebuild, compare, write nothing

Healthy data only: the grid points of <processed>/grid_10s.csv.gz in the
negative zone that hold an observed value, split by the regimes of events.yaml
(``pdm_common.zones.regime_of`` on each point). For each signal in the
profile's ``drift.signals`` and each regime it writes:

* the reference: 200 quantiles of the healthy values at (k + 0.5) / 200, each
  standing for half a percent of them, with their number and standard
  deviation;
* the threshold. The service's own check is run over the healthy data: at
  every ``drift.every`` boundary aligned to the epoch, the healthy points of the
  boundary's regime in the last ``drift.window``, kept when there are at least
  ``min_samples`` of them (half the window at the profile's sampling step). The
  threshold is 1.25 times the largest distance such a healthy window reaches, so
  no healthy window of the reference period alerts and the margin covers healthy
  days it did not contain.

Why not the 99th percentile of non-overlapping daily blocks: on the 2020 data a
regime has 28 to 63 such blocks, so their 99th percentile is about their
maximum, and the service's overlapping 6-hourly windows go past it on healthy
data. Both figures are kept in the file (``p99_daily_blocks``,
``windows_over_p99_daily``) so the choice can be checked.

The file also records events.yaml's ``gap_edges`` rule (checked against the
processed folder's manifest): the gap edges are grey, so the reference holds
none, and the service leaves the same first 15 minutes after a recording gap
out of the stream.

The distance is services/drift/drift.py's ``drift_distance`` -- this file
imports it -- so the thresholds are set with exactly the computation the
service runs, on the rounded quantiles the service reads.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "services" / "drift"))

from drift import QUANTILES, REFERENCE_SCHEMA, drift_distance, summarize  # noqa: E402
from pdm_common.events import half_open, load_events  # noqa: E402
from pdm_common.hashing import sha256_file  # noqa: E402
from pdm_common.profile import events_file, load_profile  # noqa: E402
from pdm_common.timeutil import iso_utc, parse_duration  # noqa: E402
from pdm_common.zones import regime_of  # noqa: E402

MIN_SHARE = 0.5          # a window needs half its length in samples for a verdict
MARGIN = 1.25            # threshold = MARGIN x the largest distance of a healthy check window
MIN_WINDOWS = 20         # a regime with fewer healthy check windows gets no threshold
DAY_NS = 86_400 * 10**9

THRESHOLD_RULE = (f"{MARGIN} x the largest distance of the regime's healthy check windows: every drift.every "
                  "boundary aligned to the epoch, the healthy points of the boundary's regime in the last "
                  "drift.window, at least min_samples of them. No healthy window of the reference period "
                  f"alerts; the {round((MARGIN - 1) * 100)} % margin is for healthy days it did not contain.")


def read_healthy(processed: Path, signals: list[str], ranges: dict) -> pd.DataFrame:
    """Healthy grid points: negative zone, observed; values outside their profile range are NaN."""
    grid = pd.read_csv(processed / "grid_10s.csv.gz", usecols=["timestamp", *signals, "observed", "zone"],
                       parse_dates=["timestamp"])
    healthy = grid[(grid["zone"] == "negative") & grid["observed"].astype(bool)].reset_index(drop=True)
    for name in signals:
        lo, hi = ranges.get(name, (0.0, 1.0))
        v = healthy[name].astype(float)
        healthy[name] = v.where((v >= lo) & (v <= hi))
    times = pd.DatetimeIndex(healthy["timestamp"])
    healthy["timestamp"] = times.tz_localize("UTC") if times.tz is None else times.tz_convert("UTC")
    return healthy


def check_windows(healthy: pd.DataFrame, regime: np.ndarray, signals: list[str], refs: dict, regimes: dict,
                  window_ns: int, every_ns: int, min_samples: int) -> pd.DataFrame:
    """The service's check over the healthy points: one row per boundary and signal with enough data."""
    ns = healthy["timestamp"].dt.tz_convert(None).to_numpy().astype("datetime64[ns]").astype(np.int64)
    first = -(-int(ns[0]) // every_ns) * every_ns
    bounds = np.arange(first, int(ns[-1]) + every_ns + 1, every_ns, dtype=np.int64)
    bound_regime = regime_of(pd.DatetimeIndex(bounds.astype("datetime64[ns]")).tz_localize("UTC"), regimes)
    values = {s: healthy[s].to_numpy(float) for s in signals}
    rows = []
    for at, r in zip(bounds, bound_regime):
        if r is None or r not in refs:
            continue
        i0, i1 = np.searchsorted(ns, at - window_ns), np.searchsorted(ns, at)
        same = regime[i0:i1] == r
        for s in signals:
            v = values[s][i0:i1][same]
            v = v[np.isfinite(v)]
            if v.size >= min_samples:
                rows.append({"at": int(at), "regime": r, "signal": s, "n": int(v.size),
                             "distance": drift_distance(v, refs[r][s]["quantiles"], refs[r][s]["std"])})
    return pd.DataFrame(rows, columns=["at", "regime", "signal", "n", "distance"])


def build(processed: Path, profile_path: Path) -> dict:
    profile = load_profile(profile_path)
    drift = profile.get("drift") or {}
    if not drift:
        raise SystemExit(f"{profile_path}: no drift section")
    asset = profile["asset"]["id"]
    events_path = events_file(profile, profile_path)
    events = load_events(events_path)
    signals = list(drift["signals"])
    ranges = {s["name"]: tuple(float(v) for v in s["range"]) for s in profile["signals"]["analog"]}
    edges = {k: str(events["gap_edges"][k]) for k in ("after_gaps_of", "length")}
    labelled = (json.loads((processed / "manifest.json").read_text()).get("zones") or {}).get("gap_edge") or {}
    for k, v in edges.items():
        if k in labelled and pd.Timedelta(labelled[k]) != pd.Timedelta(parse_duration(v)):
            raise SystemExit(f"{processed} was labelled with gap_edge {k} {labelled[k]}, events.yaml says {v}; "
                             "rebuild the processed folder or the reference would not match its zones")
    window = parse_duration(drift["window"])
    every = parse_duration(drift["every"])
    step = float(profile["sampling"]["step_seconds"])
    min_samples = int(MIN_SHARE * window.total_seconds() / step)
    window_ns, every_ns = int(window.total_seconds() * 10**9), int(every.total_seconds() * 10**9)

    healthy = read_healthy(processed, signals, ranges)
    regime = regime_of(pd.DatetimeIndex(healthy["timestamp"]), events)
    names = [r["name"] for r in events.get("regimes") or []]
    refs, uncalibrated = {}, {}
    for r in names:
        in_regime = regime == r
        if not in_regime.any():
            uncalibrated[r] = "no healthy points: no reference, the service skips this regime"
            continue
        refs[r] = {}
        for s in signals:
            summary = summarize(healthy[s].to_numpy(float)[in_regime], QUANTILES)
            refs[r][s] = {"n": summary["n"], "std": round(summary["std"], 6),
                          "quantiles": [round(q, 4) for q in summary["quantiles"]]}
    windows = check_windows(healthy, regime, signals, refs, {"regimes": events["regimes"]}, window_ns, every_ns,
                            min_samples)
    by_regime = {}
    for r, entries in refs.items():
        sub = windows[windows["regime"] == r]
        counts = sub.groupby("signal").size()
        short = [s for s in signals if counts.get(s, 0) < MIN_WINDOWS]
        if short:
            uncalibrated[r] = (f"fewer than {MIN_WINDOWS} healthy check windows for {', '.join(short)}: "
                               "no threshold, the service skips this regime")
            continue
        by_regime[r] = {}
        for s in signals:
            d = sub[sub["signal"] == s]
            daily = d[d["at"] % DAY_NS == 0]["distance"]
            p99_daily = float(np.percentile(daily, 99)) if len(daily) else None
            top = float(d["distance"].max())
            by_regime[r][s] = {
                **entries[s],
                "threshold": round(MARGIN * top, 4),
                "check_windows": len(d), "max_distance": round(top, 4),
                "p99_distance": round(float(np.percentile(d["distance"], 99)), 4),
                "daily_blocks": len(daily),
                "p99_daily_blocks": None if p99_daily is None else round(p99_daily, 4),
                "windows_over_p99_daily": None if p99_daily is None else int((d["distance"] > p99_daily).sum()),
            }
    rel = lambda p: str(Path(p).resolve().relative_to(ROOT)) if Path(p).resolve().is_relative_to(ROOT) else str(p)  # noqa: E731
    return {
        "schema": REFERENCE_SCHEMA,
        "asset": asset,
        "built_by": "tools/drift_reference.py",
        "built_on": dt.datetime.now(dt.timezone.utc).date().isoformat(),
        "statistic": "scipy.stats.wasserstein_distance(window values, reference quantiles) / max(reference std, "
                     "0.001): the first Wasserstein distance in healthy standard deviations, the number Evidently's "
                     "'wasserstein' stattest reports, computed directly (services/drift/drift.py drift_distance)",
        "healthy": "grid points with zone == negative and an observed value, by events.yaml regime; the gap edges "
                   "(gap_edges) are grey, so the service leaves them out of the stream too",
        "gap_edges": edges,
        "window": drift["window"], "every": drift["every"],
        "min_samples": min_samples,
        "min_samples_rule": f"{MIN_SHARE:g} x drift.window at sampling.step_seconds; fewer samples: no verdict",
        "quantiles": {"count": QUANTILES, "at": f"(k + 0.5) / {QUANTILES}, k = 0 .. {QUANTILES - 1}"},
        "threshold_rule": {"margin": MARGIN, "min_windows": MIN_WINDOWS, "text": THRESHOLD_RULE},
        "signals": signals,
        "regimes": [{"name": r["name"], "start": iso_utc(half_open(r)[0]), "end": iso_utc(half_open(r)[1])}
                    for r in events.get("regimes") or []],
        "by_regime": by_regime,
        "uncalibrated": uncalibrated,
        "sources": {
            "grid": {"path": rel(processed / "grid_10s.csv.gz"), "sha256": sha256_file(processed / "grid_10s.csv.gz")},
            "manifest": {"path": rel(processed / "manifest.json"), "sha256": sha256_file(processed / "manifest.json")},
            "profile": {"path": rel(profile_path), "sha256": sha256_file(profile_path)},
            "events": {"path": rel(events_path), "sha256": sha256_file(events_path)},
        },
    }


def to_text(reference: dict) -> str:
    """Indented JSON with every list of numbers on one line, so the file stays short and diffs read."""
    text = json.dumps(reference, indent=1, sort_keys=True)
    return re.sub(r"\[\s+([-0-9.eE,\s]+?)\s+\]", lambda m: "[" + " ".join(m.group(1).split()) + "]", text) + "\n"


def table(reference: dict) -> str:
    signals = reference["signals"]
    lines = [f"{'regime':<18}" + "".join(f"{s:>17}" for s in signals)]
    for r, entries in reference["by_regime"].items():
        lines.append(f"{r:<18}" + "".join(f"{entries[s]['threshold']:>8.4f} ({entries[s]['max_distance']:.3f})"
                                          for s in signals))
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--processed", required=True, help="processed folder holding grid_10s.csv.gz")
    p.add_argument("--profile", default=str(ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"))
    p.add_argument("--out", default=None, help="default eval/frozen/<asset>/drift_reference.json")
    p.add_argument("--check", action="store_true", help="rebuild and compare with --out; write nothing")
    args = p.parse_args(argv)
    reference = build(Path(args.processed), Path(args.profile))
    out = Path(args.out) if args.out else ROOT / "eval" / "frozen" / reference["asset"] / "drift_reference.json"
    print("threshold (largest healthy check-window distance) per regime and signal, in healthy standard deviations")
    print(table(reference))
    for r, why in reference["uncalibrated"].items():
        print(f"{r}: {why}")
    if args.check:
        if not out.exists():
            print(f"{out} does not exist")
            return 1
        old = json.loads(out.read_text())
        keys = ("by_regime", "gap_edges", "min_samples", "regimes", "signals", "window", "every", "uncalibrated")
        changed = [k for k in keys if old.get(k) != json.loads(to_text(reference)).get(k)]
        if changed:
            print(f"{out} differs from a rebuild in: {', '.join(changed)}")
            return 1
        print(f"{out} matches a rebuild")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_text(reference))
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
