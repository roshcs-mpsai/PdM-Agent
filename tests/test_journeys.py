"""The three journeys end to end, in one process (T28, PRD section 6; R2b exit criterion).

Every deployed component runs here except the transports: the replayer, ingest, detector,
prognostics and gate on the in-memory bus (eval/pipeline.py), the agent on each opened incident
(services/agent, with the rules policy of eval/policy.py standing in for the model), the tool
server in process, the HTTP API through starlette's test client, the connector, and the
work-order simulator as the plant's maintenance system.

* Journey A: replay -> incident -> brief -> a reviewer approves -> the work order reaches the
  maintenance system exactly once -> the incident is sent, and the audit rebuilds the decision.
* Journey B: an incident whose evidence is insufficient -> the brief says so -> a reviewer
  rejects -> the incident closes and no work order exists.
* Journey C: a sensor fault injected by the replayer (panel pressure 2 bar low) -> the gate opens
  -> the divergence check fails -> the brief blames the instrument and asks for a sensor check ->
  NE 107 shows out of specification, "diagnosis points to an instrument".

The synthetic versions always run; the ones on MetroPT-3 data need the dataset.
"""
import datetime as dt
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
for sub in ("eval", "services/agent", "services/mcp_server", "services/api", "services/connector",
            "services/replayer", "tools", "tests"):
    sys.path.insert(0, str(ROOT / sub))

import agent as agent_service  # noqa: E402
import auth  # noqa: E402
import connector as connector_service  # noqa: E402
import main as api  # noqa: E402
import replayer  # noqa: E402
import runner as runner_mod  # noqa: E402
import sinks  # noqa: E402
import toolsets  # noqa: E402
import workorder_sim  # noqa: E402
from pipeline import Pipeline  # noqa: E402
from policy import RulesModel  # noqa: E402
from pdm_common import incidents as lifecycle  # noqa: E402
from pdm_common.corpus import Corpus  # noqa: E402
from pdm_common.profile import analog_names, profile_hash, signal_names  # noqa: E402
from pdm_common.skills import load_skills  # noqa: E402
from pdm_common.store import Store  # noqa: E402
from test_pipeline import synthetic  # noqa: E402

PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
RAW = ROOT / "data" / "MetroPT3(AirCompressor).csv"
FAULTS = ROOT / "profiles" / "metropt3_apu" / "faults" / "sensor_faults.yaml"
TOKENS = {"PDM_TOKEN_REVIEWER_ROSH": "r" * 40, "PDM_TOKEN_EVALUATOR": "v" * 40, "PDM_TOKEN_AGENT": "g" * 40}
SIM_TOKEN = "s" * 40
needs_data = pytest.mark.skipif(not RAW.exists(), reason="needs the MetroPT-3 CSV in data/")


class Stack:
    """The deployed components around one store."""

    def __init__(self):
        self.store = Store("sqlite://")
        self.store.init_schema()
        self.pipe = Pipeline(PROFILE_PATH, self.store, compute_every=60)
        server = toolsets.toolserver.ToolServer(self.store, self.pipe.profile, self.pipe.events,
                                                registry=self.pipe.registry, corpus=Corpus.load(ROOT / "corpus"),
                                                frozen=self.pipe.frozen)
        skills = load_skills(self.pipe.profile, ROOT)
        sha = profile_hash(PROFILE_PATH)

        def make_runner():
            return runner_mod.Runner(tools=toolsets.InProcessTools(server),
                                     model=RulesModel(signals=analog_names(self.pipe.profile)),
                                     sink=sinks.DirectSink(self.store), profile=self.pipe.profile,
                                     profile_sha256=sha, skills=skills, survival_run=self.pipe.frozen.survival_run_id)
        self.agent = agent_service.AgentService(profile=self.pipe.profile, sink=sinks.DirectSink(self.store),
                                                make_runner=make_runner, config={})
        self.pipe.attach_agent(self.agent.process)
        self.api = TestClient(api.create_app(self.store, auth.load_tokens(TOKENS)))
        self.sim = TestClient(workorder_sim.create_app(token=SIM_TOKEN))
        self.connector = connector_service.Connector(self.store, "http://testserver/api/work-orders", SIM_TOKEN,
                                                     client=self.sim)

    def get(self, path, who="r"):
        r = self.api.get(path, headers={"Authorization": f"Bearer {who * 40}"})
        assert r.status_code == 200, r.text
        return r.json()

    def decide(self, brief_id, action, reason, **extra):
        r = self.api.post(f"/briefs/{brief_id}/decisions", headers={"Authorization": f"Bearer {'r' * 40}"},
                          json={"action": action, "reason": reason, **extra})
        assert r.status_code == 201, r.text
        return r.json()

    def state(self, incident_id):
        return self.store.select("incidents", {"incident_id": incident_id})[0]["state"]

    def status_after(self, incident_id):
        """The NE 107 statuses the gate logged from the incident's opening on."""
        from pdm_common.store import _to_db
        opened = self.store.select("incidents", {"incident_id": incident_id})[0]["opened_at"]
        return [(r["to_status"], r["cause"]) for r in self.store.query(
            "SELECT * FROM status_log WHERE at >= ? ORDER BY id",
            [_to_db("status_log", "at", opened, self.store.dialect)], table="status_log")]


