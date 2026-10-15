"""The blinded three-arm study (T34; HIL-09, PR-16, ESR-05): services/api/study.py.

Real incidents: the synthetic excursion of tests/test_pipeline.py through eval/pipeline.py opens one
incident at the gate; more are opened the way the gate opens them (its cause, its parameters and the
checks as of the opening). The live brief of each comes from the agent runner with a scripted model,
and the study brief from the retrieval-only arm run with ``study=True``, as ``agent.py --study`` does.
The world is built once and copied for each test, so every test starts with an empty study_sessions.
"""
import datetime as dt
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path
from typing import NamedTuple

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
for sub in ("eval", "services/api", "services/agent", "services/mcp_server", "tests", "tools"):
    sys.path.insert(0, str(ROOT / sub))

import auth  # noqa: E402
import main as api  # noqa: E402
import runner as runner_mod  # noqa: E402
import sinks  # noqa: E402
import study  # noqa: E402
import toolsets  # noqa: E402
from llm import ScriptedModel, call  # noqa: E402
from pipeline import Pipeline  # noqa: E402
from pdm_common import incidents as lifecycle  # noqa: E402
from pdm_common.asof import evidence_asof  # noqa: E402
from pdm_common.corpus import Corpus  # noqa: E402
from pdm_common.skills import load_skills  # noqa: E402
from pdm_common.store import Store  # noqa: E402
from pdm_common.timeutil import iso_utc, to_utc  # noqa: E402
from test_agent import submit  # noqa: E402
from test_pipeline import synthetic  # noqa: E402

PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
ENV = {"PDM_TOKEN_REVIEWER_ROSH": "r" * 40, "PDM_TOKEN_REVIEWER_ASHOK": "a" * 40, "PDM_TOKEN_REVIEWER": "w" * 40,
       "PDM_TOKEN_ENGINEER": "e" * 40, "PDM_TOKEN_OPERATOR": "o" * 40, "PDM_TOKEN_EVALUATOR": "v" * 40,
       "PDM_TOKEN_AGENT": "g" * 40}
AS = {"rosh": "r" * 40, "ashok": "a" * 40, "reviewer": "w" * 40, "engineer": "e" * 40, "operator": "o" * 40,
      "evaluator": "v" * 40, "agent": "g" * 40}
SALT = "study-salt-for-the-tests-0123"
START = dt.datetime(2026, 10, 12, 9, 0, tzinfo=dt.timezone.utc)


def h(who):
    return {"Authorization": f"Bearer {AS[who]}"}


# --- the world: incidents with a live brief and, for most, a study brief ------------------------------

class World(NamedTuple):
    store: Store
    asset: str
    incidents: list        # [(incident_id, live evidence state, study brief: True, False or "invalid")]


def _runner(store, pipe, server, steps, *, arm="full", study_run=False, budget=runner_mod.Budget()):
    return runner_mod.Runner(tools=toolsets.InProcessTools(server), model=ScriptedModel(steps),
                             sink=sinks.DirectSink(store), profile=pipe.profile, profile_sha256="f" * 64,
                             skills=load_skills(pipe.profile, ROOT), budget=budget, arm=arm, study=study_run)


def _open_like_the_gate(store, pipe, template: dict, cause: str, at: dt.datetime) -> str:
    """An incident as the gate opens one: its parameters, its cause and the checks as of the opening."""
    asset = pipe.profile["asset"]["id"]
    iid = f"INC-{asset}-{at:%Y%m%dT%H%MZ}"
    results = pipe.registry.run(evidence_asof(store, pipe.profile, pipe.events, asset, iso_utc(at)))
    lifecycle.open_incident(store, {"incident_id": iid, "asset_id": asset, "opened_at": at, "as_of": at,
                                    "gate": template["gate"], "detector_run_id": template["detector_run_id"],
                                    "profile_sha256": template["profile_sha256"],
                                    "triage": {"checks": [r.as_dict() for r in results],
                                               "triage": pipe.registry.triage(results)}}, cause=cause, at=at)
    return iid


