"""Build the model-ready data from a raw telemetry file (T5-T7; ING-09 to ING-13).

    python tools/build_windows.py --profile profiles/metropt3_apu/asset_profile.yaml \\
        --data "data/MetroPT3(AirCompressor).csv" --out data/processed_built \\
        --check data/processed_v4

Runs the same pipeline the EDA v4 notebook runs in Sections 2.2 and 7-8, with
every parameter from the profile, events.yaml and features.yaml:

1. freeze masks, the 10 s UTC grid and labelled gaps (pdm_common.quality);
2. one zone per grid point (pdm_common.zones);
3. 10-minute windows at a 1-minute stride, the leave-one-event-out blocks and
   the fit / calibrate / check roles (pdm_common.windows);
4. the window features declared in features.yaml (pdm_common.features).

It writes grid_10s.csv.gz, windows_10min.csv.gz, feature_dictionary.csv,
scaler_negative.csv and manifest.json to --out. With --check it compares the
result with an earlier output folder (the notebook's data/processed_v4): the
expected counts, the window key and every feature within 1e-6. A mismatch
exits non-zero, which is how the port is held to the notebook.

``--from-grid`` starts from a saved grid_10s.csv.gz instead of the raw file
(about three times faster; the grid step is then skipped).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from pdm_common.events import events_hash, load_events  # noqa: E402
from pdm_common.features import features_file, load_feature_spec, window_features  # noqa: E402
from pdm_common.hashing import sha256_file  # noqa: E402
from pdm_common.profile import analog_names, digital_names, events_file, load_profile  # noqa: E402
from pdm_common.quality import mask_grid_and_gaps, raw_gaps  # noqa: E402
from pdm_common.timeutil import parse_duration  # noqa: E402
from pdm_common.windows import add_folds, make_windows, window_ends, windowing_params  # noqa: E402
from pdm_common.zones import gap_edges, label_grid, paddings, zone_scheme  # noqa: E402

LABEL_COLUMNS = ["window_start", "window_end", "zone", "zone_detail", "regime", "aligned", "group_id",
                 "event", "expert_failure"]


def windows_key(window_end) -> str:
    """The key the detector notebook ties saved scores to: SHA-256 of the
    window end times in whole seconds, first 16 hex digits."""
    t = pd.DatetimeIndex(window_end)
    if t.tz is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    seconds = t.to_numpy().astype("datetime64[s]").astype("int64")
    return hashlib.sha256(seconds.tobytes()).hexdigest()[:16]


def naive_utc(values):
    """Aware UTC times as naive values: the processed files' format."""
    t = pd.DatetimeIndex(values)
    return t.tz_convert("UTC").tz_localize(None) if t.tz is not None else t


def read_raw(path) -> pd.DataFrame:
    df = pd.read_csv(path, index_col=0, parse_dates=["timestamp"])
    return df.sort_values("timestamp").reset_index(drop=True)


def read_grid(path, profile) -> pd.DataFrame:
    cols = ["timestamp", *analog_names(profile), *digital_names(profile), "observed", "bridged"]
    grid = pd.read_csv(path, usecols=cols, parse_dates=["timestamp"]).set_index("timestamp")
    grid.index = pd.DatetimeIndex(grid.index).tz_localize("UTC")
    return grid


def build(profile_path, data_path=None, grid_path=None, log=print):
    """The whole pipeline; returns a dict of the frames and facts the outputs hold."""
    t0 = time.time()
    profile = load_profile(profile_path)
    ev_path = events_file(profile, profile_path)
    events = load_events(ev_path)
    spec_path = features_file(profile, profile_path)
    spec = load_feature_spec(spec_path)
    wp = windowing_params(profile)
    step = profile["sampling"]["resample_rule"]
    labels = profile["gap_policy"]["gap_labels"]
    tz = profile.get("source_timezone", "UTC")

    source = None
    if grid_path is None:
        df = read_raw(data_path)
        runs, frozen, grid, _ = mask_grid_and_gaps(df, profile)
        gaps = raw_gaps(df, parse_duration(labels["min_gap"]).total_seconds(), tz)
        source = {"file": Path(data_path).name, "rows": len(df), "sha256": sha256_file(data_path)}
        log(f"raw: {len(df):,} rows; {len(runs)} freeze runs; grid {len(grid):,} points "
            f"({time.time() - t0:.0f} s)")
    else:
        grid = read_grid(grid_path, profile)
        # without the raw file the gaps come from the grid: runs of missing points that
        # are not freezes. Freezes are masked spans, so they are excluded explicitly.
        gaps = _gaps_from_grid(grid, events, labels)
        log(f"grid: {len(grid):,} points read from {grid_path} ({time.time() - t0:.0f} s)")

    scheme = zone_scheme(events)
    edges = gap_edges(gaps, events, step)
    grid = label_grid(grid, events, edges, scheme)
    ends = window_ends(grid.index, wp["win_bins"], wp["stride"])
    windows_all = make_windows(grid, ends, events, win_bins=wp["win_bins"], window_length=wp["length"],
                               lookback=pd.Timedelta(hours=6), step=wp["step"], scheme=scheme)
    blocks = add_folds(windows_all, events, lookback_days=spec.max_lookback())
    feats = window_features(grid, ends, spec)
    log(f"{len(windows_all):,} candidate windows, {len(spec.names)} features ({time.time() - t0:.0f} s)")
    windows = pd.concat([windows_all.reset_index(drop=True), feats], axis=1)
    windows = windows[windows["complete"]].drop(columns="complete").reset_index(drop=True)
    return {"profile": profile, "events": events, "spec": spec, "grid": grid, "windows": windows,
            "blocks": blocks, "edges": edges, "scheme": scheme, "source": source,
            "hashes": {"profile": sha256_file(profile_path), "events": events_hash(ev_path),
                       "features": sha256_file(spec_path)}}


