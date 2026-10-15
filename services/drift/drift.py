"""Drift -- per-signal drift checks against the healthy reference (T25; OPS-04, OPS-06).

Subscribes to the profile's telemetry topic. Every ``drift.every`` (6 h) of
stream time, on boundaries aligned to the epoch (00:00, 06:00, 12:00 and
18:00 UTC), it compares the last ``drift.window`` (24 h) of each signal in
``drift.signals`` with the healthy reference of the regime the window ends in,
and publishes one drift_v1 on ``pdm/_sys/drift`` (retained):

* the statistic is the first Wasserstein distance between the window's values
  and the reference, divided by the reference's standard deviation (floored
  at 0.001) -- the number Evidently's ``wasserstein`` stattest reports.
  Evidently is not installed here; ``drift_distance`` computes the same
  distance with scipy.stats.wasserstein_distance;
* the reference and thresholds come from eval/frozen/<asset>/drift_reference.json,
  built by tools/drift_reference.py from healthy data only: 200 quantiles of
  the healthy values per signal and regime, and one threshold per signal and
  regime;
* only samples of the regime the window ends in count (``regime_of`` on the
  window end, events.yaml regimes), so the light-duty February and the summer
  after the Jun 8 repair are judged against their own healthy data, and a
  window that straddles a regime change is judged on the new regime's samples;
* the stream is cut the way the healthy reference was: the gap edge (the first
  ``gap_edges.length`` after a recording gap of ``gap_edges.after_gaps_of`` or
  more, the refill after a restart, grey in events.yaml) is left out, since
  the reference holds none; so are readings outside their profile range, as
  ingest leaves them off the grid, and logger freezes (the profile's
  ``freeze_rule``, found with pdm_common.quality the way ingest finds them);
  records ingest would quarantine and records out of time order are ignored,
  and seq 0 after other records is a new replay: a new stream with no alert;
* a signal with fewer samples than the reference's ``min_samples`` (half the
  window at the profile's sampling step) gets no verdict: distance null;
* a signal's alert holds until a check with enough data comes in under its
  threshold, so a recording gap neither raises nor clears it. The message's
  ``alert`` is true while any signal's is; the gate turns it into NE 107 out
  of specification.

Every check writes one drift_alerts row per signal. When a signal goes from no
alert to alert, the check also writes one candidate request for a training run
(OPS-06; eval/retrain_candidate.py reads it) and carries it in drift_v1's
``candidate``. Nothing here trains, registers or promotes a model (PR-23).

    python services/drift/drift.py --profile profiles/metropt3_apu/asset_profile.yaml [--store "$PDM_STORE"]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import re
import sys
from collections import Counter, deque
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from scipy.stats import wasserstein_distance

ROOT = Path(__file__).resolve().parents[2]

from pdm_common import logs, metrics, schemas  # noqa: E402
from pdm_common.events import load_events  # noqa: E402
from pdm_common.hashing import canonical_json, sha256_file  # noqa: E402
from pdm_common.mqtt import Out, run_service  # noqa: E402
from pdm_common.profile import (ProfileError, analog_names, events_file, load_profile,  # noqa: E402
                                profile_hash, signal_names)
from pdm_common.quality import freeze_runs, frozen_rows  # noqa: E402
from pdm_common.timeutil import iso_utc, parse_duration, to_utc  # noqa: E402
from pdm_common.zones import regime_of  # noqa: E402

log = logging.getLogger("drift")

DRIFT_SCHEMA = "drift_v1"
DRIFT_TOPIC = "pdm/_sys/drift"           # FRD section 9: one system topic, retained; the gate reads it
REFERENCE_SCHEMA = "drift_reference_v1"
REQUEST_SCHEMA = "candidate_request_v1"
NORM_FLOOR = 0.001                       # Evidently's floor on the reference standard deviation
QUANTILES = 200                          # reference quantiles per signal and regime


# --- the statistic (shared with tools/drift_reference.py) ------------------------------------

def reference_probabilities(count: int = QUANTILES) -> np.ndarray:
    """Where the reference quantiles sit: the middle of ``count`` equal slices,
    (k + 0.5) / count, so each quantile stands for the same share of the data."""
    return (np.arange(count) + 0.5) / count


def summarize(values, count: int = QUANTILES) -> dict:
    """A healthy sample as the reference stores it: size, standard deviation and quantiles."""
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if v.size == 0:
        raise ValueError("no finite values to summarize")
    return {"n": int(v.size), "std": float(v.std()), "quantiles": np.quantile(v, reference_probabilities(count)).tolist()}


def drift_distance(values, quantiles, std) -> float:
    """First Wasserstein distance between ``values`` and the reference quantiles, in
    reference standard deviations: scipy.stats.wasserstein_distance(values, quantiles)
    / max(std, 0.001), as Evidently's ``wasserstein`` stattest normalises it."""
    return float(wasserstein_distance(np.asarray(values, dtype=float), np.asarray(quantiles, dtype=float))
                 / max(float(std), NORM_FLOOR))