def _live_brief(store, pipe, server, iid: str, state: str) -> None:
    if state == "budget_exhausted":                    # one model turn: the runtime writes the partial brief
        run = _runner(store, pipe, server, [[call("run_check")]], budget=runner_mod.Budget(turns=1)).run(iid)
    elif state == "insufficient":
        run = _runner(store, pipe, server, [[call("run_check")], submit(state="insufficient")]).run(iid)
    else:
        run = _runner(store, pipe, server, [[call("run_check")], [call("attribute_anomaly", top=3)],
                                            submit(state="sufficient")]).run(iid)
    assert run["outcome"] == state, run


def _study_brief(store, pipe, server, iid: str, kind) -> None:
    if kind == "invalid":                              # stored, as accept_brief stores a refused draft
        store.insert("briefs", {"brief_id": "B-0badbeef-1", "incident_id": iid,
                                "document": {"incident_id": iid}, "evidence_state": "insufficient", "valid": False,
                                "validation": ["/summary: missing"], "trace_id": "T-invalid", "arm": "retrieval_only",
                                "created_at": "2026-10-10T00:00:00Z"})
    elif kind:
        run = _runner(store, pipe, server, [[call("search_knowledge", query="air leak")], submit()],
                      arm="retrieval_only", study_run=True).run(iid)
        assert run["outcome"] == "insufficient", run


def build_world(plan) -> World:
    """One incident per (live evidence state, study brief) in ``plan``; the first is the gate's own."""
    store = Store("sqlite://")
    store.init_schema()
    pipe = Pipeline(PROFILE_PATH, store, compute_every=60)
    pipe.feed(synthetic(pipe.profile, 200, anomalous_after=60))
    pipe.flush()
    server = toolsets.toolserver.ToolServer(store, pipe.profile, pipe.events, registry=pipe.registry,
                                            corpus=Corpus.load(ROOT / "corpus"), frozen=pipe.frozen)
    template = store.select("incidents")[0]
    cause = lifecycle.history(store, template["incident_id"])[0]["cause"]
    out = []
    for n, (state, kind) in enumerate(plan):
        at = dt.datetime(2020, 6, 1, 1, 40, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=n)
        iid = template["incident_id"] if n == 0 else _open_like_the_gate(store, pipe, template, cause, at)
        _live_brief(store, pipe, server, iid, state)
        _study_brief(store, pipe, server, iid, kind)
        out.append((iid, state, kind))
    return World(store, pipe.profile["asset"]["id"], out)


def clone(store: Store) -> Store:
    """A copy of an in-memory store, its insert-only triggers included."""
    copy = Store("sqlite://")
    store.conn.backup(copy.conn)
    return copy


class Clock:
    def __init__(self, start=START):
        self.now = start

    def __call__(self):
        return self.now

    def tick(self, seconds: float):
        self.now += dt.timedelta(seconds=seconds)


def study_client(store, *, env=None, rng=None, clock=None, tokens=None) -> TestClient:
    tokens = auth.load_tokens(ENV) if tokens is None else tokens
    routes = study.routes(lambda: store, tokens, env={"PDM_STUDY_SALT": SALT} if env is None else env,
                          rng=rng, now=clock)
    return TestClient(api.create_app(store, tokens, extra_routes=routes))


PLAN = [("sufficient", True)] * 4 + [("insufficient", True)] * 2 + [("budget_exhausted", True),
                                                                     ("sufficient", False), ("sufficient", "invalid")]


@pytest.fixture(scope="module")
def world():
    return build_world(PLAN)


@pytest.fixture
def fresh(world):
    """A copy of the world's store and a study API over it with a fixed clock and seed."""
    store = clone(world.store)
    clock = Clock()
    return store, study_client(store, rng=random.Random(7), clock=clock), clock


def items_of(store, session_id) -> list[dict]:
    """The item rows of a session in order (the test reads the store as the evaluator would)."""
    return [r for r in store.select("study_sessions", {"session_id": session_id}, order="order_no")
            if r["order_no"] is not None and r["decision"] is None and r["rating"] is None]


