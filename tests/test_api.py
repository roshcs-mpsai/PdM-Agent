"""The HTTP API (T20; HIL-01 to HIL-08, INT-05, BRF-02).

Real incidents from a synthetic excursion, briefs from the real agent runner with a scripted
model, and the API in process (starlette's TestClient): who may call what, the read views, a
decision of each kind with its record and work order, the audit rebuild, the agent's endpoints
used by its API sink, and the negative test that no route but a decision creates a work order.
"""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
for sub in ("eval", "services/api", "services/agent", "services/mcp_server", "tests", "tools"):
    sys.path.insert(0, str(ROOT / sub))

import auth  # noqa: E402
import main as api  # noqa: E402
import runner as runner_mod  # noqa: E402
import sinks  # noqa: E402
import toolsets  # noqa: E402
from llm import ScriptedModel, call  # noqa: E402
from pipeline import Pipeline  # noqa: E402
from pdm_common import incidents as lifecycle  # noqa: E402
from pdm_common.corpus import Corpus  # noqa: E402
from pdm_common.skills import load_skills  # noqa: E402
from pdm_common.store import Store  # noqa: E402
from test_agent import brief_from, submit  # noqa: E402
from test_pipeline import synthetic  # noqa: E402

PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
ENV = {"PDM_TOKEN_REVIEWER_ROSH": "r" * 40, "PDM_TOKEN_REVIEWER_ASHOK": "a" * 40, "PDM_TOKEN_ENGINEER": "e" * 40,
       "PDM_TOKEN_OPERATOR": "o" * 40, "PDM_TOKEN_EVALUATOR": "v" * 40, "PDM_TOKEN_AGENT": "g" * 40}
AS = {"rosh": "r" * 40, "ashok": "a" * 40, "engineer": "e" * 40, "operator": "o" * 40, "evaluator": "v" * 40,
      "agent": "g" * 40}


def h(who):
    return {"Authorization": f"Bearer {AS[who]}"}


@pytest.fixture(scope="module")
def world():
    store = Store("sqlite://")
    store.init_schema()
    pipe = Pipeline(PROFILE_PATH, store, compute_every=60)
    pipe.feed(synthetic(pipe.profile, 200, anomalous_after=60))
    pipe.flush()
    server = toolsets.toolserver.ToolServer(store, pipe.profile, pipe.events, registry=pipe.registry,
                                            corpus=Corpus.load(ROOT / "corpus"), frozen=pipe.frozen)
    client = TestClient(api.create_app(store, auth.load_tokens(ENV)))
    return store, pipe, server, client


_minute = [100]


def diagnosed(world, sink=None, steps=None, state="sufficient") -> tuple[str, dict]:
    """A new incident, diagnosed by the runner (scripted): awaiting review."""
    store, pipe, server, _ = world
    _minute[0] += 1
    at = dt.datetime(2020, 6, 1, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=_minute[0])
    iid = f"INC-{pipe.profile['asset']['id']}-{at:%Y%m%dT%H%MZ}"
    lifecycle.open_incident(store, {"incident_id": iid, "asset_id": pipe.profile["asset"]["id"], "opened_at": at,
                                    "as_of": at, "detector_run_id": pipe.frozen.detector_run_id},
                            cause="gate passed (test)", at=at)
    steps = steps or [[call("run_check")], [call("attribute_anomaly", top=3)], submit(state=state)]
    run = runner_mod.Runner(tools=toolsets.InProcessTools(server), model=ScriptedModel(steps),
                            sink=sink or sinks.DirectSink(store), profile=pipe.profile, profile_sha256="f" * 64,
                            skills=load_skills(pipe.profile, ROOT)).run(iid)
    return iid, run


# --- who may call what (INT-05, HIL-07) ---------------------------------------------------------