# --- the reference ----------------------------------------------------------------------------

class DriftError(ValueError):
    """The drift reference is missing, malformed or not for this profile."""


def load_reference(path) -> dict:
    path = Path(path)
    if not path.exists():
        raise DriftError(f"{path}: no drift reference; build it with "
                         "python tools/drift_reference.py --processed <processed folder>")
    try:
        return json.loads(path.read_text())
    except ValueError as exc:
        raise DriftError(f"{path}: not JSON: {exc}") from exc


def _duration(text) -> Optional[dt.timedelta]:
    try:
        return parse_duration(text)
    except (ValueError, TypeError):
        return None


def _spans(regimes) -> list[tuple]:
    return [(r["name"], to_utc(r["start"]), to_utc(r["end"])) for r in regimes or []]


def check_reference(reference: dict, profile: dict, events: Optional[dict] = None) -> None:
    """Refuse a reference built for another asset, window, cadence or set of regimes,
    or one that lacks a threshold for a signal the profile checks."""
    problems = []
    drift = profile.get("drift") or {}
    if not drift:
        raise DriftError("the profile has no drift section (OPS-04)")
    if reference.get("schema") != REFERENCE_SCHEMA:
        problems.append(f"expected schema {REFERENCE_SCHEMA}, got {reference.get('schema')!r}")
    if reference.get("asset") != profile["asset"]["id"]:
        problems.append(f"built for asset {reference.get('asset')!r}, not {profile['asset']['id']!r}")
    for key in ("window", "every"):
        if _duration(reference.get(key)) is None or _duration(reference.get(key)) != _duration(drift.get(key)):
            problems.append(f"{key} {reference.get(key)!r} is not the profile's drift.{key} {drift.get(key)!r}")
    if not isinstance(reference.get("min_samples"), int) or reference["min_samples"] < 1:
        problems.append("min_samples must be a positive integer")
    edges = reference.get("gap_edges")
    if edges is not None and (_duration(edges.get("after_gaps_of")) is None or _duration(edges.get("length")) is None):
        problems.append("gap_edges needs after_gaps_of and length durations")
    by_regime = reference.get("by_regime") or {}
    if not by_regime:
        problems.append("no regime has a reference")
    known = {r["name"] for r in reference.get("regimes") or []}
    for regime, entries in by_regime.items():
        if regime not in known:
            problems.append(f"regime {regime!r} is not among the reference's regimes")
        for name in drift.get("signals") or []:
            e = (entries or {}).get(name) or {}
            if not e.get("quantiles") or not isinstance(e.get("std"), (int, float)) \
                    or not isinstance(e.get("threshold"), (int, float)):
                problems.append(f"no quantiles, std or threshold for {name} in {regime}")
    if events is not None and events.get("regimes") and _spans(events["regimes"]) != _spans(reference.get("regimes")):
        problems.append("the events file's regimes are not the ones the reference was built on; "
                        "rebuild it with tools/drift_reference.py")
    if events is not None and edges is not None and events.get("gap_edges") and not all(
            _duration(events["gap_edges"].get(k)) == _duration(edges.get(k)) for k in ("after_gaps_of", "length")):
        problems.append("the events file's gap_edges are not the ones the reference was built with; "
                        "rebuild it with tools/drift_reference.py")
    if problems:
        raise DriftError("drift reference refused: " + "; ".join(problems))


