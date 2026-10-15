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
the asset profile (PR-04), loaded through pdm_common. Nothing in this file
names an asset-specific signal. The payload schema is documented in
services/replayer/README.md and validated by tests/test_replayer.py.

Every run can write a manifest (--manifest, ING-05): the data file's SHA-256,
the profile and events hashes, the arguments, the message count and the
SHA-256 of the message log, which is computed whether or not --log is given.
--inject applies a faults file (faults_v1: offsets, drifts, stuck or set
values, dropped rows, corrupted messages) at recorded timestamps, so a
Journey C sensor fault or a bad message is part of a deterministic replay;
the faults file and its hash go into the manifest.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

from pdm_common import broker
from pdm_common.hashing import canonical_json, sha256_file
from pdm_common.profile import ProfileError, events_file, load_profile, profile_hash, signal_names

__all__ = ["load_profile", "signal_names", "build_message", "replay", "step_wait", "Faults", "FaultsError"]

MANIFEST_SCHEMA = "replay_manifest_v1"
FAULTS_SCHEMA = "faults_v1"
VALUE_FAULTS = ("offset", "drift", "stuck", "set")
CORRUPT_MODES = ("bad_json", "wrong_schema", "wrong_asset", "missing_signal", "non_numeric", "duplicate", "stale")


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
    payload = canonical_json(
        {
            "schema": profile["topics"]["payload_schema"],
            "asset": profile["asset"]["id"],
            "seq": seq,
            "ts": ts.isoformat(),
            "signals": values,
        }
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


class FaultsError(ValueError):
    """The faults file cannot be applied."""


def _fault_time(value, what: str) -> datetime:
    """A fault time in the file's own time base (naive, like the CSV)."""
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise FaultsError(f"{what}: not an ISO time: {value!r}") from exc
    if moment.tzinfo is not None:
        moment = moment.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return moment


class Faults:
    """Deterministic faults applied to the replayed rows (faults_v1).

    Value faults change one signal over [from, to): ``offset`` adds ``value``,
    ``drift`` adds ``rate_per_h`` times the hours since ``from``, ``stuck``
    holds the value of the first affected row, ``set`` writes ``value`` (with
    ``at`` instead of from/to: the first row at or after it). ``drop`` leaves
    rows out but still counts their sequence numbers, so ingest sees them as
    lost. ``corrupt`` (``at``, ``mode``) damages the message of one row:
    bad_json, wrong_schema, wrong_asset, missing_signal, non_numeric (with
    ``signal``), duplicate (sent twice) or stale (the previous message sent
    again after it).
    """

    def __init__(self, data: dict, signals: list[str], source: str = "faults"):
        if not isinstance(data, dict) or data.get("schema") != FAULTS_SCHEMA:
            raise FaultsError(f"{source}: expected schema: {FAULTS_SCHEMA}")
        self.entries = []
        for i, f in enumerate(data.get("faults") or []):
            where = f"{source}: faults[{i}]"
            kind = f.get("kind")
            if kind not in VALUE_FAULTS + ("drop", "corrupt"):
                raise FaultsError(f"{where}: unknown kind {kind!r}")
            entry = dict(f)
            if kind in VALUE_FAULTS and f.get("signal") not in signals:
                raise FaultsError(f"{where}: signal {f.get('signal')!r} is not in the profile")
            if kind == "corrupt" and f.get("mode") not in CORRUPT_MODES:
                raise FaultsError(f"{where}: mode must be one of {', '.join(CORRUPT_MODES)}")
            if "at" in f:
                entry["at"] = _fault_time(f["at"], where)
            elif kind == "corrupt":
                raise FaultsError(f"{where}: corrupt needs at")
            else:
                if "from" not in f or "to" not in f:
                    raise FaultsError(f"{where}: give at, or from and to")
                entry["from"], entry["to"] = _fault_time(f["from"], where), _fault_time(f["to"], where)
            for key, kinds in (("value", ("offset", "set")), ("rate_per_h", ("drift",))):
                if kind in kinds and not isinstance(f.get(key), (int, float)):
                    raise FaultsError(f"{where}: {kind} needs a numeric {key}")
            self.entries.append(entry)
        self._held: dict[int, float] = {}
        self._fired: set[int] = set()
        self.applied = {k: 0 for k in VALUE_FAULTS + ("drop", "corrupt")}

    @classmethod
    def read(cls, path, signals: list[str]) -> "Faults":
        return cls(yaml.safe_load(Path(path).read_text()), signals, str(path))

    def _active(self, i: int, f: dict, ts: datetime) -> bool:
        if "at" in f:
            if i in self._fired or ts < f["at"]:
                return False
            self._fired.add(i)
            return True
        return f["from"] <= ts < f["to"]

    def apply(self, ts: datetime, values: dict) -> tuple[dict | None, list[str]]:
        """(values after the value faults, or None when the row is dropped; corrupt modes)."""
        values = dict(values)
        modes = []
        for i, f in enumerate(self.entries):
            if not self._active(i, f, ts):
                continue
            kind = f["kind"]
            self.applied[kind] += 1
            if kind == "drop":
                return None, []
            if kind == "corrupt":
                modes.append(f["mode"] if f["mode"] not in ("missing_signal", "non_numeric")
                             else f"{f['mode']}:{f.get('signal', '')}")
                continue
            sig = f["signal"]
            if kind == "offset":
                values[sig] += float(f["value"])
            elif kind == "drift":
                values[sig] += float(f["rate_per_h"]) * (ts - f["from"]).total_seconds() / 3600
            elif kind == "stuck":
                values[sig] = self._held.setdefault(i, values[sig])
            elif kind == "set":
                values[sig] = float(f["value"])
        return values, modes


def corrupt_payload(payload: str, mode: str, signals: list[str]) -> str:
    """The damaged form of one message (Faults.apply's corrupt modes)."""
    name, _, signal = mode.partition(":")
    msg = json.loads(payload)
    if name == "bad_json":
        return payload[: len(payload) // 2]
    if name == "wrong_schema":
        msg["schema"] = "telemetry_json_v0"
    elif name == "wrong_asset":
        msg["asset"] = msg["asset"] + "_unknown"
    elif name == "missing_signal":
        msg["signals"].pop(signal or signals[0], None)
    elif name == "non_numeric":
        msg["signals"][signal or signals[0]] = "n/a"
    return canonical_json(msg)


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
    faults: Faults | None = None,
    manifest_path: str | Path | None = None,
    profile_path: str | Path | None = None,
    arguments: dict | None = None,
) -> dict:
    """Replay the file. `publish` is a callable(topic, payload) or None.

    Returns counters: rows read, published, skipped (unparseable), filtered,
    dropped (by a drop fault) and messages (everything sent, corrupted and
    repeated ones included), plus the message log's SHA-256.
    """
    signals = signal_names(profile)
    counts = {"read": 0, "published": 0, "skipped": 0, "filtered": 0, "dropped": 0, "messages": 0}
    log = open(log_path, "w") if log_path else None
    digest = hashlib.sha256()
    prev_ts: datetime | None = None
    first_ts = last_ts = None
    previous = None
    seq = 0

    def send(topic: str, payload: str) -> None:
        if publish is not None:
            publish(topic, payload)
        line = f"{topic} {payload}\n"
        digest.update(line.encode())
        if log:
            log.write(line)
        counts["messages"] += 1

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
                modes = []
                if faults is not None:
                    values, modes = faults.apply(ts, values)
                    if values is None:
                        counts["dropped"] += 1
                        seq += 1
                        continue
                wait = step_wait(prev_ts, ts, speed, max_wait)
                if wait > 0:
                    time.sleep(wait)
                prev_ts = ts
                first_ts = first_ts or ts
                last_ts = ts
                topic, payload = build_message(profile, seq, ts, values)
                seq += 1
                damaged = payload
                for mode in modes:
                    if mode not in ("duplicate", "stale"):
                        damaged = corrupt_payload(damaged, mode, signals)
                send(topic, damaged)
                if "duplicate" in modes:
                    send(topic, damaged)
                if "stale" in modes and previous is not None:
                    send(topic, previous)
                previous = payload
                counts["published"] += 1
                if progress_every and counts["published"] % progress_every == 0:
                    print(f"published {counts['published']} messages "
                          f"(through {ts.isoformat()})", file=sys.stderr)
                if limit and counts["published"] >= limit:
                    break
    finally:
        if log:
            log.close()
    counts["log_sha256"] = digest.hexdigest()
    if manifest_path:
        write_manifest(manifest_path, profile, data_path, counts, first_ts, last_ts,
                       profile_path=profile_path, faults=faults, arguments=arguments)
    return counts


def write_manifest(path, profile: dict, data_path, counts: dict, first_ts, last_ts, *,
                   profile_path=None, faults: Faults | None = None, arguments: dict | None = None) -> dict:
    """The replay manifest (ING-05). Everything but ``written_at`` is a function
    of the inputs, so two identical replays give the same manifest otherwise."""
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "asset": profile["asset"]["id"],
        "data_file": Path(data_path).name,
        "data_sha256": sha256_file(data_path),
        "profile_sha256": profile_hash(profile_path) if profile_path else None,
        "events_sha256": (sha256_file(events_file(profile, profile_path)) if profile_path
                          and events_file(profile, profile_path).exists() else None),
        "arguments": arguments or {},
        "faults": None,
        "rows_read": counts["read"],
        "published": counts["published"],
        "messages": counts["messages"],
        "dropped": counts["dropped"],
        "filtered": counts["filtered"],
        "skipped": counts["skipped"],
        "first_ts": first_ts.isoformat() if first_ts else None,
        "last_ts": last_ts.isoformat() if last_ts else None,
        "log_sha256": counts["log_sha256"],
        "written_at": datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if faults is not None:
        manifest["faults"] = {"entries": len(faults.entries), "applied": faults.applied,
                              "file": getattr(faults, "source", None),
                              "sha256": getattr(faults, "sha256", None)}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def make_mqtt_publisher(host: str, port: int, qos: int):
    """Connect to the broker; return (publish, close)."""
    import paho.mqtt.client as mqtt

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="pdm-replayer",
    )
    broker.connect(client, "replayer", host, port)
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
    p.add_argument("--manifest", default=None,
                   help="write the replay manifest here (default runs/<asset_id>/replay_manifest.json)")
    p.add_argument("--inject", default=None, help="faults file (faults_v1) to apply during the replay")
    args = p.parse_args(argv)
    if args.speed <= 0:
        p.error("--speed must be positive")
    return args


def main(argv=None) -> None:
    args = parse_args(argv)
    try:
        profile = load_profile(args.profile)
    except ProfileError as exc:
        raise SystemExit(str(exc))
    faults = None
    if args.inject:
        try:
            faults = Faults.read(args.inject, signal_names(profile))
        except (FaultsError, OSError, yaml.YAMLError) as exc:
            raise SystemExit(f"--inject: {exc}")
        faults.source, faults.sha256 = Path(args.inject).name, sha256_file(args.inject)
    manifest = args.manifest or f"runs/{profile['asset']['id']}/replay_manifest.json"
    arguments = {"speed": args.speed, "from": args.from_ts.isoformat() if args.from_ts else None,
                 "to": args.to_ts.isoformat() if args.to_ts else None, "limit": args.limit,
                 "max_wait": args.max_wait, "qos": args.qos, "dry_run": args.dry_run}
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
            faults=faults,
            manifest_path=manifest,
            profile_path=args.profile,
            arguments=arguments,
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
    print(f"manifest {manifest} (log sha256 {counts['log_sha256'][:16]}...)")


if __name__ == "__main__":
    main()