def journey_a(stack: Stack, incident_id: str) -> dict:
    """Approve the incident's brief and follow the work order to the maintenance system."""
    doc = stack.get(f"/incidents/{incident_id}")
    assert doc["incident"]["state"] == "awaiting_review" and doc["cited_results"]
    decision = stack.decide(doc["brief"]["brief_id"], "approve", "the evidence supports the order")["decision"]
    assert stack.state(incident_id) == "approved"
    assert stack.connector.run_once()["sent"] == 1
    assert stack.connector.run_once()["due"] == 0                       # sent once, nothing left
    orders = stack.sim.get("/api/work-orders", headers={"Authorization": f"Bearer {SIM_TOKEN}"}).json()["orders"]
    mine = [o for o in orders if o["order"]["incident_id"] == incident_id]
    assert len(mine) == 1 and mine[0]["order"]["approval_id"] == decision["decision_id"]
    assert stack.state(incident_id) == "sent"
    audit = stack.get(f"/decisions/{decision['decision_id']}", who="v")
    assert audit["evidence_matches"] and audit["incident"]["consistent"]
    assert audit["incident"]["state_from_events"] == "sent"
    assert [e["to_state"] for e in audit["incident"]["events"]] == ["open", "diagnosing", "awaiting_review",
                                                                   "approved", "sent"]
    return doc


# --- synthetic -------------------------------------------------------------------------------------

def test_journey_a_on_a_synthetic_excursion():
    stack = Stack()
    stack.pipe.feed(synthetic(stack.pipe.profile, 150, anomalous_after=60))
    stack.pipe.flush()
    (incident,) = stack.store.select("incidents")
    assert stack.pipe.agent_runs[0]["outcome"] in ("sufficient", "insufficient")
    doc = journey_a(stack, incident["incident_id"])
    assert doc["trace"]["tool_calls"][0]["tool"] == "run_check"


def test_journey_b_insufficient_evidence_is_rejected_and_closed():
    stack = Stack()
    stack.pipe.feed(synthetic(stack.pipe.profile, 150, anomalous_after=60))
    stack.pipe.flush()
    (incident,) = stack.store.select("incidents")
    iid = incident["incident_id"]
    doc = stack.get(f"/incidents/{iid}")
    assert doc["brief"]["evidence_state"] == "insufficient" and doc["banner"] == "insufficient"
    assert doc["brief"]["document"]["recommended_action"]["type"] in ("monitor", "data_quality_check")
    stack.decide(doc["brief"]["brief_id"], "reject", "nothing to act on: the checks pass")
    assert stack.state(iid) == "closed" and stack.store.count("work_orders") == 0
    assert stack.connector.run_once()["due"] == 0


# --- MetroPT-3 data ---------------------------------------------------------------------------------

@needs_data
def test_journey_a_on_the_f4_lead_up():
    stack = Stack()
    stack.pipe.replay(RAW, from_ts=dt.datetime(2020, 7, 7), to_ts=dt.datetime(2020, 7, 15, 6))
    opened = [i["incident_id"] for i in stack.store.select("incidents", order="opened_at")]
    assert opened == ["INC-metropt3_apu-20200708T1810Z", "INC-metropt3_apu-20200714T0233Z",
                      "INC-metropt3_apu-20200715T0046Z"]
    assert all(r["outcome"] in ("sufficient", "insufficient") for r in stack.pipe.agent_runs)
    journey_a(stack, "INC-metropt3_apu-20200715T0046Z")


@needs_data
def test_journey_c_an_injected_sensor_fault_is_blamed_on_the_instrument():
    stack = Stack()
    faults = replayer.Faults.read(FAULTS, signal_names(stack.pipe.profile))
    stack.pipe.replay(RAW, from_ts=dt.datetime(2020, 6, 25, 12), to_ts=dt.datetime(2020, 7, 2, 16), faults=faults)
    (incident,) = [i for i in stack.store.select("incidents") if i["opened_at"] >= "2020-07-02T08"]
    iid = incident["incident_id"]
    checks = {c["id"]: c["result"] for c in incident["triage"]["checks"]}
    assert checks["C9"] == "fail"
    doc = stack.get(f"/incidents/{iid}")
    brief = doc["brief"]["document"]
    assert brief["hypotheses"][0]["category"] == "instrumentation" and brief["hypotheses"][0]["likelihood"] == "likely"
    assert brief["recommended_action"]["type"] == "sensor_check" and brief["work_order"]["priority"] == "medium"
    statuses = stack.status_after(iid)
    assert ("out_of_specification", "diagnosis points to an instrument") in statuses
    assert lifecycle.history(stack.store, iid)[-1]["to_state"] == "awaiting_review"
    stack.decide(doc["brief"]["brief_id"], "approve", "check the panel pressure sensor")
    assert stack.connector.run_once()["sent"] == 1 and stack.state(iid) == "sent"
