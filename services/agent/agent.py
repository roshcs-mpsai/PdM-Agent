"""Agent service -- diagnose every incident the gate opens (T16; AGT-06 to AGT-08, AGT-12, INC-06).

    python services/agent/agent.py --profile profiles/metropt3_apu/asset_profile.yaml
        [--store "$PDM_STORE"]                    records straight to the store (default), or
        [--api http://localhost:8000]             through the HTTP API with $PDM_TOKEN_AGENT
        [--tools http://localhost:8765/mcp]       the MCP tool server (default: its code in process)
        [--config services/agent/config.yaml] [--arm full|retrieval_only] [--scripted steps.json]
    ... --once INC-<asset>-<time>                 diagnose one incident and exit
    ... --study INC-... --arm retrieval_only      a study brief: no claim, the lifecycle untouched (T34)

The service subscribes to the profile's incidents topic. An ``opened`` incident_v1 queues its
incident; one worker thread diagnoses queued incidents one at a time (services/agent/runner.py),
so a run that takes minutes never blocks the MQTT connection. Every 30 s it also queues the
asset's incidents still open in the store: ones whose message it missed while it was down, and
ones handed back because the model could not be reached, once their wait is over (3 tries over
15 min by default; after the last, a brief written without the model, so the planner always
sees the alarm). On start it resumes runs that stopped mid-way (their checkpoints).

The model is claude-opus-5-5 through the Messages API ($ANTHROPIC_API_KEY); ``--scripted``
replays fixed answers instead (a JSON list of steps, each a list of content blocks), for dry
runs without a key.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import queue
import sys
import threading
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import llm  # noqa: E402
import runner as runner_mod  # noqa: E402
import sinks  # noqa: E402
import toolsets  # noqa: E402
from pdm_common import logs, metrics, tracing  # noqa: E402
from pdm_common.mqtt import Out, run_service  # noqa: E402
from pdm_common.profile import load_profile, profile_hash, read_yaml  # noqa: E402
from pdm_common.skills import load_skills  # noqa: E402
from pdm_common.timeutil import to_utc  # noqa: E402

log = logging.getLogger("agent")
CONFIG = HERE / "config.yaml"
TICK_S = 30.0


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def load_config(path=None) -> dict:
    return read_yaml(path or CONFIG, what="agent config")


class AgentService:
    """The service minus MQTT: incident messages and ticks in, agent runs out.
    ``make_runner()``: a Runner for this service's arm (a new one per run)."""

    def __init__(self, *, profile: dict, sink, make_runner: Callable[[], "runner_mod.Runner"], config: dict,
                 now: Callable[[], dt.datetime] = _now):
        self.asset = profile["asset"]["id"]
        self.topic = profile["topics"]["incidents"]
        self.sink = sink
        self.make_runner = make_runner
        self.retry_after_min = [float(m) for m in (config.get("outage") or {}).get("retry_after_min", [5, 10])]
        self.now = now
        self.queue: "queue.Queue[str]" = queue.Queue()
        self._queued: set = set()
        self._lock = threading.Lock()
        self.counts: Counter = Counter()

    # -- inputs ----------------------------------------------------------------------------------------
    def handle_incident(self, payload) -> list[Out]:
        try:
            doc = json.loads(payload)
        except ValueError:
            log.warning("not JSON on %s", self.topic)
            return []
        if isinstance(doc, dict) and doc.get("schema") == "incident_v1" and doc.get("event") == "opened" \
                and doc.get("asset") == self.asset and doc.get("incident_id"):
            self.enqueue(doc["incident_id"])
        return []

    def tick(self) -> list[Out]:
        for incident_id in self.due():
            self.enqueue(incident_id)
        return []

    def enqueue(self, incident_id: str) -> bool:
        with self._lock:
            if incident_id in self._queued:
                return False
            self._queued.add(incident_id)
        self.queue.put(incident_id)
        self.counts["queued"] += 1
        return True

    def due(self, now: Optional[dt.datetime] = None) -> list[str]:
        """Open incidents to diagnose now: never tried, or handed back and past their wait."""
        now = now or self.now()
        out = []
        for incident_id in self.sink.open_incidents(self.asset):
            tries = self.sink.outage_tries(incident_id)
            if not tries:
                out.append(incident_id)
                continue
            n = len(tries)
            wait = self.retry_after_min[n - 1] if n <= len(self.retry_after_min) else 0.0
            if now >= to_utc(tries[-1]["finished_at"]) + dt.timedelta(minutes=wait):
                out.append(incident_id)
        return out

    # -- runs ------------------------------------------------------------------------------------------
    def process(self, incident_id: str) -> dict:
        """Diagnose one incident: with the model, or without it after the last outage try."""
        tries = self.sink.outage_tries(incident_id)
        runner = self.make_runner()
        if len(tries) > len(self.retry_after_min):
            reason = (f"the language model could not be reached in {len(tries)} tries; the checks ran without it")
            state = runner.run(incident_id, without_model=reason)
        else:
            state = runner.run(incident_id)
        self.counts[state["outcome"] or "error"] += 1
        return state

    def resume_unfinished(self) -> list[dict]:
        """Carry on the runs that stopped before writing their trace."""
        done = []
        for cp in self.sink.unfinished(self.asset):
            done.append(self.make_runner().resume(cp["state"]))
        return done

    def work(self, stop: threading.Event) -> None:
        """The worker thread: one queued incident at a time until ``stop``."""
        while not stop.is_set():
            try:
                incident_id = self.queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self.process(incident_id)
            except Exception:                     # one incident must not stop the worker
                log.exception("diagnosing %s failed", incident_id, extra={"incident_id": incident_id})
            finally:
                with self._lock:
                    self._queued.discard(incident_id)

    def report(self) -> dict:
        return dict(self.counts, asset=self.asset, queued=self.queue.qsize())