def walk(client, who, clock, store, *, answer=lambda item, arm: (3, "unsure"), seconds=lambda arm: 5.0):
    """Start a session and answer every item. Returns (session id, [(kind, JSON)] of everything the
    participant received: the start, each item, each answer receipt, the end, the session list)."""
    r = client.post("/study/sessions", headers=h(who), json={"consent": True})
    assert r.status_code == 201, r.text
    sid = r.json()["session_id"]
    got = [("start", r.json())]
    arms = {row["order_no"]: row["arm"] for row in items_of(store, sid)}
    while True:
        r = client.get(f"/study/sessions/{sid}/next", headers=h(who))
        assert r.status_code == 200, r.text
        item = r.json()
        if item["done"]:
            got.append(("end", item))
            break
        got.append(("item", item))
        arm = arms[item["order_no"]]
        clock.tick(seconds(arm))
        rating, decision = answer(item, arm)
        r = client.post(f"/study/sessions/{sid}/answers", headers=h(who),
                        json={"order_no": item["order_no"], "rating": rating, "decision": decision})
        assert r.status_code == 201, r.text
        got.append(("answer", r.json()))
    got.append(("sessions", client.get("/study/sessions", headers=h(who)).json()))
    return sid, got


def keys_and_values(doc, keys=None, values=None):
    keys = set() if keys is None else keys
    values = [] if values is None else values
    if isinstance(doc, dict):
        for k, v in doc.items():
            keys.add(k)
            keys_and_values(v, keys, values)
    elif isinstance(doc, list):
        for v in doc:
            keys_and_values(v, keys, values)
    else:
        values.append(doc)
    return keys, values


# --- the pieces --------------------------------------------------------------------------------------

def test_pseudonyms_and_labels():
    p = study.pseudonym(SALT, "rosh")
    assert re.fullmatch(r"P-[0-9a-f]{8}", p) and p == study.pseudonym(SALT, "rosh")
    assert len({p, study.pseudonym(SALT, "ashok"), study.pseudonym("x" * 20, "rosh")}) == 3
    mappings = {tuple(sorted(study.arm_labels(SALT, f"S-{n:016x}").items())) for n in range(200)}
    assert len(mappings) == 6                                   # every assignment of A, B, C occurs
    for m in mappings:
        assert sorted(dict(m).values()) == ["A", "B", "C"] and set(dict(m)) == set(study.ARMS)
    assert study.arm_labels(SALT, "S-1") == study.arm_labels(SALT, "S-1")      # fixed for a session


@pytest.mark.parametrize("n", range(0, 11))
def test_each_arm_gets_a_third(n):
    dealt = study.deal([f"I{k}" for k in range(n)], random.Random(n))
    counts = Counter(arm for _, arm in dealt)
    assert sorted(i for i, _ in dealt) == sorted(f"I{k}" for k in range(n))
    assert max(counts.get(a, 0) for a in study.ARMS) - min(counts.get(a, 0) for a in study.ARMS) <= 1


def test_a_blind_brief_keeps_no_id_and_relabels_its_citations():
    doc = {"schema": "brief_v1", "brief_id": "B-0123abcd-2", "incident_id": "INC-x", "asset_id": "a",
           "evidence_state": "sufficient",
           "summary": "See R-0123abcd-3; the run T-0123456789abcdef wrote B-0123abcd-2.",
           "hypotheses": [{"id": "H1", "category": "process", "statement": "s", "likelihood": "likely",
                           "claims": [{"text": "per R-0123abcd-3 and R-0123abcd-9", "cites": ["R-0123abcd-3"]}]}],
           "prognosis": {"cites": ["R-0123abcd-1"]},
           "recommended_action": {"type": "inspection", "text": "t", "cites": ["R-0123abcd-3"]},
           "work_order": {"title": "t", "priority": "low", "tasks": ["x"], "safety_notes": ["y"]},
           "checks_run": ["R-0123abcd-3"], "versions": {"llm": "some-model", "skills": {}, "profile": "p"}}
    results = {f"R-0123abcd-{n}": {"result_id": f"R-0123abcd-{n}", "arguments": {}, "output": {"n": n},
                                   "tool": "incident_context" if n == 1 else "run_check",
                                   "created_at": "2026-10-10T00:00:00Z"} for n in (1, 3, 9)}
    brief, evidence = study.blind_brief(doc, results)
    assert not {"schema", "brief_id", "versions"} & set(brief)
    assert brief["hypotheses"][0]["claims"][0]["cites"] == ["E1"] and brief["prognosis"]["cites"] == ["E2"]
    assert brief["summary"] == "See E1; the run (withheld) wrote (withheld)."
    assert brief["hypotheses"][0]["claims"][0]["text"] == "per E1 and an uncited result"
    assert list(evidence) == ["E1", "E2"] and evidence["E2"] == {"tool": "incident_context", "arguments": {},
                                                                 "output": {"n": 1}}
    assert "R-0123abcd" not in json.dumps([brief, evidence]) and "some-model" not in json.dumps([brief, evidence])


