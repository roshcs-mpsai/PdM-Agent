"""Gate -- persistence gate, incidents and NE 107 status (T12b; INC-01 to INC-07).

Subscribes to the profile's scores, windows and prognosis topics and to the
system drift topic. For every score it:

1. feeds the persistence gate (pdm_common.gate.StreamingGate, the harness's
   gate_passes/incidents one window at a time): at least ``gate.min_fraction``
   of the windows in the last ``gate.span`` alerting, with at least half of
   them present, opens an incident; later passes attach to it until the
   episode has been quiet for ``gate.hold_off`` (INC-01, INC-03, INC-04);
2. logs every decision -- open, attach, suppress, close -- with the gate
   parameters and the score trajectory behind it (gate_log, INC-02);
3. on opening, runs the asset's checks on the data as of the opening
   (pdm_common.asof, checks/<asset>/) and stores their results and the triage
   rules' verdicts with the incident, as evidence; no rule dismisses (T36);
   then publishes incident_v1 ``opened``, which the agent answers once per
   incident (INC-06);
4. derives the NE 107 status (pdm_common.status.ne107) from the score, the
   gate, the incident and its diagnosis, the prognosis, drift and
   out-of-range readings, publishes status_v1 (retained) and logs every change
   with its cause (status_log, INC-07, INT-03).

``tick`` (every few seconds of wall time) turns the status to function check
when no window has arrived for ``ne107.no_data_after``.

    python services/gate/gate.py --profile profiles/metropt3_apu/asset_profile.yaml
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]

from pdm_common import incidents as lifecycle  # noqa: E402
from pdm_common import logs, metrics  # noqa: E402
from pdm_common.asof import evidence_asof  # noqa: E402
from pdm_common.checks import Evidence, load_registry  # noqa: E402
from pdm_common.events import load_events  # noqa: E402
from pdm_common.gate import GateEvent, StreamingGate, gate_params  # noqa: E402
from pdm_common.hashing import canonical_json  # noqa: E402
from pdm_common.mqtt import Out, run_service  # noqa: E402
from pdm_common.profile import ProfileError, events_file, load_profile, profile_hash  # noqa: E402
from pdm_common.status import NE107_STATES, StatusInputs, ne107  # noqa: E402
from pdm_common.timeutil import iso_utc, parse_duration, to_utc  # noqa: E402

log = logging.getLogger("gate")

INCIDENT_SCHEMA = "incident_v1"
STATUS_SCHEMA = "status_v1"
DRIFT_TOPIC = "pdm/_sys/drift"
FAILURE_SIGNATURE = "continuous_load_signature"     # the registry check whose failure means failure (FRD 8)
DIAGNOSIS = {"process": "process", "device": "process", "instrumentation": "instrumentation",
             "data_quality": "instrumentation"}


def _iso_ns(ns: int) -> str:
    return iso_utc(dt.datetime.fromtimestamp(ns / 1e9, tz=dt.timezone.utc))


def _ns(text: str) -> int:
    moment = to_utc(text)
    return int(moment.timestamp()) * 10**9 + moment.microsecond * 1000


def incident_id(asset: str, opened_ns: int) -> str:
    """Deterministic from stream time, so a replay into the same store opens the same ids."""
    return f"INC-{asset}-" + dt.datetime.fromtimestamp(opened_ns / 1e9, tz=dt.timezone.utc).strftime("%Y%m%dT%H%MZ")


class GateService:
    """The service minus MQTT: scores, windows, prognoses and drift in; incidents and status out."""

    def __init__(self, profile: dict, profile_sha256: str, *, events: dict, registry=None, store=None,
                 clock: Callable[[], float] = time.monotonic):
        self.profile = profile
        self.asset = profile["asset"]["id"]
        self.params = gate_params(profile)
        self.gate = StreamingGate(self.params)
        self.events = events
        self.registry = registry
        self.store = store
        self.profile_sha256 = profile_sha256
        self.ne107_params = dict(profile.get("ne107") or {})
        self.no_data_after_s = parse_duration(self.ne107_params.get("no_data_after", "5min")).total_seconds()
        self.topics = profile["topics"]
        self.clock = clock
        self.last_input = clock()
        self.latest: dict = {}
        self.window_flags: list[str] = []
        self.continuous_load = False
        self.prognosis: Optional[dict] = None
        self.drift = False
        self.incident: Optional[dict] = None          # the latest incident this gate opened
        self.status: Optional[tuple[str, tuple]] = None
        self.counts = {"scores": 0, "opened": 0, "attached": 0, "suppressed": 0, "closed": 0, "status_changes": 0}
        self.m = metrics.service_metrics("gate")
        for decision in ("open", "attach", "suppress", "close"):
            self.m.gate_decisions_total.labels(asset=self.asset, decision=decision)
        for state in NE107_STATES:
            self.m.ne107_status.labels(asset=self.asset, status=state).set(0)
            self.m.ne107_status_changes_total.labels(asset=self.asset, to=state)

    # -- inputs -------------------------------------------------------------------------------
    def handle_window(self, payload) -> list[Out]:
        """Out-of-range readings and the failure signature of the latest window."""
        try:
            w = json.loads(payload)
        except ValueError:
            return []
        if w.get("schema") != "window_v1" or w.get("asset") != self.asset:
            return []
        self.last_input = self.clock()
        self.window_flags = sorted((w.get("out_of_range") or {}).keys())
        self.continuous_load = self._signature(w.get("features") or {})
        return []

    def _signature(self, features: dict) -> bool:
        """Does the registry's continuous-load check fail on these window features? It reads
        only the window, so an empty grid will do."""
        if self.registry is None:
            return False
        check_id = next((cid for cid, fn in self.registry.checks.items() if fn.__name__ == FAILURE_SIGNATURE), None)
        if check_id is None:
            return False
        empty = pd.DataFrame(columns=["observed", "bridged"], index=pd.DatetimeIndex([], tz="UTC"))
        ev = Evidence(pd.Timestamp("2000-01-01", tz="UTC"), empty, features, profile=self.profile, events=self.events)
        return self.registry.checks[check_id](ev).result == "fail"

    def handle_prognosis(self, payload) -> list[Out]:
        try:
            p = json.loads(payload)
        except ValueError:
            return []
        if p.get("schema") == "prognosis_v1" and p.get("asset") == self.asset:
            self.prognosis = p
        return []

    def handle_drift(self, payload) -> list[Out]:
        try:
            d = json.loads(payload)
        except ValueError:
            return []
        if d.get("schema") == "drift_v1" and d.get("asset") == self.asset:
            self.drift = bool(d.get("alert"))
        return []

    def handle_score(self, payload) -> list[Out]:
        try:
            s = json.loads(payload)
        except ValueError:
            return []
        if s.get("schema") != "score_v1" or s.get("asset") != self.asset:
            return []
        self.last_input = self.clock()
        self.counts["scores"] += 1
        self.latest = s
        outs = []
        for event in self.gate.add(_ns(s["window_end"]), s["score"], s["alert"]):
            outs += self._on_event(event, s)
        return outs + self._publish_status(s["window_end"])

    def tick(self) -> list[Out]:
        """Wall-clock check: no window for longer than no_data_after is a function check."""
        if self.latest and self.clock() - self.last_input > self.no_data_after_s:
            return self._publish_status(self.latest.get("window_end"), no_data=True)
        return []

    # -- gate decisions -----------------------------------------------------------------------
    def _log(self, event: GateEvent, incident: Optional[str]) -> None:
        if self.store is None:
            return
        self.store.insert("gate_log", {
            "asset_id": self.asset, "at": _iso_ns(event.at_ns), "decision": event.kind, "incident_id": incident,
            "params": self.params.as_dict(),
            "trajectory": [{"window_end": _iso_ns(t), "score": sc, "alert": a} for t, sc, a in event.trajectory]})

    def _on_event(self, event: GateEvent, score: dict) -> list[Out]:
        self.m.gate_decisions_total.labels(asset=self.asset, decision=event.kind).inc()
        if event.kind == "open":
            return self._open(event, score)
        current = self.incident["incident_id"] if self.incident else None
        self._log(event, current if event.kind != "suppress" else None)
        if event.kind == "suppress":
            self.counts["suppressed"] += 1
            return []
        if event.kind == "attach":
            self.counts["attached"] += 1
            if self.store is not None and current:
                self.store.update("incidents", {"incident_id": current}, {"last_pass_at": _iso_ns(event.at_ns)})
            return []
        # close: the episode went quiet for the hold-off; the lifecycle carries on
        self.counts["closed"] += 1
        if self.store is not None and current:
            self.store.update("incidents", {"incident_id": current},
                              {"episode_closed_at": _iso_ns(event.at_ns), "last_pass_at": _iso_ns(event.at_ns
                                                                                                 - self.params.hold_off_ns)})
        return [self._incident_message("episode_closed", event, current)]

    def _evidence(self, opened_iso: str) -> dict:
        """The checks on the data as of the opening, and the triage rules' verdicts."""
        if self.registry is None or self.store is None:
            return {}
        try:
            ev = evidence_asof(self.store, self.profile, self.events, self.asset, opened_iso)
            results = self.registry.run(ev)
            return {"checks": [r.as_dict() for r in results], "triage": self.registry.triage(results)}
        except Exception as exc:                       # evidence must never stop an alarm
            log.exception("checks failed at %s", opened_iso)
            return {"error": f"{type(exc).__name__}: {exc}"}

    def _open(self, event: GateEvent, score: dict) -> list[Out]:
        opened = _iso_ns(event.at_ns)
        iid = incident_id(self.asset, event.at_ns)
        cause = (f"gate passed: {event.alerting} of {event.present} windows over the threshold in the last "
                 f"{self.params.as_dict()['span']}")
        self.incident = {"incident_id": iid, "opened_at": opened}
        self.counts["opened"] += 1
        self.m.incidents_opened_total.labels(asset=self.asset).inc()
        evidence = self._evidence(opened)
        if self.store is not None:
            created = lifecycle.open_incident(self.store, {
                "incident_id": iid, "asset_id": self.asset, "opened_at": opened, "as_of": opened,
                "gate": dict(self.params.as_dict(), alerting=event.alerting, present=event.present),
                "ne107": None, "triage": evidence or None, "last_pass_at": opened,
                "profile_sha256": self.profile_sha256, "detector_run_id": score.get("detector_run_id")},
                cause=cause, at=opened)
            if not created:
                log.info("incident %s already in the store (a replay); not opened again", iid)
        self._log(event, iid)
        log.info("incident %s opened at %s: %s", iid, opened, cause, extra={"incident_id": iid, "asset": self.asset})
        return [self._incident_message("opened", event, iid, cause=cause, evidence=evidence)]

    def _incident_message(self, kind: str, event: GateEvent, iid: Optional[str], **extra) -> Out:
        doc = {"schema": INCIDENT_SCHEMA, "asset": self.asset, "event": kind, "incident_id": iid,
               "at": _iso_ns(event.at_ns), "as_of": self.incident["opened_at"] if self.incident else None,
               "gate": self.params.as_dict(), "profile_sha256": self.profile_sha256,
               "detector_run_id": self.latest.get("detector_run_id"),
               "trajectory": [{"window_end": _iso_ns(t), "score": sc, "alert": a} for t, sc, a in event.trajectory]}
        if "cause" in extra:
            doc["cause"] = extra["cause"]
        if extra.get("evidence"):
            doc["checks"] = [{k: c[k] for k in ("id", "check", "kind", "result")} for c in extra["evidence"].get("checks", [])]
        return Out(self.topics["incidents"], canonical_json(doc))

    # -- status ---------------------------------------------------------------------------------
    def _incident_state(self) -> tuple[bool, Optional[str], Optional[str]]:
        """(incident open, its state, its diagnosis) from the store."""
        if not self.incident:
            return False, None, None
        iid = self.incident["incident_id"]
        if self.store is None:
            return self.gate.episode_open, "open" if self.gate.episode_open else None, None
        row = self.store.one("SELECT state FROM incidents WHERE incident_id = ?", [iid])
        state = row["state"] if row else None
        brief = self.store.one("SELECT document, evidence_state FROM briefs WHERE incident_id = ? AND valid = ? "
                               "ORDER BY created_at DESC LIMIT 1", [iid, True if self.store.dialect == "postgres" else 1],
                               table="briefs")
        diagnosis = None
        if brief:
            if brief["evidence_state"] != "sufficient":
                diagnosis = "insufficient"
            else:
                hyps = (brief["document"] or {}).get("hypotheses") or []
                lead = next((h for h in hyps if h.get("likelihood") == "likely"), hyps[0] if hyps else None)
                diagnosis = DIAGNOSIS.get((lead or {}).get("category"), "process")
        return state not in (None, "closed"), state, diagnosis

    def _publish_status(self, ts, no_data: bool = False) -> list[Out]:
        open_, state, diagnosis = self._incident_state()
        horizons = (self.prognosis or {}).get("horizons") or {}
        x = StatusInputs(alert=bool(self.latest.get("alert")), episode_open=self.gate.episode_open,
                         incident_open=open_, diagnosis=diagnosis, continuous_load=self.continuous_load,
                         p24_lower=(horizons.get("24") or {}).get("lower"),
                         p72_upper=(horizons.get("72") or {}).get("upper"),
                         drift=self.drift, out_of_range=self.window_flags, no_data=no_data)
        status, causes = ne107(x, self.ne107_params)
        doc = {"schema": STATUS_SCHEMA, "asset": self.asset, "ts": ts, "ne107": status, "causes": causes,
               "score": self.latest.get("score"), "threshold": self.latest.get("threshold"),
               "alert": self.latest.get("alert"), "detector_run_id": self.latest.get("detector_run_id"),
               "incident_id": self.incident["incident_id"] if open_ else None, "incident_state": state if open_ else None,
               "episode_open": self.gate.episode_open, "prognosis": horizons or None,
               "profile_sha256": self.profile_sha256}
        key = (status, tuple(causes))
        for state in NE107_STATES:
            self.m.ne107_status.labels(asset=self.asset, status=state).set(int(state == status))
        if self.status is None or self.status[0] != status:
            self.counts["status_changes"] += 1
            self.m.ne107_status_changes_total.labels(asset=self.asset, to=status).inc()
            if self.store is not None:
                self.store.insert("status_log", {"asset_id": self.asset, "at": ts, "from_status":
                                                 self.status[0] if self.status else None, "to_status": status,
                                                 "cause": "; ".join(causes) or "no condition holds"})
            log.info("status %s -> %s at %s (%s)", self.status[0] if self.status else None, status, ts,
                     "; ".join(causes) or "-")
        self.status = key
        return [Out(self.topics["status"], canonical_json(doc), retain=True)]

    def report(self) -> dict:
        return dict(self.counts, asset=self.asset, status=self.status[0] if self.status else None,
                    episode_open=self.gate.episode_open)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile", required=True, help="asset profile YAML")
    p.add_argument("--broker", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--store", default=None, help="store URL (default $PDM_STORE or sqlite:///runs/pdm.db)")
    p.add_argument("--tick", type=float, default=5.0, help="seconds between no-data checks")
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    logs.setup("gate")
    try:
        profile = load_profile(args.profile)
    except ProfileError as exc:
        raise SystemExit(str(exc))
    from pdm_common.store import open_store
    store = open_store(args.store)
    events = load_events(events_file(profile, args.profile))
    service = GateService(profile, profile_hash(args.profile), events=events,
                          registry=load_registry(profile, ROOT), store=store)
    topics = profile["topics"]
    log.info("asset %s: %s, %s, %s, %s -> %s, %s", service.asset, topics["scores"], topics["windows"],
             topics["prognosis"], DRIFT_TOPIC, topics["incidents"], topics["status"])
    run_service("gate", {topics["scores"]: service.handle_score, topics["windows"]: service.handle_window,
                         topics["prognosis"]: service.handle_prognosis, DRIFT_TOPIC: service.handle_drift},
                host=args.broker, port=args.port, tick=(args.tick, service.tick))
    print(json.dumps(service.report(), sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