# --- wiring ---------------------------------------------------------------------------------------

def make_model(cfg: dict, scripted: Optional[str] = None):
    if scripted:
        steps = json.loads(Path(scripted).read_text())
        return llm.ScriptedModel(steps, name=f"scripted:{Path(scripted).name}", then="fail")
    m = cfg["model"]
    if m.get("provider", "anthropic") != "anthropic":
        raise SystemExit(f"agent: unknown model provider {m.get('provider')!r}")
    return llm.AnthropicModel(m["name"], max_tokens=m.get("max_tokens", 8000), effort=m.get("effort", "medium"),
                              timeout_s=m.get("timeout_s", 60), retries=m.get("retries", 2))


def make_factory(*, profile: dict, profile_path, cfg: dict, arm: str, study: bool, tools, sink, model,
                 survival_run: Optional[str] = None) -> Callable[[], "runner_mod.Runner"]:
    skills = load_skills(profile, ROOT)
    budget = runner_mod.Budget.from_config(cfg.get("budget") or {})
    sha = profile_hash(profile_path)

    def factory():
        return runner_mod.Runner(tools=tools, model=model, sink=sink, profile=profile, profile_sha256=sha,
                                 skills=skills, budget=budget, arm=arm,
                                 strict_tools=bool(cfg["model"].get("strict_tools", True)),
                                 survival_run=survival_run, study=study)
    return factory


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile", required=True)
    p.add_argument("--config", default=None, help="agent config (default services/agent/config.yaml)")
    p.add_argument("--store", default=None, help="store URL (default $PDM_STORE or sqlite:///runs/pdm.db)")
    p.add_argument("--api", default=os.environ.get("PDM_API_URL"),
                   help="write through the HTTP API at this URL (token $PDM_TOKEN_AGENT) instead of the store")
    p.add_argument("--tools", default=os.environ.get("PDM_MCP_URL"),
                   help="the MCP tool server's URL (…/mcp); default: the tool code in this process")
    p.add_argument("--arm", choices=("full", "retrieval_only"), default=None)
    p.add_argument("--scripted", default=None, help="replay fixed model answers from this JSON file")
    p.add_argument("--once", metavar="INCIDENT_ID", default=None, help="diagnose one incident and exit")
    p.add_argument("--study", metavar="INCIDENT_ID", default=None,
                   help="write a study brief for one incident (no claim, no lifecycle move) and exit")
    p.add_argument("--broker", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logs.setup("agent")
    tracing.configure("agent")                 # OTLP to $OTEL_EXPORTER_OTLP_ENDPOINT when the SDK is installed
    cfg = load_config(args.config)
    arm = args.arm or cfg.get("arm", "full")
    profile = load_profile(args.profile)
    token = os.environ.get("PDM_TOKEN_AGENT")
    try:
        model = make_model(cfg, args.scripted)
    except llm.ModelError as exc:
        raise SystemExit(f"agent: {exc}; set it, or use --scripted for a dry run")
    survival_run = None
    if args.api:
        sink = sinks.ApiSink(args.api, token)
    else:
        from pdm_common.store import open_store
        sink = sinks.DirectSink(open_store(args.store))
    if args.tools:
        tools = toolsets.McpTools(args.tools, token)
    else:
        sys.path.insert(0, str(ROOT / "services" / "mcp_server"))
        import server as mcp_server
        server_tools = mcp_server.make_tools(args.profile, args.store)
        survival_run = server_tools.frozen.survival_run_id if server_tools.frozen else None
        tools = toolsets.InProcessTools(server_tools)
    factory = make_factory(profile=profile, profile_path=args.profile, cfg=cfg, arm=arm, study=bool(args.study),
                           tools=tools, sink=sink, model=model, survival_run=survival_run)
    service = AgentService(profile=profile, sink=sink, make_runner=factory, config=cfg)
    try:
        if args.once or args.study:
            state = service.process(args.once) if args.once else factory().run(args.study)
            print(json.dumps({k: state[k] for k in ("incident_id", "trace_id", "arm", "outcome", "brief_id",
                                                    "tool_calls", "turns", "elapsed_s", "budget_hit")},
                             sort_keys=True))
            return 0 if state["outcome"] not in ("error",) else 1
        for state in service.resume_unfinished():
            log.info("resumed %s: %s", state["trace_id"], state["outcome"])
        stop = threading.Event()
        worker = threading.Thread(target=service.work, args=(stop,), name="agent-worker", daemon=True)
        worker.start()
        service.tick()
        log.info("asset %s: %s -> agent runs (%s arm, model %s)", service.asset, service.topic, arm, model.name)
        try:
            run_service("agent", {service.topic: service.handle_incident}, host=args.broker, port=args.port,
                        tick=(TICK_S, service.tick))
        finally:
            stop.set()
            worker.join(timeout=5)
        print(json.dumps(service.report(), sort_keys=True))
        return 0
    finally:
        tools.close()


if __name__ == "__main__":
    sys.exit(main())