# --- who may call what ---------------------------------------------------------------------------------

def test_without_a_salt_every_study_endpoint_answers_503(world):
    store = clone(world.store)
    for env, why in (({}, "unset"), ({"PDM_STUDY_SALT": "short"}, "shorter than 16")):
        client = study_client(store, env=env)
        for method, path, who in (("POST", "/study/sessions", "rosh"), ("GET", "/study/sessions", "rosh"),
                                  ("GET", "/study/sessions/S-x/next", "rosh"),
                                  ("POST", "/study/sessions/S-x/answers", "rosh"),
                                  ("GET", "/study/results", "evaluator")):
            r = client.request(method, path, headers=h(who), json={"consent": True})
            assert r.status_code == 503 and why in r.json()["error"], (path, r.text)
        assert client.post("/study/sessions", json={"consent": True}).status_code == 401      # sign-in still first
    assert store.count("study_sessions") == 0


def test_participants_are_named_reviewers_and_only_evaluators_read_results(fresh):
    store, client, _ = fresh
    r = client.post("/study/sessions", json={"consent": True})
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    for who in ("engineer", "operator", "evaluator", "agent"):
        assert client.post("/study/sessions", headers=h(who), json={"consent": True}).status_code == 403, who
    r = client.post("/study/sessions", headers=h("reviewer"), json={"consent": True})
    assert r.status_code == 403 and "your own reviewer token" in r.json()["error"]
    for who in ("rosh", "engineer"):
        assert client.get("/study/results", headers=h(who)).status_code == 403
    assert client.get("/study/results", headers=h("evaluator")).status_code == 200
    assert store.count("study_sessions") == 0


# --- consent, dealing and order -------------------------------------------------------------------------

def test_consent_comes_first(fresh, world):
    store, client, _ = fresh
    for body in ({}, {"consent": False}, {"consent": "yes"}, {"consent": 1}):
        r = client.post("/study/sessions", headers=h("rosh"), json=body)
        assert r.status_code == 422 and "consent comes first" in r.json()["error"]
    assert client.post("/study/sessions", headers=h("rosh"), content=b"not json").status_code == 422
    assert store.count("study_sessions") == 0
    r = client.post("/study/sessions", headers=h("rosh"), json={"consent": True})
    assert r.status_code == 201 and set(r.json()) == {"session_id", "items"}
    sid = r.json()["session_id"]
    rows = store.select("study_sessions", {"session_id": sid}, order="id")
    first = rows[0]
    assert first["consent"] is True
    assert first["order_no"] is None and first["arm"] is None and first["incident_id"] is None
    assert [x["order_no"] for x in rows[1:]] == list(range(1, len(rows)))
    assert {x["reviewer"] for x in rows} == {study.pseudonym(SALT, "rosh")}
    assert all(x["rating"] is None and x["consent"] is None for x in rows[1:])
    assert "rosh" not in json.dumps(rows)


def test_only_incidents_with_a_valid_brief_in_both_arms_are_dealt(fresh, world):
    store, client, _ = fresh
    expected = {iid for iid, _, kind in world.incidents if kind is True}
    assert set(study.eligible(store)) == expected and len(expected) == 7
    sid = client.post("/study/sessions", headers=h("rosh"), json={"consent": True}).json()["session_id"]
    assert {r["incident_id"] for r in items_of(store, sid)} == expected
    bare = Store("sqlite://")
    bare.init_schema()
    r = study_client(bare).post("/study/sessions", headers=h("rosh"), json={"consent": True})
    assert r.status_code == 409 and "--study" in r.json()["error"] and bare.count("study_sessions") == 0


