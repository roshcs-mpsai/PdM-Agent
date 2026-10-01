"""Ingest stub -- validate telemetry, quarantine rejects, publish windows (R0.3).

Subscribes to the profile's telemetry topic and validates every message with
a pydantic model built from the profile (ING-06, stub): schema id, asset id,
sequence number, timestamp, and every profile signal present, numeric and
finite. Rejects are appended to quarantine.jsonl with an error code and the
raw payload; nothing is dropped silently. Accepted samples feed one-minute
means of the detector's raw inputs, published as window_v1 on the profile's
windows topic. The counts reconcile: in = accepted + quarantined (ING-08);
gaps in seq are counted as lost.

Version Zero: R1a.6 replaces the one-minute means with 10-minute windows on
the masked, bridged 10 s grid, flags out-of-range values (ING-07) and writes
to TimescaleDB. Payloads are documented in services/ingest/README.md.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from pdm_common.hashing import canonical_json
from pdm_common.mqtt import Out, run_service
from pdm_common.profile import ProfileError, load_profile, profile_hash, signal_names
from pdm_common.timeutil import iso_utc, to_utc
from pdm_common.windows import MinuteWindower, Window

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
    """The service minus MQTT: ``handle`` takes a raw payload, returns messages."""

    def __init__(self, profile: dict, profile_sha256: str,
                 quarantine_path: Optional[Path] = None):
        self.asset = profile["asset"]["id"]
        self.windows_topic = profile["topics"]["windows"]
        self.source_tz = profile.get("source_timezone", "UTC")
        self.profile_sha256 = profile_sha256
        self.model = telemetry_model(profile)
        self.inputs = list(profile["detector"]["inputs"])
        self.windower = MinuteWindower(self.inputs)
        self.quarantine_path = Path(quarantine_path) if quarantine_path else None
        self.counts = Counter({"in": 0, "accepted": 0, "quarantined": 0, "lost": 0, "windows": 0})
        self.by_code: Counter = Counter()
        self._last_ts: Optional[dt.datetime] = None
        self._last_seq: Optional[int] = None

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
        return ts, values

    def _check_order(self, ts: dt.datetime) -> None:
        if self._last_ts is not None and ts <= self._last_ts:
            code = "duplicate_ts" if ts == self._last_ts else "stale_ts"
            raise Rejected(code, f"ts {iso_utc(ts)} is not after the last accepted "
                                 f"{iso_utc(self._last_ts)}")

    def _track_seq(self, payload: bytes | str) -> list[Out]:
        """Sequence bookkeeping for every message whose seq is readable, accepted
        or not: a jump counts as lost, and seq 0 starts a new replay, which
        closes the previous replay's open window."""
        seq = _peek_seq(payload)
        if seq is None:
            return []
        outs = []
        if seq == 0 and self._last_seq is not None:
            log.info("seq 0 after seq %d: a new replay; closing the open window", self._last_seq)
            outs = self.flush()
            self._last_ts = self._last_seq = None
        if self._last_seq is not None and seq > self._last_seq + 1:
            self.counts["lost"] += seq - self._last_seq - 1
        if self._last_seq is None or seq > self._last_seq:
            self._last_seq = seq
        return outs

    def _quarantine(self, payload: bytes | str, err: Rejected) -> None:
        self.counts["quarantined"] += 1
        self.by_code[err.code] += 1
        raw = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
        record = {
            "received_at": iso_utc(dt.datetime.now(dt.timezone.utc)),   # wall clock: logs only
            "asset": self.asset,
            "error_code": err.code,
            "error": str(err),
            "payload": raw,
        }
        if self.quarantine_path is not None:
            with open(self.quarantine_path, "a") as f:
                f.write(json.dumps(record, sort_keys=True) + "\n")
        log.warning("quarantined %s: %s", err.code, err)

    # -- the handler ------------------------------------------------------
    def handle(self, payload: bytes | str) -> list[Out]:
        """Every message ends accepted or quarantined, so the counts balance."""
        self.counts["in"] += 1
        outs = self._track_seq(payload)
        try:
            ts, values = self._parse(payload)
            self._check_order(ts)
        except Rejected as err:
            self._quarantine(payload, err)
            return outs
        except Exception as exc:              # a bug must not unbalance the counts
            log.exception("unexpected error validating a message")
            self._quarantine(payload, Rejected("internal_error", f"{type(exc).__name__}: {exc}"))
            return outs
        self._last_ts = ts
        self.counts["accepted"] += 1
        return outs + [self._window_message(w) for w in self.windower.add(ts, values)]

    def flush(self) -> list[Out]:
        """Close the open window (shutdown)."""
        return [self._window_message(w) for w in self.windower.flush()]

    def _window_message(self, w: Window) -> Out:
        self.counts["windows"] += 1
        payload = canonical_json({
            "schema": WINDOW_SCHEMA,
            "asset": self.asset,
            "profile_sha256": self.profile_sha256,
            "start": iso_utc(w.start),
            "end": iso_utc(w.end),
            "n": w.n,
            "features": w.means,
        })
        c = self.counts
        log.info("window %s n=%d | in=%d accepted=%d quarantined=%d lost=%d",
                 iso_utc(w.end), w.n, c["in"], c["accepted"], c["quarantined"], c["lost"])
        return Out(self.windows_topic, payload)

    def reconciliation(self) -> dict:
        c = self.counts
        return {
            "asset": self.asset,
            "in": c["in"], "accepted": c["accepted"], "quarantined": c["quarantined"],
            "lost": c["lost"], "windows": c["windows"],
            "quarantined_by_code": dict(sorted(self.by_code.items())),
            "balanced": c["in"] == c["accepted"] + c["quarantined"],
        }


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile", required=True, help="asset profile YAML")
    p.add_argument("--broker", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--quarantine", default=None,
                   help="quarantine file (default runs/<asset_id>/quarantine.jsonl)")
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        profile = load_profile(args.profile)
    except ProfileError as exc:
        raise SystemExit(str(exc))
    asset = profile["asset"]["id"]
    quarantine = Path(args.quarantine or f"runs/{asset}/quarantine.jsonl")
    quarantine.parent.mkdir(parents=True, exist_ok=True)
    ingest = Ingest(profile, profile_hash(args.profile), quarantine)
    log.info("asset %s: %s -> %s, quarantine %s", asset, profile["topics"]["telemetry"],
             profile["topics"]["windows"], quarantine)
    run_service("ingest", {profile["topics"]["telemetry"]: ingest.handle},
                host=args.broker, port=args.port, on_stop=ingest.flush)
    report = ingest.reconciliation()
    print(json.dumps(report, sort_keys=True), file=sys.stdout)
    if not report["balanced"]:
        raise SystemExit("reconciliation failed: in != accepted + quarantined")


if __name__ == "__main__":
    main()
