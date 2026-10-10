"""Ingest -- validate, flag, window and store telemetry (T3; ING-06 to ING-13).

Subscribes to the profile's telemetry topic. Every message ends accepted or
quarantined, never dropped (ING-06, ING-08):

* Validation: a pydantic model built from the profile (schema id, asset id,
  seq, timestamp, every profile signal present, numeric and finite, nothing
  else), then time order. Rejects go to quarantine with an error code -- the
  store's quarantine table and, if given, a JSONL file.
* Out of range (ING-07): a value outside its signal's profile range (digital
  signals: anything but 0 or 1) is kept and flagged on the telemetry row, and
  left off the grid, so it never reaches a score. An isolated bad reading is
  bridged like a missing sample; a persistent one leaves windows incomplete.
* Windows (ING-09 to ING-12): pdm_common.stream.StreamWindows runs the batch
  pipeline -- freeze mask, 10 s grid with bridging, 10-minute windows at a
  1-minute stride, the features of features.yaml -- on a rolling buffer and
  releases each window once later data cannot change it (about 30 s after it
  ends). Complete windows are published as window_v1 and stored; incomplete
  ones are counted as dropped. tools/stream_check.py replays the whole 2020
  file this way and gets the evaluated window table back.
* Reconciliation (ING-08): in = accepted + quarantined, candidate windows =
  complete + dropped, and a jump in seq counts as lost. At shutdown the report
  is printed and appended to runs/<asset>/reconciliation.jsonl, and the
  windows get a data-catalogue entry (ING-13).

Payloads are documented in services/ingest/README.md.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from collections import Counter, deque
from pathlib import Path
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from pdm_common import catalogue, logs, metrics
from pdm_common.features import FeatureSpec, features_file, load_feature_spec
from pdm_common.frozen import features_sha256
from pdm_common.hashing import canonical_json, sha256_file
from pdm_common.mqtt import Out, run_service
from pdm_common.profile import ProfileError, events_file, load_profile, profile_hash, signal_names
from pdm_common.stream import StreamWindow, StreamWindows
from pdm_common.timeutil import iso_utc, to_utc

log = logging.getLogger("ingest")

WINDOW_SCHEMA = "window_v1"
Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]

# When a message breaks several rules, the code reported is the first match.
ERROR_CODES = (
    "bad_json", "wrong_schema", "unknown_asset", "bad_timestamp", "bad_seq",
    "missing_signal", "non_numeric", "unknown_signal", "unexpected_field",
    "duplicate_ts", "stale_ts", "internal_error",
)


def _peek_seq(payload: bytes | str) -> Optional[int]:
    """seq from a payload that may fail validation, or None if unreadable."""
    try:
        seq = json.loads(payload).get("seq")
    except (ValueError, AttributeError, TypeError):
        return None
    return seq if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0 else None


class Rejected(Exception):
    def __init__(self, code: str, text: str):
        super().__init__(text)
        self.code = code


def telemetry_model(profile: dict) -> type[BaseModel]:
    """A pydantic model for telemetry_json_v1, built from the profile.

    Signal names are aliases, so any name the profile declares works.
    """
    signals = create_model(
        "Signals",
        __config__=ConfigDict(extra="forbid"),
        **{f"s{i}": (Number, Field(alias=name)) for i, name in enumerate(signal_names(profile))},
    )
    return create_model(
        "Telemetry",
        __config__=ConfigDict(extra="forbid"),
        schema_id=(Literal[profile["topics"]["payload_schema"]], Field(alias="schema")),
        asset=(Literal[profile["asset"]["id"]], ...),
        seq=(Annotated[int, Field(strict=True, ge=0)], ...),
        ts=(str, ...),
        signals=(signals, ...),
    )


def signal_ranges(profile: dict) -> dict[str, tuple[float, float] | None]:
    """The plausible range of each signal: the profile's ``range`` for analog
    signals; digital signals are 0 or 1 (None marks them)."""
    ranges = {s["name"]: tuple(float(v) for v in s["range"]) if s.get("range") else None
              for s in profile["signals"]["analog"]}
    ranges.update({s["name"]: None for s in profile["signals"]["digital"]})
    return ranges


def out_of_range(values: dict, ranges: dict) -> dict[str, float]:
    """{signal: value} for every value outside its range (ING-07)."""
    flags = {}
    for name, value in values.items():
        bounds = ranges.get(name, "unknown")
        if bounds == "unknown":
            continue
        if bounds is None:
            if value not in (0.0, 1.0):
                flags[name] = value
        elif not bounds[0] <= value <= bounds[1]:
            flags[name] = value
    return flags


def _error_code(errors: list[dict]) -> str:
    codes = set()
    for e in errors:
        loc, kind = tuple(e["loc"]), e["type"]
        if kind == "json_invalid" or (not loc and kind in ("model_type", "dict_type")):
            codes.add("bad_json")
        elif loc[:1] == ("schema",):
            codes.add("wrong_schema")
        elif loc[:1] == ("asset",):
            codes.add("unknown_asset")
        elif loc[:1] == ("ts",):
            codes.add("bad_timestamp")
        elif loc[:1] == ("seq",):
            codes.add("bad_seq")
        elif loc[:1] == ("signals",):
            if kind == "missing" or len(loc) == 1:
                codes.add("missing_signal")
            elif kind == "extra_forbidden":
                codes.add("unknown_signal")
            else:
                codes.add("non_numeric")
        else:
            codes.add("unexpected_field")
    return next((c for c in ERROR_CODES if c in codes), "unexpected_field")


class Ingest:
    """The service minus MQTT: ``handle`` takes a raw payload, returns messages.

    ``store``: a pdm_common.store.Store for telemetry, windows and quarantine
    rows (None: nothing stored). ``spec``: the feature declarations (default:
    the profile's features file next to ``profile_path``).
    """

    def __init__(self, profile: dict, profile_sha256: str,
                 quarantine_path: Optional[Path] = None, *, spec: Optional[FeatureSpec] = None,
                 profile_path: Optional[Path] = None, store=None, telemetry_batch: int = 360):
        self.profile = profile
        self.asset = profile["asset"]["id"]
        self.windows_topic = profile["topics"]["windows"]
        self.source_tz = profile.get("source_timezone", "UTC")
        self.profile_sha256 = profile_sha256
        self.model = telemetry_model(profile)
        self.ranges = signal_ranges(profile)
        if spec is None:
            if profile_path is None:
                raise ValueError("Ingest needs the feature spec or the profile's path")
            spec = load_feature_spec(features_file(profile, profile_path))
        self.spec = spec
        self.features_sha256 = features_sha256(spec.raw)
        self.stream = StreamWindows(profile, spec)
        self.quarantine_path = Path(quarantine_path) if quarantine_path else None
        self.store = store
        self.telemetry_batch = telemetry_batch
        self._telemetry_rows: list[dict] = []
        self._flagged: deque = deque()          # (ts, {signal: value}) of recent out-of-range records
        self.counts = Counter({"in": 0, "accepted": 0, "quarantined": 0, "lost": 0, "out_of_range": 0,
                               "windows": 0, "complete": 0, "dropped": 0})
        self.by_code: Counter = Counter()
        self.flagged_by_signal: Counter = Counter()
        self.first_ts: Optional[dt.datetime] = None
        self.last_ts: Optional[dt.datetime] = None
        self._last_ts: Optional[dt.datetime] = None
        self._last_seq: Optional[int] = None
        self.replays = 1
        self.m = metrics.service_metrics("ingest")
        for code in ERROR_CODES:              # every series exists from the start, so rate() sees the first one
            self.m.records_quarantined_total.labels(asset=self.asset, code=code)
        self.m.queue_depth.labels(queue="stream_buffer").set_function(lambda: self.stream.buffered)
        self.m.queue_depth.labels(queue="telemetry_batch").set_function(lambda: len(self._telemetry_rows))

    # -- validation -------------------------------------------------------
    def _parse(self, payload: bytes | str):
        try:
            msg = self.model.model_validate_json(payload)
        except ValidationError as exc:
            errors = exc.errors()
            text = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'payload'}: {e['msg']}"
                             for e in errors)
            raise Rejected(_error_code(errors), text) from None
        try:
            ts = to_utc(msg.ts, tz=self.source_tz)
        except (ValueError, TypeError, OverflowError) as exc:
            raise Rejected("bad_timestamp", f"ts: {exc}") from None
        values = msg.signals.model_dump(by_alias=True)
        return msg.seq, ts, values

    def _check_order(self, ts: dt.datetime) -> None:
        if self._last_ts is not None and ts <= self._last_ts:
            code = "duplicate_ts" if ts == self._last_ts else "stale_ts"
            raise Rejected(code, f"ts {iso_utc(ts)} is not after the last accepted "
                                 f"{iso_utc(self._last_ts)}")

    def _track_seq(self, payload: bytes | str) -> list[Out]:
        """Sequence bookkeeping for every message whose seq is readable, accepted
        or not: a jump counts as lost, and seq 0 starts a new replay, which
        releases what the previous replay left open and starts a new stream."""
        seq = _peek_seq(payload)
        if seq is None:
            return []
        outs = []
        if seq == 0 and self._last_seq is not None:
            log.info("seq 0 after seq %d: a new replay; closing the previous stream", self._last_seq)
            outs = self.flush()
            self.stream = StreamWindows(self.profile, self.spec)
            self._flagged.clear()
            self._last_ts = self._last_seq = None
            self.replays += 1
        if self._last_seq is not None and seq > self._last_seq + 1:
            self.counts["lost"] += seq - self._last_seq - 1
            self.m.records_lost_total.labels(asset=self.asset).inc(seq - self._last_seq - 1)
        if self._last_seq is None or seq > self._last_seq:
            self._last_seq = seq
        return outs

    def _quarantine(self, payload: bytes | str, err: Rejected) -> None:
        self.counts["quarantined"] += 1
        self.by_code[err.code] += 1
        self.m.records_quarantined_total.labels(asset=self.asset, code=err.code).inc()
        raw = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
        received = iso_utc(dt.datetime.now(dt.timezone.utc))      # wall clock: logs only
        record = {"received_at": received, "asset": self.asset, "error_code": err.code,
                  "error": str(err), "payload": raw}
        if self.quarantine_path is not None:
            with open(self.quarantine_path, "a") as f:
                f.write(json.dumps(record, sort_keys=True) + "\n")
        if self.store is not None:
            self.store.insert("quarantine", {"asset_id": self.asset, "received_at": received,
                                             "error_code": err.code, "error_text": str(err), "payload": raw})
        log.warning("quarantined %s: %s", err.code, err)

    # -- the handler ------------------------------------------------------
    def handle(self, payload: bytes | str) -> list[Out]:
        """Every message ends accepted or quarantined, so the counts balance."""
        self.counts["in"] += 1
        self.m.records_in_total.labels(asset=self.asset).inc()
        outs = self._track_seq(payload)
        try:
            seq, ts, values = self._parse(payload)
            self._check_order(ts)
        except Rejected as err:
            self._quarantine(payload, err)
            return outs
        except Exception as exc:              # a bug must not unbalance the counts
            log.exception("unexpected error validating a message")
            self._quarantine(payload, Rejected("internal_error", f"{type(exc).__name__}: {exc}"))
            return outs
        self._last_ts = ts
        self.first_ts = self.first_ts or ts
        self.last_ts = ts
        self.counts["accepted"] += 1
        self.m.records_accepted_total.labels(asset=self.asset).inc()
        flags = out_of_range(values, self.ranges)
        if flags:
            self.counts["out_of_range"] += 1
            self.m.records_out_of_range_total.labels(asset=self.asset).inc()
            self.flagged_by_signal.update(flags.keys())
            self._flagged.append((ts, flags))
            log.warning("out of range at %s: %s", iso_utc(ts), flags)
        self._telemetry_rows.append({"asset_id": self.asset, "ts": ts, "seq": seq, "signals": values,
                                     "out_of_range": flags or None})
        windows = self.stream.add(ts, values, excluded=flags.keys())
        if windows or len(self._telemetry_rows) >= self.telemetry_batch:
            self._write_telemetry()
        return outs + self._publish(windows)

    def flush(self) -> list[Out]:
        """End of stream or shutdown: release every window the data allows."""
        outs = self._publish(self.stream.flush())
        self._write_telemetry()
        return outs

    # -- output -----------------------------------------------------------
    def _write_telemetry(self) -> None:
        if self.store is not None and self._telemetry_rows:
            self.store.insert_many("telemetry", self._telemetry_rows)
        self._telemetry_rows = []

    def _window_flags(self, w: StreamWindow) -> dict[str, int]:
        """Out-of-range readings per signal inside the window."""
        start, end = w.start.to_pydatetime(), w.end.to_pydatetime()
        while self._flagged and self._flagged[0][0] < start - dt.timedelta(minutes=10):
            self._flagged.popleft()
        counts: Counter = Counter()
        for ts, flags in self._flagged:
            if start <= ts < end:
                counts.update(flags.keys())
        return dict(sorted(counts.items()))

    def window_payload(self, w: StreamWindow) -> dict:
        return {
            "schema": WINDOW_SCHEMA,
            "asset": self.asset,
            "profile_sha256": self.profile_sha256,
            "features_sha256": self.features_sha256,
            "start": iso_utc(w.start.to_pydatetime()),
            "end": iso_utc(w.end.to_pydatetime()),
            "n_observed": w.n_observed,
            "n_bridged": w.n_bridged,
            "out_of_range": self._window_flags(w),
            "features": w.json_features(),
        }

    def _publish(self, windows: list[StreamWindow]) -> list[Out]:
        outs = []
        rows = []
        for w in windows:
            self.counts["windows"] += 1
            if not w.complete:
                self.counts["dropped"] += 1
                self.m.windows_dropped_total.labels(asset=self.asset).inc()
                continue
            self.counts["complete"] += 1
            self.m.windows_published_total.labels(asset=self.asset).inc()
            payload = self.window_payload(w)
            outs.append(Out(self.windows_topic, canonical_json(payload)))
            rows.append({"asset_id": self.asset, "window_start": payload["start"], "window_end": payload["end"],
                         "features": payload["features"], "n_bridged": w.n_bridged,
                         "profile_sha256": self.profile_sha256})
        if rows and self.store is not None:
            self.store.insert_many("windows", rows)
        if outs:
            c = self.counts
            log.info("windows to %s | in=%d accepted=%d quarantined=%d lost=%d complete=%d dropped=%d",
                     json.loads(outs[-1].payload)["end"], c["in"], c["accepted"], c["quarantined"], c["lost"],
                     c["complete"], c["dropped"])
        return outs

    def reconciliation(self) -> dict:
        c = self.counts
        return {
            "asset": self.asset,
            "in": c["in"], "accepted": c["accepted"], "quarantined": c["quarantined"], "lost": c["lost"],
            "quarantined_by_code": dict(sorted(self.by_code.items())),
            "out_of_range_records": c["out_of_range"],
            "out_of_range_by_signal": dict(sorted(self.flagged_by_signal.items())),
            "windows": c["windows"], "windows_complete": c["complete"], "windows_dropped": c["dropped"],
            "replays": self.replays,
            "first_ts": iso_utc(self.first_ts) if self.first_ts else None,
            "last_ts": iso_utc(self.last_ts) if self.last_ts else None,
            "balanced": c["in"] == c["accepted"] + c["quarantined"] and c["windows"] == c["complete"] + c["dropped"],
        }

    def catalogue_entry(self, *, replay_manifest: Optional[Path] = None, profile_path: Optional[Path] = None,
                        path=None) -> dict:
        """Record the windows this run produced in the data catalogue (ING-13)."""
        manifest = json.loads(Path(replay_manifest).read_text()) if replay_manifest else {}
        events = events_file(self.profile, profile_path) if profile_path else None
        lineage = {"telemetry": manifest.get("data_file"), "data_sha256": manifest.get("data_sha256"),
                   "replay_log_sha256": manifest.get("log_sha256"), "profile_sha256": self.profile_sha256,
                   "events_sha256": sha256_file(events) if events and events.exists() else None,
                   "features_sha256": self.features_sha256, "code": "services/ingest/ingest.py"}
        r = self.reconciliation()
        name = f"store:windows/{self.asset}/{r['first_ts']}..{r['last_ts']}"
        return catalogue.add(name, "stream_windows", rows=r["windows_complete"],
                             columns=len(self.spec.names) + 7,
                             manifest_sha256=sha256_file(replay_manifest) if replay_manifest else None,
                             lineage=lineage, path=path)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile", required=True, help="asset profile YAML")
    p.add_argument("--broker", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--store", default=None, help="store URL (default $PDM_STORE or sqlite:///runs/pdm.db)")
    p.add_argument("--no-store", action="store_true", help="publish windows only; store nothing")
    p.add_argument("--quarantine", default=None,
                   help="quarantine file (default runs/<asset_id>/quarantine.jsonl)")
    p.add_argument("--replay-manifest", default=None,
                   help="the replayer's manifest, for the catalogue entry's lineage (ING-05, ING-13)")
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    logs.setup("ingest")
    try:
        profile = load_profile(args.profile)
    except ProfileError as exc:
        raise SystemExit(str(exc))
    asset = profile["asset"]["id"]
    quarantine = Path(args.quarantine or f"runs/{asset}/quarantine.jsonl")
    quarantine.parent.mkdir(parents=True, exist_ok=True)
    store = None
    if not args.no_store:
        from pdm_common.store import open_store
        store = open_store(args.store)
    ingest = Ingest(profile, profile_hash(args.profile), quarantine, profile_path=Path(args.profile), store=store)
    log.info("asset %s: %s -> %s, quarantine %s, store %s", asset, profile["topics"]["telemetry"],
             profile["topics"]["windows"], quarantine, store.url if store else "none")
    run_service("ingest", {profile["topics"]["telemetry"]: ingest.handle},
                host=args.broker, port=args.port, on_stop=ingest.flush)
    report = ingest.reconciliation()
    print(json.dumps(report, sort_keys=True), file=sys.stdout)
    with open(quarantine.parent / "reconciliation.jsonl", "a") as f:
        f.write(json.dumps(report, sort_keys=True) + "\n")
    if report["windows_complete"]:
        entry = ingest.catalogue_entry(replay_manifest=args.replay_manifest, profile_path=Path(args.profile))
        log.info("catalogue: %s (%d windows)", entry["dataset"], entry["rows"])
    if not report["balanced"]:
        raise SystemExit("reconciliation failed: in != accepted + quarantined or windows != complete + dropped")


if __name__ == "__main__":
    main()