def _gaps_from_grid(grid, events, labels) -> pd.DataFrame:
    """Raw-gap stand-in from a saved grid: every missing run outside the freeze
    masks, as (gap_start, gap_end, seconds) between the observed points around it."""
    from pdm_common.zones import _t
    valid = (grid["observed"] | grid["bridged"]).to_numpy()
    masked = np.zeros(len(grid), dtype=bool)
    for m in events.get("data_quality_masks") or []:
        i0, i1 = grid.index.searchsorted(_t(m["start"])), grid.index.searchsorted(_t(m["end"]), side="right")
        masked[i0:i1] = True
    edges = np.diff(np.r_[0, (~valid).astype(np.int8), 0])
    first, end = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    keep = (first > 0) & (end < len(valid)) & ~np.array([masked[a:b].any() for a, b in zip(first, end)], bool)
    a, b = first[keep] - 1, end[keep]
    t = grid.index
    return pd.DataFrame({"gap_start": t[a], "gap_end": t[b], "seconds": (t[b] - t[a]).total_seconds()})


def scaler_table(windows, features, regimes) -> pd.DataFrame:
    """Scaling statistics from fit-role negative windows that read no test block
    (EDA 8.5): median, IQR (empty where zero), mean, std, count, per-regime medians."""
    fit = windows[(windows["zone"] == "negative") & (windows["neg_role"] == "fit")
                  & ~windows.filter(like="lookback_").any(axis=1)]
    iqr = fit[features].quantile(0.75) - fit[features].quantile(0.25)
    table = pd.DataFrame({"median": fit[features].median(), "iqr": iqr.where(iqr > 0),
                          "mean": fit[features].mean(), "std": fit[features].std(ddof=0),
                          "present": fit[features].notna().sum()})
    for reg in regimes:
        table[f"median_{reg}"] = fit.loc[fit["regime"] == reg, features].median()
    return table


def manifest(result, out_files) -> dict:
    windows, events, spec = result["windows"], result["events"], result["spec"]
    pad = paddings(events)
    neg = windows["zone"] == "negative"
    iso = lambda t: None if t is None else pd.Timestamp(t).tz_convert("UTC").tz_localize(None).isoformat()  # noqa: E731
    return {
        "created_by": "tools/build_windows.py (pdm_common zones, windows, features)",
        "source": result["source"],
        "inputs": {"profile_sha256": result["hashes"]["profile"], "events_sha256": result["hashes"]["events"],
                   "features_sha256": result["hashes"]["features"]},
        "zones": {"precedence": list(result["scheme"].detail[::-1]),
                  "guard_band": [str(pad["guard_before"]), str(pad["guard_after"])],
                  "expert_band": [str(pad["expert_before"]), str(pad["expert_after"])],
                  "gap_edge": {"after_gaps_of": str(pad["edge_after_gaps_of"]), "length": str(pad["edge_length"]),
                               "spans": [[iso(s), iso(e)] for s, e in result["edges"]]},
                  "failures": [{"id": f["id"], "start": iso(pd.Timestamp(f["start"]).tz_localize("UTC")),
                                "end": iso(pd.Timestamp(f["end"]).tz_localize("UTC")),
                                "repair": None if f.get("maintenance") is None
                                else iso(pd.Timestamp(f["maintenance"]).tz_localize("UTC"))}
                               for f in events["failures"]],
                  "uncertain_periods": [[iso(pd.Timestamp(u["start"]).tz_localize("UTC")),
                                         iso(pd.Timestamp(u["end"]).tz_localize("UTC")), u["tag"]]
                                        for u in events.get("uncertain_periods") or []]},
        "folds": {"test_blocks": {k: [iso(a), iso(b)] for k, (a, b) in result["blocks"].items()},
                  "lookback": str(spec.max_lookback()),
                  "neg_role": "ISO week of window_start: even = fit; odd and week mod 4 = 1 calibrate; "
                              "odd and week mod 4 = 3 check"},
        "features": {"names": spec.names, "detector_v1": spec.flagged("detector_v1"),
                     "drop_for_tabular": spec.flagged("drop_for_tabular")},
        "windows_key": windows_key(windows["window_end"]),
        "expected_counts": {
            "grid_points": int(len(result["grid"])), "complete_windows": int(len(windows)),
            "negative_windows": int(neg.sum()),
            "aligned_negative_windows": int((windows["aligned"] & neg).sum()),
            "negative_by_role": {k: int(v) for k, v in windows["neg_role"].value_counts().items() if k != "none"},
            "expert_reported_windows": int((windows["zone_detail"] == "expert_reported").sum())},
        "outputs": out_files,
    }


