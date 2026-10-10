"""Replay a telemetry file through the streaming windows and compare with a window table (T3; ING-11, ING-12).

    python tools/stream_check.py --data "data/MetroPT3(AirCompressor).csv" --check data/processed_v4

Feeds every row, in file order, through pdm_common.stream.StreamWindows (the
code the ingest service runs) and compares the complete windows with the
batch window table in --check (tools/build_windows.py or the EDA v4
notebook's output): the same window starts, and every feature within
--tolerance. This is the evidence that the stream scores what the harness
evaluated. ``--every`` sets how many rows go in between computations; the
result does not depend on it (tests/test_stream.py), only the run time does.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from pdm_common.features import features_file, load_feature_spec  # noqa: E402
from pdm_common.profile import analog_names, digital_names, load_profile  # noqa: E402
from pdm_common.stream import StreamWindows  # noqa: E402


def run(profile_path, data_path, every: int, log=print):
    profile = load_profile(profile_path)
    spec = load_feature_spec(features_file(profile, profile_path))
    df = pd.read_csv(data_path, index_col=0, parse_dates=["timestamp"]).sort_values("timestamp")
    cols = analog_names(profile) + digital_names(profile)
    values = df[cols].to_numpy(float)
    ts = pd.DatetimeIndex(df["timestamp"])
    sw = StreamWindows(profile, spec)
    complete, starts = [], []
    t0 = time.time()
    for i in range(len(df)):
        sw.append(ts[i], dict(zip(cols, values[i])))
        if i % every == every - 1:
            for w in sw.compute():
                if w.complete:
                    starts.append(w.start)
                    complete.append(w.features)
        if i % 200_000 == 0 and i:
            log(f"{i:,} rows, {len(starts):,} complete windows, buffer {sw.buffered:,} ({time.time() - t0:.0f} s)")
    for w in sw.flush():
        if w.complete:
            starts.append(w.start)
            complete.append(w.features)
    table = pd.DataFrame(complete, index=pd.DatetimeIndex(starts).tz_convert("UTC").tz_localize(None))
    table.index.name = "window_start"
    return table, sw.counts, time.time() - t0


def compare(stream: pd.DataFrame, reference: pd.DataFrame, tolerance: float) -> dict:
    ref = reference.set_index("window_start")[list(stream.columns)]
    same_set = stream.index.equals(ref.index)
    common = stream.index.intersection(ref.index)
    a, b = stream.loc[common], ref.loc[common]
    nan_mismatch = int((a.isna() != b.isna()).sum().sum())
    # the saved tables hold six significant digits, so a difference up to 5e-6
    # relative is the reference's rounding (the same rule as build_windows --check)
    rel = ((a - b).abs() / b.abs().clip(lower=1)).max()
    worst = rel.sort_values(ascending=False).head(3)
    limit = max(tolerance, 5e-6)
    return {"stream_windows": len(stream), "reference_windows": len(ref), "same_window_set": bool(same_set),
            "only_stream": len(stream.index.difference(ref.index)), "only_reference": len(ref.index.difference(stream.index)),
            "nan_mismatches": nan_mismatch, "max_relative_difference": {k: float(v) for k, v in worst.items()},
            "limit": limit,
            "within_tolerance": bool(same_set and nan_mismatch == 0 and (rel.fillna(0) <= limit).all())}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile", default=str(ROOT / "profiles/metropt3_apu/asset_profile.yaml"))
    p.add_argument("--data", required=True)
    p.add_argument("--check", required=True, help="folder holding windows_10min.csv.gz")
    p.add_argument("--every", type=int, default=2160, help="rows between computations (2160 = 6 h)")
    p.add_argument("--tolerance", type=float, default=1e-6)
    p.add_argument("--out", help="write the result here (JSON)")
    args = p.parse_args(argv)
    stream, counts, seconds = run(args.profile, args.data, args.every)
    reference = pd.read_csv(Path(args.check) / "windows_10min.csv.gz", parse_dates=["window_start"])
    result = compare(stream, reference, args.tolerance)
    result.update({"candidate_windows": counts["windows"], "dropped_incomplete": counts["dropped"],
                   "seconds": round(seconds, 1), "every": args.every})
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n")
    return 0 if result["within_tolerance"] else 1


if __name__ == "__main__":
    sys.exit(main())
