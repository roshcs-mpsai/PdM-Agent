"""Deterministic stream replayer -- telemetry CSV to MQTT (PR-01).

Reads an asset's telemetry CSV, validates its columns against the asset
profile, and publishes one JSON message per row to the profile's telemetry
topic at a configurable multiple of recorded time (1x = real time).

Replay is deterministic: rows are published in file order and every payload
is built only from file contents, never from the wall clock, so two replays
of the same file and arguments produce identical message sequences. Verify
with --log (one "<topic> <payload>" line per message): the logs are
byte-identical across runs. There is no random element, hence no seed.

Asset facts -- signal names, topic namespace, payload schema id -- come from
the asset profile (PR-04). Nothing in this file names an asset-specific
signal. The payload schema is documented in services/replayer/README.md and
validated by tests/test_replayer.py.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml


def load_profile(path: str | Path) -> dict:
    """Load the asset profile and check the keys the replayer depends on."""
    with open(path) as f:
        profile = yaml.safe_load(f)
    try:
        profile["asset"]["id"]
        profile["topics"]["telemetry"]
        profile["topics"]["payload_schema"]
        profile["signals"]["analog"]
        profile["signals"]["digital"]
    except (KeyError, TypeError) as exc:
        raise SystemExit(f"{path}: asset profile is missing required key: {exc}")
    return profile


def signal_names(profile: dict) -> list[str]:
    """All signal names, analog then digital, in profile order."""
    return [s["name"] for s in profile["signals"]["analog"]] + [
        s["name"] for s in profile["signals"]["digital"]
    ]


def check_header(fieldnames: list[str] | None, signals: list[str], source: str) -> None:
    header = fieldnames or []
    missing = [c for c in ["timestamp", *signals] if c not in header]
    if missing:
        raise SystemExit(
            f"{source}: missing expected column(s): {', '.join(missing)}. "
            "The asset profile drives the schema; check the file against it."
        )


def build_message(profile: dict, seq: int, ts: datetime, values: dict) -> tuple[str, str]:
    """One row -> (topic, canonical JSON payload). Pure function; no clock."""
    payload = json.dumps(
        {
            "schema": profile["topics"]["payload_schema"],
            "asset": profile["asset"]["id"],
            "seq": seq,
            "ts": ts.isoformat(),
            "signals": values,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return profile["topics"]["telemetry"], payload


def step_wait(prev_ts: datetime | None, ts: datetime, speed: float, max_wait: float) -> float:
    """Wall seconds to wait before publishing this row.

    Recorded gaps are honoured at 1/speed, but never beyond max_wait wall
    seconds per step, so day-scale recording gaps (the dataset has them) do
    not stall the replay. Out-of-order timestamps wait zero.
    """
    if prev_ts is None:
        return 0.0
    delta = (ts - prev_ts).total_seconds()
    if delta <= 0:
        return 0.0
    return min(delta / speed, max_wait)


def replay(
    profile: dict,
    data_path: str | Path,
    publish,
    *,
    speed: float = 1.0,
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
    limit: int | None = None,
    max_wait: float = 60.0,
    log_path: str | Path | None = None,
    progress_every: int = 0,
) -> dict:
    """Replay the file. `publish` is a callable(topic, payload) or None.

    Returns counters: rows read, published, skipped (unparseable), filtered.
    """
    signals = signal_names(profile)
    counts = {"read": 0, "published": 0, "skipped": 0, "filtered": 0}
    log = open(log_path, "w") if log_path else None
    prev_ts: datetime | None = None
    try:
        with open(data_path, newline="") as f:
            reader = csv.DictReader(f)
            check_header(reader.fieldnames, signals, str(data_path))
            for lineno, row in enumerate(reader, start=2):
                counts["read"] += 1
                try:
                    ts = datetime.fromisoformat(row["timestamp"])
                    values = {s: float(row[s]) for s in signals}
                except (ValueError, TypeError):
                    counts["skipped"] += 1
                    print(f"skipping unparseable row at line {lineno}", file=sys.stderr)
                    continue
                if (from_ts and ts < from_ts) or (to_ts and ts > to_ts):
                    counts["filtered"] += 1
                    continue
                wait = step_wait(prev_ts, ts, speed, max_wait)
                if wait > 0:
                    time.sleep(wait)
                prev_ts = ts
                topic, payload = build_message(profile, counts["published"], ts, values)
                if publish is not None:
                    publish(topic, payload)
                if log:
                    log.write(f"{topic} {payload}\n")
                counts["published"] += 1
                if progress_every and counts["published"] % progress_every == 0:
                    print(f"published {counts['published']} messages "
                          f"(through {ts.isoformat()})", file=sys.stderr)
                if limit and counts["published"] >= limit:
                    break
    finally:
        if log:
            log.close()
    return counts


def make_mqtt_publisher(host: str, port: int, qos: int):
    """Connect to the broker; return (publish, close)."""
    import paho.mqtt.client as mqtt

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="pdm-replayer",
    )
    client.connect(host, port)
    client.loop_start()

    def publish(topic: str, payload: str) -> None:
        info = client.publish(topic, payload, qos=qos)
        if qos > 0:
            info.wait_for_publish(timeout=10)

    def close() -> None:
        client.loop_stop()
        client.disconnect()

    return publish, close


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile", required=True, help="asset profile YAML (PR-04)")
    p.add_argument("--data", required=True, help="telemetry CSV to replay")
    p.add_argument("--broker", default="localhost", help="MQTT broker host")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--qos", type=int, default=1, choices=[0, 1, 2])
    p.add_argument("--speed", type=float, default=1.0,
                   help="replay-rate multiple of recorded time (PR-01: 1-10)")
    p.add_argument("--from", dest="from_ts", type=datetime.fromisoformat, default=None,
                   metavar="ISO", help="replay rows at or after this timestamp")
    p.add_argument("--to", dest="to_ts", type=datetime.fromisoformat, default=None,
                   metavar="ISO", help="replay rows at or before this timestamp")
    p.add_argument("--limit", type=int, default=None, help="stop after N messages")
    p.add_argument("--max-wait", type=float, default=60.0,
                   help="cap on wall seconds waited per step (bridges recording gaps)")
    p.add_argument("--log", default=None, help="write '<topic> <payload>' lines here")
    p.add_argument("--dry-run", action="store_true",
                   help="no broker: read, pace and log only")
    p.add_argument("--progress-every", type=int, default=1000,
                   help="stderr progress line every N messages (0 = off)")
    args = p.parse_args(argv)
    if args.speed <= 0:
        p.error("--speed must be positive")
    return args


def main(argv=None) -> None:
    args = parse_args(argv)
    profile = load_profile(args.profile)
    publish = close = None
    if not args.dry_run:
        publish, close = make_mqtt_publisher(args.broker, args.port, args.qos)
    started = time.monotonic()
    try:
        counts = replay(
            profile,
            args.data,
            publish,
            speed=args.speed,
            from_ts=args.from_ts,
            to_ts=args.to_ts,
            limit=args.limit,
            max_wait=args.max_wait,
            log_path=args.log,
            progress_every=args.progress_every,
        )
    finally:
        if close:
            close()
    elapsed = time.monotonic() - started
    rate = counts["published"] / elapsed if elapsed > 0 else float("inf")
    print(
        f"replayed {counts['published']} of {counts['read']} rows "
        f"({counts['filtered']} outside --from/--to, {counts['skipped']} unparseable) "
        f"in {elapsed:.1f}s ({rate:.1f} msg/s) at {args.speed}x"
        + (" [dry run]" if args.dry_run else f" to {args.broker}:{args.port}")
    )


if __name__ == "__main__":
    main()