def test_tokens_are_checked_and_weak_ones_refused(world):
    client = world[3]
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/metrics").status_code == 404           # metrics are on 9107, core network only
    from pdm_common import metrics
    assert "pdm_http_requests_total" in metrics.REGISTRY.exposition()
    r = client.get("/assets")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert client.get("/assets", headers={"Authorization": "Bearer " + "x" * 40}).status_code == 401
    assert client.get("/assets", headers=h("agent")).status_code == 403
    assert client.get("/me", headers=h("rosh")).json() == {"role": "reviewer", "name": "rosh"}
    assert client.get("/me", headers=h("engineer")).json() == {"role": "engineer", "name": "engineer"}
    with pytest.raises(auth.AuthError, match="shorter"):
        auth.load_tokens({"PDM_TOKEN_REVIEWER": "short"})
    with pytest.raises(auth.AuthError, match="same token"):
        auth.load_tokens({"PDM_TOKEN_REVIEWER": "z" * 40, "PDM_TOKEN_AGENT": "z" * 40})
    with pytest.raises(auth.AuthError, match="no PDM_TOKEN"):
        auth.load_tokens({"PDM_TOKEN_CONNECTOR": "c" * 40})                      # grants nothing here


def test_an_error_never_echoes_the_token(world):
    fake = "secret-value-" + "x" * 30
    r = world[3].get("/assets", headers={"Authorization": f"Bearer {fake}"})
    assert "secret-value" not in r.text


# --- the read views (HIL-01 to HIL-04) ------------------------------------------------------------

def test_the_asset_list_puts_the_highest_p72_first_and_every_probability_has_bounds(world):
    store, pipe, _, client = world
    store.insert("prognoses", {"asset_id": "zz_other", "ts": "2020-06-01T03:00:00Z", "model_run_id": "m",
                               "probabilities": {h_: {"p": 0.9, "lower": 0.8, "upper": 0.95}
                                                 for h_ in ("24", "72", "168")}})
    rows = client.get("/assets", headers=h("operator")).json()["assets"]
    assert [r["asset_id"] for r in rows] == ["zz_other", pipe.profile["asset"]["id"]]
    mine = rows[1]
    assert mine["ne107"] and mine["last_update"] and mine["open_incidents"] >= 1
    for horizon in mine["prognosis"]["horizons"].values():
        assert {"p", "lower", "upper"} <= set(horizon)
    assert mine["prognosis"]["skill"]["meets_bar"] is False and mine["prognosis"]["skill"]["bar"] == 0.15


def test_the_asset_detail_and_series(world):
    _, pipe, _, client = world
    aid = pipe.profile["asset"]["id"]
    doc = client.get(f"/assets/{aid}", headers=h("rosh")).json()
    assert doc["latest_alert"]["attribution"][0]["share"] > 0 and doc["incidents"]
    s = client.get(f"/assets/{aid}/series", headers=h("rosh")).json()
    assert len(s["scores"]["score"]) == len(s["scores"]["threshold"]) > 100
    assert s["prognoses"]["ts"] and len(s["prognoses"]["72"]["upper"]) == len(s["prognoses"]["ts"])
    assert client.get("/assets/nope", headers=h("rosh")).status_code == 404


def test_the_incident_view_expands_every_citation(world):
    client = world[3]
    iid, run = diagnosed(world)
    doc = client.get(f"/incidents/{iid}", headers=h("rosh")).json()
    assert doc["incident"]["state"] == "awaiting_review" and doc["brief"]["brief_id"] == run["brief_id"]
    from pdm_common.brief import cited
    assert set(doc["cited_results"]) == set(cited(doc["brief"]["document"]))
    assert doc["trace"]["outcome"] == "sufficient" and doc["banner"] is None
    rid = next(iter(doc["cited_results"]))
    assert client.get(f"/results/{rid}", headers=h("rosh")).json()["result_id"] == rid
    queue = client.get("/incidents?state=awaiting_review", headers=h("rosh")).json()["incidents"]
    assert iid in [i["incident_id"] for i in queue]


def test_insufficient_and_partial_briefs_carry_a_banner(world):
    client = world[3]
    iid, _ = diagnosed(world, state="insufficient")
    assert client.get(f"/incidents/{iid}", headers=h("rosh")).json()["banner"] == "insufficient"


# --- decisions (HIL-03, HIL-05, HIL-06) -------------------------------------------------------------

