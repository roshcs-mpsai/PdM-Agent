"""Recompute the check thresholds from healthy operation (T15b; AGT-05).

    python tools/calibrate_checks.py --processed data/processed_v4            # print, compare with the profile
    python tools/calibrate_checks.py --processed data/processed_v4 --check    # exit 1 if the profile disagrees

Every statistical threshold in the profile's consistency_checks comes from
here, from healthy data only, never from an event:

* C4, C5, C6, C7: the 99th percentile over the fit windows (the harness's
  fit role: negative windows of even ISO weeks that read no test block) of
  hours_since_rest, load_share_6h, Oil_temperature_mean and lps_minutes_24h;
* C8: the 99th percentile of panel pressure lost per hour across healthy gaps
  (negative data on both sides, the far side past the restart edge);
* C11: the 10th percentile of the refill rate over the same healthy restarts
  where the motor loaded long enough to measure it.

C1-C3, C9 and C10 are rules with fixed values in the profile. The method is
notebooks/detector_selection_v7.2.ipynb Section 8.2 (``LIMIT``) and 8.7
(``REFILL_LIMIT``).
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "eval"))

from pdm_common.checks import Evidence, load_registry  # noqa: E402
from pdm_common.events import load_events  # noqa: E402
from pdm_common.profile import analog_names, digital_names, events_file, load_profile  # noqa: E402

FEATURE_THRESHOLDS = {"C4": "hours_since_rest", "C5": "load_share_6h", "C6": "Oil_temperature_mean",
                      "C7": "lps_minutes_24h"}


def read_grid(processed: Path, profile: dict) -> pd.DataFrame:
    cols = ["timestamp", *analog_names(profile), *digital_names(profile), "observed", "bridged", "zone"]
    head = pd.read_csv(processed / "grid_10s.csv.gz", nrows=1)
    grid = pd.read_csv(processed / "grid_10s.csv.gz", usecols=[c for c in cols if c in head.columns],
                       parse_dates=["timestamp"]).set_index("timestamp")
    grid.index = pd.DatetimeIndex(grid.index).tz_localize("UTC")
    return grid


def fit_windows(processed: Path) -> pd.DataFrame:
    harness = importlib.import_module("harness")
    data = harness.load(processed)
    roles = harness.assign_roles(data.windows)
    return data.windows[roles == "fit"]


def healthy_gaps(ev: Evidence, grid: pd.DataFrame):
    """Gaps with negative data on both sides, the far side checked past the restart edge and one more minute."""
    zone = grid["zone"].to_numpy()
    out = []
    for g in ev.gaps:
        past_edge = min(g.after_pos + int((ev.restart_edge + ev.step * 6) / ev.step), len(zone) - 1)
        if zone[g.before_pos] == "negative" and zone[past_edge] == "negative":
            out.append(g)
    return out


def calibrate(processed: Path, profile_path: Path, root: Path = ROOT) -> dict:
    profile = load_profile(profile_path)
    events = load_events(events_file(profile, profile_path))
    registry = load_registry(profile, root)
    checks = registry.module
    fit = fit_windows(processed)
    out = {cid: {"threshold": float(np.nanpercentile(fit[feature], 99)), "from": f"99th percentile of {feature} "
                 f"over {int(fit[feature].notna().sum()):,} fit windows"} for cid, feature in FEATURE_THRESHOLDS.items()}
    grid = read_grid(processed, profile)
    ev = Evidence(grid.index[-1] + pd.Timedelta(seconds=10), grid, {}, profile=profile, events=events)
    healthy = healthy_gaps(ev, grid)
    loss = np.array([checks.leak_rate_across(ev, g) for g in healthy])
    loss = loss[np.isfinite(loss)]
    out["C8"] = {"threshold": float(np.percentile(loss, 99)),
                 "from": f"99th percentile over {len(loss)} healthy gaps (of {len(ev.gaps)})"}
    refill = np.array([checks.restart_refill_rate(ev, g.after_pos, until=ev.opens) for g in healthy])
    refill = refill[np.isfinite(refill)]
    out["C11"] = {"threshold": float(np.percentile(refill, 10)),
                  "from": f"10th percentile over {len(refill)} healthy restarts with enough loaded refill "
                          f"(median {np.median(refill):.3f})"}
    return out


def compare(computed: dict, profile: dict, rel: float = 2e-3) -> list[str]:
    declared = {c["id"]: c.get("threshold") for c in profile.get("consistency_checks") or []}
    problems = []
    for cid, c in computed.items():
        have = declared.get(cid)
        if have is None or abs(have - c["threshold"]) > rel * max(abs(c["threshold"]), 1e-9):
            problems.append(f"{cid}: profile {have}, computed {c['threshold']:.4g}")
    return problems


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--processed", required=True, help="a processed folder (grid_10s.csv.gz with zones, windows)")
    p.add_argument("--profile", default=str(ROOT / "profiles/metropt3_apu/asset_profile.yaml"))
    p.add_argument("--check", action="store_true", help="exit 1 when the profile's thresholds differ")
    args = p.parse_args(argv)
    computed = calibrate(Path(args.processed), Path(args.profile))
    print(json.dumps({k: {"threshold": round(v["threshold"], 4), "from": v["from"]} for k, v in computed.items()},
                     indent=2))
    problems = compare(computed, load_profile(args.profile))
    if problems:
        print("profile thresholds differ:\n  " + "\n  ".join(problems))
        return 1 if args.check else 0
    print("the profile's thresholds match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