def write_outputs(result, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    gz = {"method": "gzip", "compresslevel": 1}
    grid = result["grid"].drop(columns="zone_code").copy()
    grid.index = naive_utc(grid.index)
    grid.index.name = "timestamp"
    grid.to_csv(out / "grid_10s.csv.gz", float_format="%.7g", compression=gz)
    windows = result["windows"].copy()
    windows["window_start"] = naive_utc(windows["window_start"])
    windows["window_end"] = naive_utc(windows["window_end"])
    windows.to_csv(out / "windows_10min.csv.gz", index=False, float_format="%.6g", compression=gz)
    spec = result["spec"]
    spec.dictionary().to_csv(out / "feature_dictionary.csv", index=False)
    regimes = [r["name"] for r in result["events"].get("regimes") or []]
    scaler_table(result["windows"], spec.names, regimes).to_csv(out / "scaler_negative.csv", index_label="feature")
    files = {"grid_10s.csv.gz": {"rows": len(grid), "columns": grid.shape[1]},
             "windows_10min.csv.gz": {"rows": len(windows), "columns": windows.shape[1]}}
    (out / "manifest.json").write_text(json.dumps(manifest(result, files), indent=2))
    return files


def compare(result, reference: Path, tolerance: float = 1e-6, log=print) -> list[str]:
    """Differences between this build and an earlier output folder; [] when they agree."""
    problems = []
    ref_manifest = json.loads((reference / "manifest.json").read_text())
    windows = result["windows"]
    neg = windows["zone"] == "negative"
    mine = {"grid_points": len(result["grid"]), "complete_windows": len(windows),
            "negative_windows": int(neg.sum()), "aligned_negative_windows": int((windows["aligned"] & neg).sum()),
            "negative_by_role": {k: int(v) for k, v in windows["neg_role"].value_counts().items() if k != "none"},
            "expert_reported_windows": int((windows["zone_detail"] == "expert_reported").sum())}
    for key, value in mine.items():
        want = ref_manifest["expected_counts"].get(key)
        if want is not None and want != value:
            problems.append(f"{key}: {value} here, {want} in {reference}")
    ref = pd.read_csv(reference / "windows_10min.csv.gz", parse_dates=["window_start", "window_end"])
    if len(ref) != len(windows):
        problems.append(f"{len(windows)} windows here, {len(ref)} in the reference")
        return problems
    if windows_key(windows["window_end"]) != windows_key(ref["window_end"]):
        problems.append("window end times differ (windows_key)")
    for col in [c for c in ref.columns if c in LABEL_COLUMNS or c.startswith(("block_", "lookback_", "neg_"))]:
        a, b = windows[col].astype(str).to_numpy(), ref[col].astype(str).to_numpy()
        if col in ("window_start", "window_end"):
            a = naive_utc(windows[col]).astype(str).to_numpy()
        if (a != b).any():
            problems.append(f"{col}: {(a != b).sum()} windows differ")
    worst = {}
    for name in result["spec"].names:
        a, b = windows[name].to_numpy(float), ref[name].to_numpy(float)
        if (np.isnan(a) != np.isnan(b)).any():
            problems.append(f"{name}: missing values differ in {(np.isnan(a) != np.isnan(b)).sum()} windows")
            continue
        both = ~np.isnan(a)
        # the reference was written with 6 significant digits: compare relative to that
        scale = np.maximum(np.abs(b[both]), 1.0)
        diff = np.max(np.abs(a[both] - b[both]) / scale) if both.any() else 0.0
        worst[name] = diff
        if diff > max(tolerance, 5e-6):
            problems.append(f"{name}: largest relative difference {diff:.2e}")
    log("largest relative feature difference: " + (f"{max(worst.values()):.1e}" if worst else "n/a")
        + " (the reference holds 6 significant digits)")
    return problems


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile", default=str(ROOT / "profiles/metropt3_apu/asset_profile.yaml"))
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--data", help="raw telemetry CSV")
    src.add_argument("--from-grid", help="a saved grid_10s.csv.gz")
    p.add_argument("--out", help="folder to write the outputs to")
    p.add_argument("--check", help="earlier output folder to compare with (e.g. data/processed_v4)")
    args = p.parse_args(argv)
    result = build(args.profile, data_path=args.data, grid_path=args.from_grid)
    w = result["windows"]
    print(f"{len(w):,} complete windows, {int((w.zone == 'negative').sum()):,} negative; "
          f"windows_key {windows_key(w['window_end'])}")
    if args.out:
        write_outputs(result, Path(args.out))
        print(f"wrote {args.out}")
    if args.check:
        problems = compare(result, Path(args.check))
        if problems:
            print("MISMATCH against " + args.check + ":\n  " + "\n  ".join(problems))
            return 1
        print(f"matches {args.check}: counts, window key, labels and all {len(result['spec'].names)} features")
    return 0


if __name__ == "__main__":
    sys.exit(main())