def test_approve_records_the_decision_moves_the_incident_and_queues_the_order(world):
    store, _, _, client = world
    iid, run = diagnosed(world)
    r = client.post(f"/briefs/{run['brief_id']}/decisions", headers=h("rosh"),
                    json={"action": "approve", "reason": "matches the leak signature"})
    assert r.status_code == 201, r.text
    decision, wo = r.json()["decision"], r.json()["work_order"]
    assert decision["reviewer"] == "rosh" and decision["trace_id"] == run["trace_id"]
    assert decision["versions"]["api"] == "pdm-api 1.0" and len(decision["evidence_sha256"]) == 64
    assert wo["status"] == "queued" and wo["decision_id"] == decision["decision_id"]
    events = lifecycle.history(store, iid)
    assert events[-1]["to_state"] == "approved" and events[-1]["actor"] == "reviewer:rosh"
    # decisions are insert-only, and a second decision is refused
    with pytest.raises(Exception, match="insert-only"):
        store.execute("UPDATE decisions SET action = 'reject'")
    again = client.post(f"/briefs/{run['brief_id']}/decisions", headers=h("ashok"),
                        json={"action": "reject", "reason": "changed my mind"})
    assert again.status_code == 409 and "not awaiting review" in again.json()["error"]


def test_edit_stores_the_diff_and_sends_the_edited_text(world):
    store, _, _, client = world
    iid, run = diagnosed(world)
    draft = client.get(f"/incidents/{iid}", headers=h("rosh")).json()["brief"]["document"]["work_order"]
    edited = dict(draft, priority="high", tasks=draft["tasks"] + ["Check the drain valve"])
    r = client.post(f"/briefs/{run['brief_id']}/decisions", headers=h("ashok"),
                    json={"action": "edit", "reason": "add the drain check", "work_order": edited})
    assert r.status_code == 201, r.text
    decision, wo = r.json()["decision"], r.json()["work_order"]
    compact = decision["diff"].replace(" ", "")
    assert '+"Checkthedrainvalve"' in compact and '-"priority":"low"' in compact and '+"priority":"high"' in compact
    assert wo["body"]["priority"] == "high" and wo["body"]["approved_by"] == "ashok"
    from pdm_common.workorders import text_sha256
    assert wo["text_sha256"] == text_sha256(decision["work_order"]) != text_sha256(draft)


def test_reject_closes_the_incident_without_a_work_order(world):
    store, _, _, client = world
    iid, run = diagnosed(world, state="insufficient")
    r = client.post(f"/briefs/{run['brief_id']}/decisions", headers=h("rosh"),
                    json={"action": "reject", "reason": "a known restart"})
    assert r.status_code == 201 and r.json()["work_order"] is None
    assert [e["to_state"] for e in lifecycle.history(store, iid)][-2:] == ["rejected", "closed"]
    assert store.count("work_orders", {"incident_id": iid}) == 0


def test_bad_decisions_are_refused(world):
    client = world[3]
    iid, run = diagnosed(world)
    url = f"/briefs/{run['brief_id']}/decisions"
    assert client.post(url, headers=h("engineer"), json={"action": "approve", "reason": "x"}).status_code == 403
    assert client.post(url, headers=h("rosh"), json={"action": "approve"}).status_code == 422          # no reason
    assert client.post(url, headers=h("rosh"), json={"action": "ship", "reason": "x"}).status_code == 422
    assert client.post(url, headers=h("rosh"), json={"action": "edit", "reason": "x"}).status_code == 422
    bad = {"title": "", "priority": "now", "tasks": [], "safety_notes": []}
    r = client.post(url, headers=h("rosh"), json={"action": "edit", "reason": "x", "work_order": bad})
    assert r.status_code == 422 and "/work_order/" in r.json()["error"]
    assert client.post("/briefs/B-nope-1/decisions", headers=h("rosh"),
                       json={"action": "approve", "reason": "x"}).status_code == 404
    assert client.post(url, headers=h("rosh"), content=b"not json").status_code == 422