def test_arms_are_balanced_and_order_and_labels_change_between_sessions(fresh):
    store, client, clock = fresh
    orders, extra_arm, mappings = set(), set(), set()
    for _ in range(12):
        sid = client.post("/study/sessions", headers=h("rosh"), json={"consent": True}).json()["session_id"]
        items = items_of(store, sid)
        counts = Counter(r["arm"] for r in items)
        assert len(items) == 7 and sorted(counts.values()) == [2, 2, 3]
        extra_arm.add(counts.most_common(1)[0][0])
        orders.add(tuple(r["incident_id"] for r in items))
        mappings.add(tuple(sorted(study.arm_labels(SALT, sid).items())))
    assert len(orders) >= 10 and extra_arm == set(study.ARMS) and len(mappings) > 1
    system_random = study_client(store, clock=clock)              # the default: random.SystemRandom
    seen = set()
    for _ in range(4):
        sid = system_random.post("/study/sessions", headers=h("ashok"), json={"consent": True}).json()["session_id"]
        seen.add(tuple(r["incident_id"] for r in items_of(store, sid)))
    assert len(seen) > 1


def test_each_arm_keeps_one_label_through_a_session(fresh):
    store, client, clock = fresh
    sid, got = walk(client, "rosh", clock, store)
    arms = {r["order_no"]: r["arm"] for r in items_of(store, sid)}
    pairs = {(arms[doc["order_no"]], doc["label"]) for kind, doc in got if kind == "item"}
    assert len(pairs) == 3 and {label for _, label in pairs} == {"A", "B", "C"}
    assert dict(pairs) == study.arm_labels(SALT, sid)


# --- what a participant sees ------------------------------------------------------------------------------

def test_a_participant_sees_no_arm_no_model_and_no_name(fresh):
    store, client, clock = fresh
    sid, got = walk(client, "rosh", clock, store)
    docs = [doc for _, doc in got]
    text = json.dumps(docs)
    keys, values = keys_and_values(docs)
    assert not keys & {"arm", "arms", "versions", "llm", "model", "trace_id", "brief_id", "reviewer", "skills",
                       "prompt", "result_id", "seconds"}
    assert not {v for v in values if isinstance(v, str)} & set(study.ARMS)
    assert "retrieval" not in text.lower() and "scripted" not in text
    briefs = store.select("briefs")
    for b in briefs:
        assert b["brief_id"] not in text and b["trace_id"] not in text
    assert not re.search(r"\bR-[0-9a-f]{8}-[0-9]+\b", text)
    assert "rosh" not in text and study.pseudonym(SALT, "rosh") not in text
    items = [doc for kind, doc in got if kind == "item"]
    assert len(items) == 7 and {i["kind"] for i in items} == {"alert", "brief"}
    for item in items:
        assert item["label"] in ("A", "B", "C") and item["incident"]["asset_id"]
        if item["kind"] == "alert":
            alert = item["alert"]
            assert alert["cause"].startswith("gate passed") and alert["checks"]
            minutes = [p["minutes_before_opening"] for p in alert["trajectory"]]
            assert minutes and minutes == sorted(minutes, reverse=True) and minutes[-1] >= 0
            assert all(p["threshold"] is not None for p in alert["trajectory"])
        else:
            brief, evidence = item["brief"], item["evidence"]
            assert set(evidence) == {f"E{n}" for n in range(1, len(evidence) + 1)}
            context = brief["prognosis"]["cites"][0]
            assert evidence[context]["tool"] == "incident_context"
            assert item["banner"] == (None if brief["evidence_state"] == "sufficient" else brief["evidence_state"])


