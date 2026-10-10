"""The stack in one process: replayer -> ingest -> detector -> prognostics -> gate (T28).

    python eval/pipeline.py --data "data/MetroPT3(AirCompressor).csv" --from 2020-07-07 --to 2020-07-16 \\
        --store sqlite:///runs/replay.db

The evaluation runs the deployed path in replay mode (FRD section 10): the same service
classes the MQTT stack runs, on an in-memory bus instead of a broker, writing the same
store. Messages are delivered in publication order, depth first, so a window's score
reaches the gate before the next window is published, exactly the order the broker
gives one consumer at QoS 1. Retained topics keep their last message, as Mosquitto does.

Anything else -- the API's connector, the OPC UA server, the drift job -- joins with
``subscribe(topic, handler)``; a handler takes a payload and returns Outs, which go back
on the bus. ``attach_agent(process)`` runs the agent on each incident the gate opens,
synchronously, before the next window: the brief is in the store when the gate computes
the next status, as it would be a few seconds later in the stack.

``Pipeline.replay`` uses the replayer's own ``replay`` (with fault injection and its
manifest), and ingest computes windows once an hour of data has arrived
(``compute_every``), which releases the same windows faster than one at a time.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Callable, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
for _svc in ("replayer", "ingest", "detector", "gate", "prognostics", "drift"):
    sys.path.insert(0, str(ROOT / "services" / _svc))

from pdm_common.checks import load_registry  # noqa: E402
from pdm_common.events import load_events  # noqa: E402
from pdm_common.mqtt import Out  # noqa: E402
from pdm_common.profile import events_file, load_profile, profile_hash  # noqa: E402

DRIFT_TOPIC = "pdm/_sys/drift"


class Bus:
    """Topic -> handlers, delivered in order; retained topics keep their last payload."""

    def __init__(self, record: bool = False):
        self.handlers: dict[str, list[Callable]] = defaultdict(list)
        self.retained: dict[str, str] = {}
        self.counts: dict[str, int] = defaultdict(int)
        self.log: Optional[list] = [] if record else None
        self._queue: deque = deque()
        self._busy = False

    def subscribe(self, topic: str, handler: Callable[[str], Iterable[Out]]) -> None:
        self.handlers[topic].append(handler)

    def publish(self, out: Out) -> None:
        self._queue.append(out)
        if self._busy:
            return
        self._busy = True
        try:
            while self._queue:
                o = self._queue.popleft()
                self.counts[o.topic] += 1
                if o.retain:
                    self.retained[o.topic] = o.payload
                if self.log is not None:
                    self.log.append((o.topic, o.payload))
                for handler in self.handlers.get(o.topic, ()):
                    for nxt in handler(o.payload) or ():
                        self._queue.append(nxt)
        finally:
            self._busy = False

    def messages(self, topic: str) -> list[dict]:
        return [json.loads(p) for t, p in (self.log or []) if t == topic]


class Pipeline:
    def __init__(self, profile_path, store, *, prognostics: bool = True, gate: bool = True, drift: bool = False,
                 record: bool = False, compute_every: Optional[int] = 360):
        import detector
        import gate as gate_service
        import ingest
        import prognosis

        self.profile_path = Path(profile_path)
        self.profile = load_profile(profile_path)
        self.events = load_events(events_file(self.profile, profile_path))
        self.store = store
        sha = profile_hash(profile_path)
        self.topics = self.profile["topics"]
        self.bus = Bus(record=record)
        self.ingest = ingest.Ingest(self.profile, sha, profile_path=self.profile_path, store=store,
                                    compute_every=compute_every)
        self.frozen = detector.load_for_profile(self.profile, self.profile_path)
        self.detector = detector.Detector(self.profile, self.frozen, sha, store)
        self.bus.subscribe(self.topics["telemetry"], self.ingest.handle)
        self.bus.subscribe(self.topics["windows"], self.detector.handle)
        self.prognostics = self.gate = self.drift = None
        if prognostics:
            self.prognostics = prognosis.Prognostics(self.profile, self.frozen, sha, store)
            self.bus.subscribe(self.topics["windows"], self.prognostics.handle_window)
            self.bus.subscribe(self.topics["scores"], self.prognostics.handle_score)
        if gate:
            self.registry = load_registry(self.profile, ROOT)
            self.gate = gate_service.GateService(self.profile, sha, events=self.events, registry=self.registry,
                                                 store=store)
            self.bus.subscribe(self.topics["windows"], self.gate.handle_window)
            self.bus.subscribe(self.topics["scores"], self.gate.handle_score)
            self.bus.subscribe(self.topics["prognosis"], self.gate.handle_prognosis)
            self.bus.subscribe(DRIFT_TOPIC, self.gate.handle_drift)
        if drift:
            import drift as drift_service
            reference = drift_service.load_reference(drift_service.default_reference(self.profile["asset"]["id"]))
            self.drift = drift_service.DriftService(self.profile, reference, store, events=self.events)
            self.bus.subscribe(self.topics["telemetry"], self.drift.handle_telemetry)

    def subscribe(self, topic: str, handler) -> None:
        self.bus.subscribe(topic, handler)

    def attach_agent(self, process: Callable[[str], dict]) -> None:
        """Run ``process(incident_id)`` (services/agent AgentService.process, or a Runner's run)
        on every incident the gate opens; the states it returns are kept in ``agent_runs``."""
        self.agent_runs: list[dict] = []

        def on_incident(payload) -> list:
            doc = json.loads(payload)
            if doc.get("event") == "opened" and doc.get("incident_id"):
                self.agent_runs.append(process(doc["incident_id"]))
            return []
        self.bus.subscribe(self.topics["incidents"], on_incident)

    def feed(self, payloads: Iterable[str]) -> None:
        for p in payloads:
            self.bus.publish(Out(self.topics["telemetry"], p))

    def flush(self) -> None:
        for out in self.ingest.flush():
            self.bus.publish(out)

    def replay(self, data_path, *, from_ts=None, to_ts=None, faults=None, manifest_path=None, log_path=None) -> dict:
        import replayer
        counts = replayer.replay(self.profile, data_path, lambda topic, payload: self.bus.publish(Out(topic, payload)),
                                 speed=1e12, from_ts=from_ts, to_ts=to_ts, faults=faults, manifest_path=manifest_path,
                                 profile_path=self.profile_path, log_path=log_path, max_wait=0)
        self.flush()
        return counts

    def report(self) -> dict:
        out = {"ingest": self.ingest.reconciliation(), "detector": self.detector.report()}
        if self.gate:
            out["gate"] = self.gate.report()
        if self.prognostics:
            out["prognostics"] = dict(self.prognostics.counts)
        if self.drift:
            out["drift"] = self.drift.report()
        return out


def main(argv=None) -> int:
    import datetime as dt
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile", default=str(ROOT / "profiles/metropt3_apu/asset_profile.yaml"))
    p.add_argument("--data", required=True)
    p.add_argument("--from", dest="from_ts", type=dt.datetime.fromisoformat)
    p.add_argument("--to", dest="to_ts", type=dt.datetime.fromisoformat)
    p.add_argument("--store", default="sqlite:///runs/replay.db")
    p.add_argument("--drift", action="store_true")
    args = p.parse_args(argv)
    from pdm_common.store import open_store
    pipe = Pipeline(args.profile, open_store(args.store), drift=args.drift)
    counts = pipe.replay(args.data, from_ts=args.from_ts, to_ts=args.to_ts)
    print(json.dumps({"replayer": counts, **pipe.report()}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
