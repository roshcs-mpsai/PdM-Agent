"""Database benchmark: load a month of telemetry, join it to incidents (T3 step 4, the PRD's Week 5 decision).

    python tools/db_benchmark.py --store "postgresql://pdm:$PG_PASSWORD@localhost:5432/pdm"
    python tools/db_benchmark.py --store sqlite:///runs/bench.db --out runs/db_benchmark/sqlite.json

Loads one month of the telemetry CSV into the telemetry table through
``Store.insert_many`` in batches (the path ingest writes through), then times
one join: every incident's telemetry over the lookback before it opened, as
the evidence tools read it, with one signal averaged out of the JSON. The
criterion from the build guide: both the load and the join under a minute.

The benchmark writes under its own asset id (``benchmark`` by default) and
deletes that asset's telemetry and incidents before loading, so it can run
against the deployed database without touching real records.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from pdm_common.profile import analog_names, load_profile, signal_names  # noqa: E402
from pdm_common.store import Store  # noqa: E402
from pdm_common.timeutil import iso_utc, parse_duration, to_utc  # noqa: E402

LIMIT_S = 60.0


def read_month(data: Path, signals: list[str], start: str, end: str) -> pd.DataFrame:
    df = pd.read_csv(data, usecols=["timestamp", *signals])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df[(df["timestamp"] >= pd.Timestamp(start, tz="UTC")) & (df["timestamp"] < pd.Timestamp(end, tz="UTC"))]


def telemetry_rows(df: pd.DataFrame, signals: list[str], asset: str) -> list[dict]:
    stamps = df["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ").tolist()
    values = df[signals].astype(float).to_dict("records")
    return [{"asset_id": asset, "ts": ts, "seq": i, "signals": v} for i, (ts, v) in enumerate(zip(stamps, values))]


def join_sql(dialect: str, lookback_s: int) -> str:
    if dialect == "postgres":
        lower = f"i.opened_at - interval '{lookback_s} seconds'"
        value = "(t.signals ->> ?)::float"
    else:
        # stored as fixed-width text: shift the whole seconds, keep the fraction
        lower = f"strftime('%Y-%m-%dT%H:%M:%S', i.opened_at, '-{lookback_s} seconds') || substr(i.opened_at, 20)"
        value = "json_extract(t.signals, '$.' || ?)"
    return ("SELECT i.incident_id, COUNT(t.ts) AS n, AVG(" + value + ") AS mean_value "
            "FROM incidents i JOIN telemetry t ON t.asset_id = i.asset_id "
            f"AND t.ts >= {lower} AND t.ts <= i.opened_at "
            "WHERE i.asset_id = ? GROUP BY i.incident_id ORDER BY i.incident_id")


def run(store: Store, rows: list[dict], *, asset: str, signal: str, incidents: int, lookback: str,
        batch: int) -> dict:
    store.execute("DELETE FROM telemetry WHERE asset_id = ?", [asset])
    store.execute("DELETE FROM incidents WHERE asset_id = ?", [asset])

    t0 = time.perf_counter()
    for i in range(0, len(rows), batch):
        store.insert_many("telemetry", rows[i:i + batch])
    load_s = time.perf_counter() - t0

    first, last = to_utc(rows[0]["ts"]), to_utc(rows[-1]["ts"])
    step = (last - first) / (incidents + 1)
    opened = [first + step * (k + 1) for k in range(incidents)]
    store.insert_many("incidents", [{"incident_id": f"BENCH-{k:04d}", "asset_id": asset, "opened_at": iso_utc(t),
                                     "state": "open"} for k, t in enumerate(opened)])

    lookback_s = int(parse_duration(lookback).total_seconds())
    t0 = time.perf_counter()
    joined = store.query(join_sql(store.dialect, lookback_s), [signal, asset])
    join_s = time.perf_counter() - t0

    # the evidence tools read one incident at a time
    per_incident = []
    for t in opened[:20]:
        t0 = time.perf_counter()
        store.between("telemetry", "ts", asset, t - parse_duration(lookback), t)
        per_incident.append((time.perf_counter() - t0) * 1000)

    timescale = None
    if store.dialect == "postgres":
        timescale = bool(store.query("SELECT 1 AS x FROM pg_extension WHERE extname = 'timescaledb'"))
    return {
        "engine": store.dialect, "timescaledb": timescale, "rows": len(rows),
        "from": rows[0]["ts"], "to": rows[-1]["ts"], "batch": batch,
        "load_s": round(load_s, 2), "rows_per_s": round(len(rows) / load_s) if load_s else None,
        "incidents": incidents, "lookback": lookback, "join_s": round(join_s, 3),
        "joined_rows": int(sum(r["n"] for r in joined)),
        "incidents_with_rows": sum(1 for r in joined if r["n"]),
        "evidence_query_ms_median": round(statistics.median(per_incident), 2),
        "under_a_minute": load_s < LIMIT_S and join_s < LIMIT_S,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--store", required=True, help="sqlite:///path or postgresql://...")
    p.add_argument("--profile", default=str(ROOT / "profiles/metropt3_apu/asset_profile.yaml"))
    p.add_argument("--data", default=str(ROOT / "data/MetroPT3(AirCompressor).csv"))
    p.add_argument("--start", default="2020-04-01")
    p.add_argument("--end", default="2020-05-01")
    p.add_argument("--asset-id", default="benchmark")
    p.add_argument("--signal", help="the signal averaged in the join (default: the profile's first analog signal)")
    p.add_argument("--incidents", type=int, default=100)
    p.add_argument("--lookback", default="2h")
    p.add_argument("--batch", type=int, default=5000)
    p.add_argument("--out", help="also write the result here (JSON)")
    args = p.parse_args(argv)

    profile = load_profile(args.profile)
    signals = signal_names(profile)
    df = read_month(Path(args.data), signals, args.start, args.end)
    if df.empty:
        raise SystemExit(f"no telemetry between {args.start} and {args.end} in {args.data}")
    rows = telemetry_rows(df, signals, args.asset_id)
    store = Store(args.store)
    store.init_schema()
    try:
        result = run(store, rows, asset=args.asset_id, signal=args.signal or analog_names(profile)[0],
                     incidents=args.incidents, lookback=args.lookback, batch=args.batch)
    finally:
        store.close()
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n")
    return 0 if result["under_a_minute"] else 1


if __name__ == "__main__":
    sys.exit(main())