def test_time_to_disposition_runs_from_the_first_showing(fresh):
    store, client, clock = fresh
    sid = client.post("/study/sessions", headers=h("rosh"), json={"consent": True}).json()["session_id"]
    clock.tick(10)
    first = client.get(f"/study/sessions/{sid}/next", headers=h("rosh")).json()
    clock.tick(15)
    again = client.get(f"/study/sessions/{sid}/next", headers=h("rosh")).json()       # a page reload
    assert again["order_no"] == first["order_no"] == 1 and again["label"] == first["label"]
    shown = [r for r in store.select("study_sessions", {"session_id": sid}) if r["decision"] == "shown"]
    assert len(shown) == 1 and to_utc(shown[0]["at"]) == START + dt.timedelta(seconds=10)
    clock.tick(27.5)
    r = client.post(f"/study/sessions/{sid}/answers", headers=h("rosh"), json={"order_no": 1, "rating": 4,
                                                                              "decision": "act"})
    assert r.status_code == 201 and r.json() == {"session_id": sid, "order_no": 1, "recorded": True, "items": 7,
                                                 "answered": 1, "done": False}
    row = [x for x in store.select("study_sessions", {"session_id": sid}) if x["rating"] is not None][0]
    assert row["seconds"] == 42.5 and row["rating"] == 4 and row["decision"] == "act"
    assert row["arm"] == items_of(store, sid)[0]["arm"]
    assert client.get(f"/study/sessions/{sid}/next", headers=h("rosh")).json()["order_no"] == 2


def test_refusals(fresh):
    store, client, clock = fresh
    sid = client.post("/study/sessions", headers=h("rosh"), json={"consent": True}).json()["session_id"]
    url = f"/study/sessions/{sid}/answers"
    r = client.post(url, headers=h("rosh"), json={"order_no": 1, "rating": 3, "decision": "act"})
    assert r.status_code == 409 and "not been shown" in r.json()["error"]
    client.get(f"/study/sessions/{sid}/next", headers=h("rosh"))
    before = store.count("study_sessions")
    for bad in ({"rating": 0}, {"rating": 6}, {"rating": "5"}, {"rating": 4.5}, {"rating": True}, {"rating": None},
                {"decision": "maybe"}, {"decision": None}, {"order_no": "1"}):
        body = dict({"order_no": 1, "rating": 3, "decision": "act"}, **bad)
        assert client.post(url, headers=h("rosh"), json=body).status_code == 422, bad
    assert client.post(url, headers=h("rosh"), json={"order_no": 99, "rating": 3,
                                                     "decision": "act"}).status_code == 404
    assert client.post(url, headers=h("rosh"), json={"order_no": 2, "rating": 3,
                                                     "decision": "act"}).status_code == 409   # not shown yet
    # another reviewer's session: neither the next item nor an answer
    assert client.get(f"/study/sessions/{sid}/next", headers=h("ashok")).status_code == 403
    r = client.post(url, headers=h("ashok"), json={"order_no": 1, "rating": 3, "decision": "act"})
    assert r.status_code == 403 and "another participant" in r.json()["error"]
    assert client.get("/study/sessions/S-0000000000000000/next", headers=h("rosh")).status_code == 404
    assert store.count("study_sessions") == before
    assert client.post(url, headers=h("rosh"), json={"order_no": 1, "rating": 3, "decision": "act"}).status_code == 201
    r = client.post(url, headers=h("rosh"), json={"order_no": 1, "rating": 5, "decision": "dismiss"})
    assert r.status_code == 409 and "already answered" in r.json()["error"]
    assert client.get("/study/sessions", headers=h("ashok")).json() == {"sessions": []}
    mine = client.get("/study/sessions", headers=h("rosh")).json()["sessions"]
    assert [(s["session_id"], s["items"], s["answered"], s["done"]) for s in mine] == [(sid, 7, 1, False)]


# --- results, the record, and nothing else touched --------------------------------------------------------------