def test_no_route_but_a_decision_creates_a_work_order(world):
    """HIL-06: every POST route, called by every role with a body that names a brief and a work
    order, leaves work_orders as it was; only POST /briefs/{id}/decisions adds a row."""
    store, _, _, client = world
    iid, run = diagnosed(world)
    before = store.count("work_orders")
    app = client.app
    body = {"action": "approve", "reason": "x", "brief_id": run["brief_id"], "trace_id": run["trace_id"],
            "work_order": {"title": "t", "priority": "low", "tasks": ["t"], "safety_notes": ["s"]},
            "state": {"trace_id": "T-x"}, "trace": {"trace_id": "T-x"}, "brief": {"incident_id": iid}}
    for route in app.routes:
        if "POST" not in (getattr(route, "methods", None) or ()) or route.path == "/briefs/{brief_id}/decisions":
            continue
        path = route.path.replace("{incident_id}", iid).replace("{trace_id}", "T-x").replace(
            "{brief_id}", run["brief_id"])
        for who in AS:
            client.post(path, headers=h(who), json=body)
    assert store.count("work_orders") == before
    assert {r.path for r in app.routes if "POST" in (getattr(r, "methods", None) or ())} == {
        "/briefs/{brief_id}/decisions", "/incidents/{incident_id}/claim", "/incidents/{incident_id}/release",
        "/incidents/{incident_id}/briefs", "/agent/runs/{trace_id}/checkpoint", "/agent/runs/{trace_id}/trace"}


# --- the audit (HIL-08) ---------------------------------------------------------------------------

def test_a_decision_is_rebuilt_from_the_store(world):
    store, _, _, client = world
    iid, run = diagnosed(world)
    d = client.post(f"/briefs/{run['brief_id']}/decisions", headers=h("rosh"),
                    json={"action": "approve", "reason": "ok"}).json()["decision"]
    audit = client.get(f"/decisions/{d['decision_id']}", headers=h("evaluator")).json()
    assert audit["evidence_matches"] and audit["incident"]["consistent"]
    assert audit["incident"]["state_from_events"] == "approved" and audit["trace"]["trace_id"] == run["trace_id"]
    assert audit["brief"]["brief_id"] == run["brief_id"] and audit["work_order"]["decision_id"] == d["decision_id"]
    assert set(audit["evidence"]["results"]) >= set(json.loads(json.dumps(audit["brief"]["checks_run"])))
    full = client.get(f"/traces/{run['trace_id']}", headers=h("engineer")).json()
    assert full["messages"]["system"].startswith("You are the diagnostic agent")
    assert client.get(f"/traces/{run['trace_id']}", headers=h("rosh")).status_code == 403
    # a record whose evidence no longer hashes the same is reported, not hidden
    store.insert("decisions", dict(d, decision_id="D-000000000000", evidence_sha256="0" * 64))
    assert client.get("/decisions/D-000000000000", headers=h("operator")).json()["evidence_matches"] is False


# --- the agent through the API ----------------------------------------------------------------------

def test_the_agent_writes_through_the_api(world):
    store, _, _, client = world
    sink = sinks.ApiSink("http://testserver", AS["agent"], client=client)
    iid, run = diagnosed(world, sink=sink)
    assert run["outcome"] == "sufficient"
    assert store.select("incidents", {"incident_id": iid})[0]["state"] == "awaiting_review"
    assert store.count("agent_traces", {"trace_id": run["trace_id"]}) == 1
    assert store.select("agent_checkpoints", {"trace_id": run["trace_id"]})[0]["node"] == "done"
    # a bad brief through the API: 422 with the problems, stored as invalid, the incident unmoved
    iid2, run2 = diagnosed(world, sink=sink, steps=[[call("run_check")], submit(claim="It ran 7777 h."),
                                                    submit(claim="It ran 8888 h.")])
    assert run2["outcome"] == "fallback"
    rows = store.select("briefs", {"incident_id": iid2}, order="created_at")
    assert [r["valid"] for r in rows] == [False, False, True]
    assert client.post(f"/incidents/{iid2}/briefs", headers=h("rosh"), json={}).status_code == 403
    assert client.post(f"/incidents/{iid2}/claim", headers=h("agent"), json={"trace_id": "T-late"}).json() == \
        {"claimed": False}