# --- time helpers -------------------------------------------------------------------------------

def _ns(moment: dt.datetime) -> int:
    return int(moment.timestamp()) * 10**9 + moment.microsecond * 1000


def _moment(ns: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ns // 10**9, tz=dt.timezone.utc) + dt.timedelta(microseconds=ns % 10**9 // 1000)


def _ceil(ns: int, step: int) -> int:
    return -(-ns // step) * step


def _floor(ns: int, step: int) -> int:
    return ns // step * step


def _index(ns: np.ndarray) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(np.asarray(ns, dtype=np.int64).astype("datetime64[ns]")).tz_localize("UTC")


# --- the service ----------------------------------------------------------------------------------

class DriftService:
    """The service minus MQTT: telemetry in, one drift_v1 per check out.

    ``reference``: the drift_reference_v1 document. ``events``: the events file, whose
    regimes must be the reference's (default: the reference's own copy). ``requests_dir``:
    where candidate requests are written (None: only carried in drift_v1)."""

    def __init__(self, profile: dict, reference: dict, store=None, *, events: Optional[dict] = None,
                 profile_sha256: Optional[str] = None, reference_sha256: Optional[str] = None,
                 requests_dir=None):
        check_reference(reference, profile, events)
        if profile_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", profile_sha256):
            raise DriftError("profile_sha256 must be the profile file's SHA-256 (64 hex digits)")
        drift = profile["drift"]
        self.asset = profile["asset"]["id"]
        self.payload_schema = profile["topics"]["payload_schema"]
        self.source_tz = profile.get("source_timezone", "UTC")
        self.window_text = str(drift["window"])
        self.window_ns = int(parse_duration(drift["window"]).total_seconds() * 10**9)
        self.every_ns = int(parse_duration(drift["every"]).total_seconds() * 10**9)
        self.signals = list(drift["signals"])
        self.analog = analog_names(profile)
        # analog channels for the freeze check, then any digital signal the profile checks for drift
        self.channels = self.analog + [s for s in self.signals if s not in self.analog]
        self.required = signal_names(profile)                       # a record must carry them all, as ingest asks
        self.column = {s: self.channels.index(s) for s in self.signals}
        ranges = {s["name"]: tuple(float(v) for v in s["range"]) for s in profile["signals"]["analog"]}
        self.ranges = [ranges.get(s) for s in self.signals]          # None: digital, 0 or 1
        rule = profile["freeze_rule"]
        self.freeze_minutes = parse_duration(rule["min_duration"]).total_seconds() / 60
        self.freeze_step_s = float(rule["max_step_s"])
        self.freeze_ns = int(parse_duration(rule["min_duration"]).total_seconds() * 10**9)
        edges = reference.get("gap_edges")           # None: the reference kept the restarts, so does the check
        self.edge_after_ns = int(parse_duration(edges["after_gaps_of"]).total_seconds() * 10**9) if edges else None
        self.edge_length_ns = int(parse_duration(edges["length"]).total_seconds() * 10**9) if edges else 0
        self.reference = reference
        self.regimes = {"regimes": reference["regimes"]}
        self.min_samples = int(reference["min_samples"])
        self.store = store
        self.profile_sha256 = profile_sha256
        self.reference_sha256 = reference_sha256
        self.requests_dir = Path(requests_dir) if requests_dir is not None else None
        self.active = {s: False for s in self.signals}
        self.counts: Counter = Counter()
        self.m = metrics.service_metrics("drift")
        self.m.queue_depth.labels(queue="drift_buffer").set_function(lambda: len(self._buffer))
        self._reset_stream()

    def _reset_stream(self) -> None:
        # (ts ns, channel values, indexes of signals out of range, inside a gap edge)
        self._buffer: deque = deque()
        self._last_ts: Optional[int] = None
        self._last_seq: Optional[int] = None
        self._next_check: Optional[int] = None
        self._edge_until: Optional[int] = None

    # -- input -----------------------------------------------------------------------------------
    def _parse(self, payload):
        """(seq, ts ns, channel values, out-of-range signal indexes), or (None, reason)."""
        try:
            msg = json.loads(payload)
        except (ValueError, TypeError):
            return None, "bad_json"
        if not isinstance(msg, dict) or msg.get("schema") != self.payload_schema:
            return None, "wrong_schema"
        if msg.get("asset") != self.asset:
            return None, "other_asset"
        seq = msg.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            return None, "bad_seq"
        try:
            ts = _ns(to_utc(msg.get("ts"), tz=self.source_tz))
        except (ValueError, TypeError, OverflowError):
            return None, "bad_timestamp"
        signals = msg.get("signals")
        if not isinstance(signals, dict):
            return None, "missing_signal"
        for name in self.required:
            if name not in signals:
                return None, "missing_signal"
            v = signals[name]
            try:
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                    return None, "non_numeric"
            except OverflowError:                   # an integer too large for a float
                return None, "non_numeric"
        row = [float(signals[c]) for c in self.channels]
        bad = tuple(j for j, s in enumerate(self.signals) if not self._in_range(j, row[self.column[s]]))
        return (seq, ts, row, bad), None

    def _in_range(self, j: int, value: float) -> bool:
        bounds = self.ranges[j]
        return value in (0.0, 1.0) if bounds is None else bounds[0] <= value <= bounds[1]

    def handle_telemetry(self, payload) -> list[Out]:
        """One telemetry_json_v1 record; returns the drift_v1 of every check it makes due."""
        self.counts["messages"] += 1
        parsed, reason = self._parse(payload)
        if parsed is None:
            self.counts[f"ignored_{reason}"] += 1
            return []
        seq, ts, row, bad = parsed
        if seq == 0 and self._last_seq is not None:
            log.info("seq 0 after seq %d: a new replay; starting a new stream with no alert", self._last_seq)
            self._reset_stream()
            self.active = {s: False for s in self.signals}
        self._last_seq = seq
        if self._last_ts is not None and ts <= self._last_ts:
            self.counts["ignored_out_of_order"] += 1
            return []
        if self._next_check is None:
            self._next_check = _ceil(ts, self.every_ns)
        if self.edge_after_ns is not None and self._last_ts is not None and ts - self._last_ts >= self.edge_after_ns:
            self._edge_until = ts + self.edge_length_ns          # data resumes after a recording gap
        edge = self._edge_until is not None and ts < self._edge_until
        outs = self._advance(ts)
        self._buffer.append((ts, row, bad, edge))
        self._last_ts = ts
        self.counts["accepted"] += 1
        self.counts["gap_edge_records"] += int(edge)
        if bad:
            self.counts["out_of_range_values"] += len(bad)
        return outs

    def _advance(self, ts: int) -> list[Out]:
        """Run every check whose boundary ``ts`` has reached, over the records before it."""
        outs = []
        while ts >= self._next_check:
            at = self._next_check
            if self._buffer and self._buffer[-1][0] >= at - self.window_ns:
                outs += self.check(at)
                self._next_check = at + self.every_ns
            else:                       # no record reaches this window or any later one before ts
                nxt = _floor(ts, self.every_ns) + self.every_ns
                self.counts["checks_without_data"] += (nxt - at) // self.every_ns
                self._next_check = nxt
            keep_from = self._next_check - self.window_ns - self.freeze_ns
            while self._buffer and self._buffer[0][0] < keep_from:
                self._buffer.popleft()
        return outs

    # -- the check -------------------------------------------------------------------------------
    def _arrays(self):
        n = len(self._buffer)
        ts = np.fromiter((r[0] for r in self._buffer), dtype=np.int64, count=n)
        values = np.array([r[1] for r in self._buffer], dtype=float).reshape(n, len(self.channels))
        bad = np.zeros((n, len(self.signals)), dtype=bool)
        for i, r in enumerate(self._buffer):
            if r[2]:
                bad[i, list(r[2])] = True
        edge = np.fromiter((r[3] for r in self._buffer), dtype=bool, count=n)
        return ts, values, bad, edge

    def _frozen(self, ts: np.ndarray, values: np.ndarray) -> np.ndarray:
        """Records inside a logger freeze (profile freeze_rule), as ingest masks them. The buffer
        reaches ``min_duration`` before the window, so a freeze running into it is found; a run
        still shorter than that at the window's end is not a freeze yet and stays in."""
        if len(ts) < 2:
            return np.zeros(len(ts), dtype=bool)
        df = pd.DataFrame(values[:, :len(self.analog)], columns=self.analog)
        df.insert(0, "timestamp", pd.DatetimeIndex(ts.astype("datetime64[ns]")))
        runs = freeze_runs(df, self.analog, min_minutes=self.freeze_minutes, max_step_s=self.freeze_step_s)
        return frozen_rows(df, runs) if len(runs) else np.zeros(len(ts), dtype=bool)

    def check(self, at_ns: int) -> list[Out]:
        """The check at boundary ``at_ns``: the window [at - drift.window, at) of the buffer."""
        at = iso_utc(_moment(at_ns))
        regime = regime_of(_index([at_ns]), self.regimes)[0]
        by_signal = self.reference["by_regime"].get(regime) if regime is not None else None
        if by_signal is None:
            self.counts["checks_without_reference"] += 1
            log.warning("check at %s: no healthy reference for regime %s; skipped", at, regime)
            return []
        ts, values, bad, edge = self._arrays()
        in_window = (ts >= at_ns - self.window_ns) & (ts < at_ns)
        usable = in_window & ~edge & ~self._frozen(ts, values)
        if usable.any():
            usable[usable] = regime_of(_index(ts[usable]), self.regimes) == regime
        entries, newly = [], []
        for j, name in enumerate(self.signals):
            ref = by_signal[name]
            v = values[usable & ~bad[:, j], self.column[name]]
            entry = {"signal": name, "regime": regime, "threshold": float(ref["threshold"]), "n": int(v.size)}
            if v.size < self.min_samples:
                entry.update(distance=None, verdict="no_verdict",
                             note=f"{v.size} samples in the window, {self.min_samples} needed")
            else:
                distance = drift_distance(v, ref["quantiles"], ref["std"])
                over = distance >= entry["threshold"]
                entry.update(distance=distance, verdict="alert" if over else "ok")
                if over and not self.active[name]:
                    newly.append(name)
                self.active[name] = over
            entry["alert"] = self.active[name]
            entries.append(entry)
        self.counts["checks"] += 1
        self.counts["checks_alerting"] += int(any(self.active.values()))
        self.m.drift_checks_total.labels(asset=self.asset).inc()
        if any(self.active.values()):
            self.m.drift_alerts_total.labels(asset=self.asset).inc()
        candidate = self._request(at_ns, regime, entries, newly) if newly else None
        doc = {"schema": DRIFT_SCHEMA, "asset": self.asset, "ts": at, "window": self.window_text,
               "alert": any(self.active.values()), "signals": entries, "candidate": candidate}
        if self.profile_sha256:
            doc["profile_sha256"] = self.profile_sha256
        if self.store is not None:
            self.store.insert_many("drift_alerts", [
                {"asset_id": self.asset, "at": at, "regime": regime, "signal": e["signal"], "distance": e["distance"],
                 "threshold": e["threshold"], "alert": e["alert"]} for e in entries])
        summary = ", ".join(f"{e['signal']} {'-' if e['distance'] is None else format(e['distance'], '.3f')}"
                            f"/{e['threshold']:g}{' ALERT' if e['alert'] else ''}" for e in entries)
        log.info("drift %s %s: %s", at, regime, summary)
        problems = schemas.validate(DRIFT_SCHEMA, doc)
        if problems:                    # a bug here: say so loudly, publish nothing that breaks the contract
            self.counts["invalid_messages"] += 1
            log.error("drift_v1 at %s does not follow its schema, not published: %s", at, "; ".join(problems))
            return []
        return [Out(DRIFT_TOPIC, canonical_json(doc), retain=True)]

    def _request(self, at_ns: int, regime: str, entries: list[dict], signals: list[str]) -> dict:
        """OPS-06: one candidate training run request per new alert. Deterministic in stream
        time, so a replay writes the same request again rather than a second one."""
        request_id = f"CAND-{self.asset}-{_moment(at_ns).strftime('%Y%m%dT%H%MZ')}"
        request = {
            "schema": REQUEST_SCHEMA, "request_id": request_id, "asset": self.asset,
            "trigger": {"kind": "drift", "at": iso_utc(_moment(at_ns)), "regime": regime,
                        "window": self.window_text,
                        "signals": [{k: e[k] for k in ("signal", "distance", "threshold", "n")}
                                    for e in entries if e["signal"] in signals]},
            "reference_sha256": self.reference_sha256, "profile_sha256": self.profile_sha256,
            "promotion": "none by the pipeline: eval/promote.py needs a recorded team approval (OPS-07, PR-23)",
        }
        if self.requests_dir is not None:
            path = self.requests_dir / f"{request_id}.json"
            request["file"] = str(path)
            request["next_step"] = f"python eval/retrain_candidate.py --request {path} --processed <processed folder>"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(request, indent=1, sort_keys=True) + "\n")
        self.counts["candidate_requests"] += 1
        log.info("new drift alert on %s at %s: candidate request %s", ", ".join(signals), request["trigger"]["at"],
                 request.get("file", request_id))
        return request

    def report(self) -> dict:
        return dict(sorted(self.counts.items()), asset=self.asset,
                    active=[s for s, on in self.active.items() if on])


def default_reference(asset: str) -> Path:
    return ROOT / "eval" / "frozen" / asset / "drift_reference.json"


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile", required=True, help="asset profile YAML")
    p.add_argument("--reference", default=None, help="drift reference (default eval/frozen/<asset>/drift_reference.json)")
    p.add_argument("--broker", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--store", default=None, help="store URL (default $PDM_STORE or sqlite:///runs/pdm.db)")
    p.add_argument("--no-store", action="store_true")
    p.add_argument("--requests", default=None, help="folder for candidate requests (default runs/<asset>/candidates/requests)")
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    logs.setup("drift")
    try:
        profile = load_profile(args.profile)
        asset = profile["asset"]["id"]
        path = Path(args.reference) if args.reference else default_reference(asset)
        reference = load_reference(path)
        service = DriftService(profile, reference, events=load_events(events_file(profile, args.profile)),
                               profile_sha256=profile_hash(args.profile), reference_sha256=sha256_file(path),
                               requests_dir=Path(args.requests or f"runs/{asset}/candidates/requests"))
    except (ProfileError, DriftError) as exc:
        raise SystemExit(f"drift: {exc}")
    built_with = ((reference.get("sources") or {}).get("profile") or {}).get("sha256")
    if built_with and built_with != service.profile_sha256:
        log.info("the reference was built with profile %s..., running with %s...; its drift section is checked",
                 built_with[:12], service.profile_sha256[:12])
    if not args.no_store:
        from pdm_common.store import open_store
        service.store = open_store(args.store)
    log.info("asset %s: %s -> %s (retained), every %s over %s, %d signals, reference %s", asset,
             profile["topics"]["telemetry"], DRIFT_TOPIC, profile["drift"]["every"], profile["drift"]["window"],
             len(service.signals), path)
    run_service("drift", {profile["topics"]["telemetry"]: service.handle_telemetry}, host=args.broker, port=args.port)
    print(json.dumps(service.report(), sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