def test_results_are_per_true_arm_and_name_nobody(fresh):
    store, client, clock = fresh
    rating = {"raw": 2, "retrieval_only": 3, "full": 5}
    choice = {"raw": "dismiss", "retrieval_only": "unsure", "full": "act"}
    wait = {"raw": 30.0, "retrieval_only": 20.0, "full": 10.0}
    for who in ("rosh", "ashok"):
        walk(client, who, clock, store, answer=lambda item, arm: (rating[arm], choice[arm]),
             seconds=lambda arm: wait[arm])
    doc = client.get("/study/results", headers=h("evaluator")).json()
    assert doc["sessions"] == 2 and doc["participants"] == 2 and doc["answers"] == 14
    dealt = Counter(r["arm"] for r in store.select("study_sessions") if r["order_no"] is not None
                    and r["decision"] is None and r["rating"] is None)
    for arm in study.ARMS:
        a = doc["arms"][arm]
        assert a["n"] == a["dealt"] == a["shown"] == dealt[arm]
        assert a["mean_rating"] == rating[arm] and a["median_seconds"] == wait[arm]
        assert a["decisions"] == {d: (dealt[arm] if d == choice[arm] else 0) for d in study.DECISIONS}
        assert a["ratings"][str(rating[arm])] == dealt[arm]
    text = json.dumps(doc)
    for who in ("rosh", "ashok"):
        assert who not in text and study.pseudonym(SALT, who) not in text


def test_the_study_rows_are_insert_only_and_complete(fresh):
    store, client, clock = fresh
    sid, _ = walk(client, "rosh", clock, store)
    kinds = Counter("consent" if r["consent"] else "answer" if r["rating"] is not None else
                    r["decision"] or "item" for r in store.select("study_sessions", {"session_id": sid}))
    assert kinds == {"consent": 1, "item": 7, "shown": 7, "answer": 7}
    for sql in ("UPDATE study_sessions SET rating = 1", "DELETE FROM study_sessions"):
        with pytest.raises(Exception, match="insert-only"):
            store.execute(sql)
    assert store.count("study_sessions") == 22


def test_the_study_changes_no_incident_brief_decision_or_work_order(fresh, world):
    store, client, clock = fresh
    tables = ("incidents", "incident_events", "briefs", "decisions", "work_orders", "tool_results")
    before = {t: store.select(t) for t in tables}
    walk(client, "rosh", clock, store)
    client.post("/study/sessions", headers=h("rosh"), json={"consent": True, "action": "approve", "reason": "x",
                                                             "work_order": {"title": "t"}})
    assert {t: store.select(t) for t in tables} == before
    # the live review screens never show the study brief
    iid = next(i for i, _, kind in world.incidents if kind is True)
    study_brief = [b["brief_id"] for b in store.select("briefs", {"incident_id": iid}) if b["arm"] == "retrieval_only"]
    live = client.get(f"/incidents/{iid}", headers=h("rosh")).json()
    assert study_brief and live["brief"]["arm"] == "full"
    assert not set(study_brief) & {d["brief_id"] for d in live["drafts"]}


def test_main_serves_the_study_routes(monkeypatch):
    import types
    served = {}
    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda app, **kw: served.update(app=app, **kw)
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr(api.logs, "setup", lambda *a, **k: None)
    monkeypatch.setenv("PDM_METRICS_PORT", "off")
    monkeypatch.setenv("PDM_STUDY_SALT", SALT)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    assert api.main(["--store", "sqlite://"]) == 0
    paths = {r.path for r in served["app"].routes}
    assert {"/study/sessions", "/study/sessions/{session_id}/next", "/study/sessions/{session_id}/answers",
            "/study/results", "/assets"} <= paths
    client = TestClient(served["app"])
    r = client.post("/study/sessions", headers=h("rosh"), json={"consent": True})
    assert r.status_code == 409                       # the routes answer: nothing to study in an empty store


def test_the_study_dry_run_script_writes_a_valid_retrieval_only_brief():
    """services/agent/scripts/study_dry_run.json: the retrieval-only arm without a model key."""
    w = build_world([("sufficient", False)])
    pipe = Pipeline(PROFILE_PATH, Store("sqlite://"), compute_every=60)        # profile and registry only
    server = toolsets.toolserver.ToolServer(w.store, pipe.profile, pipe.events, registry=pipe.registry,
                                            corpus=Corpus.load(ROOT / "corpus"), frozen=pipe.frozen)
    steps = json.loads((ROOT / "services" / "agent" / "scripts" / "study_dry_run.json").read_text())
    iid = w.incidents[0][0]
    run = _runner(w.store, pipe, server, steps, arm="retrieval_only", study_run=True).run(iid)
    assert run["outcome"] == "insufficient", run
    assert study.eligible(w.store) == [iid]
