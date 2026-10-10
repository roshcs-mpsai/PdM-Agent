"""Payload schemas and the brief validator (T17; FRD section 9, BRF-01 to BRF-07).

The contract tests run the services in process on a synthetic stream and put
every message they publish through its schema, so the schemas describe what
the services send, not what we meant them to send.
"""
import copy
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for svc in ("replayer", "ingest", "detector", "gate", "prognostics"):
    sys.path.insert(0, str(ROOT / "services" / svc))

from pdm_common import schemas  # noqa: E402
from pdm_common.brief import fallback_brief, number_supported, validate_brief  # noqa: E402
from pdm_common.profile import load_profile, profile_hash  # noqa: E402

PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"


@pytest.fixture(scope="module")
def profile():
    return load_profile(PROFILE_PATH)


def test_every_schema_is_valid_json_schema():
    assert schemas.names() == ["brief_v1", "device_event_v1", "drift_v1", "incident_v1", "prognosis_v1",
                               "score_v1", "status_v1", "telemetry_json_v1", "tool_result_v1", "window_v1"]
    for name in schemas.names():
        schemas.validator(name)


@pytest.fixture(scope="module")
def published(profile):
    """Run replayer -> ingest -> detector -> gate + prognostics in process for 3 h of a
    synthetic stream that turns anomalous after 2 h; collect every message by topic."""
    import detector
    import gate
    import ingest
    import prognosis
    import replayer
    from pdm_common.checks import load_registry
    from pdm_common.events import load_events
    from pdm_common.profile import events_file
    from pdm_common.store import Store

    store = Store("sqlite://")
    store.init_schema()
    sha = profile_hash(PROFILE_PATH)
    i = ingest.Ingest(profile, sha, profile_path=PROFILE_PATH, store=store)
    frozen = detector.load_for_profile(profile, PROFILE_PATH)
    d = detector.Detector(profile, frozen, sha, store)
    g = gate.GateService(profile, sha, events=load_events(events_file(profile, PROFILE_PATH)),
                         registry=load_registry(profile, ROOT), store=store)
    p = prognosis.Prognostics(profile, frozen, sha, store)
    topics = profile["topics"]
    by_topic: dict = {}

    def route(outs):
        for o in outs:
            by_topic.setdefault(o.topic, []).append(o.payload)
            if o.topic == topics["windows"]:
                g.handle_window(o.payload)
                route(p.handle_window(o.payload))
                route(d.handle(o.payload))
            elif o.topic == topics["scores"]:
                route(g.handle_score(o.payload))
                route(p.handle_score(o.payload))

    for seq in range(3 * 360):
        values = {}
        for s in profile["signals"]["analog"]:
            lo, hi = s["range"]
            values[s["name"]] = lo + (hi - lo) * (0.3 + 0.0001 * seq + (0.3 if seq > 720 else 0))
        for s in profile["signals"]["digital"]:
            values[s["name"]] = float(seq % 2)
        _, payload = replayer.build_message(profile, seq, dt.datetime(2020, 6, 1) + dt.timedelta(seconds=10 * seq),
                                            values)
        by_topic.setdefault(topics["telemetry"], []).append(payload)
        route(i.handle(payload))
    route(i.flush())
    return by_topic


@pytest.mark.parametrize("topic, schema", [("telemetry", "telemetry_json_v1"), ("windows", "window_v1"),
                                           ("scores", "score_v1"), ("status", "status_v1"),
                                           ("incidents", "incident_v1"), ("prognosis", "prognosis_v1")])
def test_published_messages_follow_their_schema(profile, published, topic, schema):
    messages = published.get(profile["topics"][topic], [])
    assert messages, f"nothing was published on {topic}"
    for m in messages:
        assert schemas.validate(schema, m) == [], m[:200]


def test_a_broken_message_names_the_problem(profile, published):
    window = json.loads(published[profile["topics"]["windows"]][0])
    window["n_bridged"] = -1
    del window["features_sha256"]
    problems = schemas.validate("window_v1", window)
    assert any("features_sha256" in p for p in problems) and any(p.startswith("/n_bridged") for p in problems)


# --- the brief validator --------------------------------------------------------------------------

INCIDENT = "INC-metropt3_apu-20200715T0045Z"


def result(rid, tool, output):
    return {"schema": "tool_result_v1", "result_id": rid, "incident_id": INCIDENT, "tool": tool,
            "arguments": {}, "as_of": "2020-07-15T00:45:00Z", "output": output}


RESULTS = {
    "R-ab12cd34-1": result("R-ab12cd34-1", "incident_context",
                           {"prognosis": {"horizons": {"24": {"p": 0.04, "lower": 0.01, "upper": 0.09}}}}),
    "R-ab12cd34-2": result("R-ab12cd34-2", "run_check",
                           {"results": [{"id": "C4", "result": "fail", "value": 0.92, "threshold": 0.1889,
                                         "measured": "motor has run without a rest for 0.92 h (healthy 99th "
                                                     "percentile 0.19 h)"}]}),
    "R-ab12cd34-3": result("R-ab12cd34-3", "get_telemetry",
                           {"signals": {"panel_pressure": {"min": 7.12, "max": 9.31, "last": 7.4}}}),
}


def good_brief():
    return {
        "schema": "brief_v1", "brief_id": "B-ab12cd34-1", "incident_id": INCIDENT, "asset_id": "metropt3_apu",
        "evidence_state": "sufficient",
        "summary": "Continuous running with falling panel pressure points to an air leak on the delivery side.",
        "hypotheses": [{"id": "H1", "category": "process", "statement": "Air leak on the delivery side",
                        "likelihood": "likely",
                        "claims": [{"text": "The motor ran 0.92 h without a rest, over the 0.19 h healthy limit.",
                                    "cites": ["R-ab12cd34-2"]},
                                   {"text": "Panel pressure fell to 7.12 bar.", "cites": ["R-ab12cd34-3"]}],
                        "against": []}],
        "prognosis": {"cites": ["R-ab12cd34-1"]},
        "recommended_action": {"type": "inspection", "text": "Inspect the reservoir and the valve seating.",
                               "cites": ["R-ab12cd34-2"]},
        "work_order": {"title": "Inspect APU for an air leak", "asset": "metropt3_apu", "priority": "high",
                       "tasks": ["Leak-test the delivery side"], "safety_notes": ["Isolate and vent before work."],
                       "status": "draft"},
        "checks_run": ["R-ab12cd34-2"],
        "versions": {"llm": "claude-opus-5-5", "skills": {"incident_triage": "abc"}, "profile": "p" * 64},
    }


def test_a_grounded_brief_is_valid():
    assert validate_brief(good_brief(), RESULTS, incident_id=INCIDENT) == []


def test_an_uncited_claim_is_rejected():
    b = good_brief()
    b["hypotheses"][0]["claims"][0]["cites"] = []
    assert any("cites" in p for p in validate_brief(b, RESULTS))


def test_a_citation_to_a_result_this_run_did_not_produce_is_rejected():
    b = good_brief()
    b["recommended_action"]["cites"] = ["R-ab12cd34-9"]
    assert validate_brief(b, RESULTS) == ["recommended_action: cites R-ab12cd34-9, which this run did not produce"]
    other = dict(RESULTS, **{"R-ffff0000-1": dict(RESULTS["R-ab12cd34-3"], result_id="R-ffff0000-1",
                                                   incident_id="INC-other")})
    b["recommended_action"]["cites"] = ["R-ffff0000-1"]
    assert "a result of another incident" in validate_brief(b, other)[0]


def test_a_planted_wrong_number_is_caught():
    b = good_brief()
    b["hypotheses"][0]["claims"][1]["text"] = "Panel pressure fell to 6.5 bar."
    assert validate_brief(b, RESULTS) == ["H1.claims[1]: 6.5 appears in none of R-ab12cd34-3"]


def test_the_prognosis_must_be_the_stored_one():
    b = good_brief()
    b["prognosis"]["cites"] = ["R-ab12cd34-3"]
    assert validate_brief(b, RESULTS) == ["prognosis: R-ab12cd34-3 is not a stored prognosis"]


def test_an_insufficient_brief_recommends_no_inspection():
    b = good_brief()
    b["evidence_state"] = "insufficient"
    problems = validate_brief(b, RESULTS)
    assert problems == ["recommended_action: an insufficient brief recommends a data-quality check or monitoring"]


def test_schema_rules_for_categories_and_the_work_order():
    b = good_brief()
    b["hypotheses"][0]["category"] = "magic"
    b["work_order"]["status"] = "approved"
    problems = validate_brief(b, RESULTS)
    assert any("/hypotheses/0/category" in p for p in problems) and any("/work_order/status" in p for p in problems)


def test_the_fallback_brief_is_valid():
    fb = fallback_brief(brief_id="B-ab12cd34-2", incident_id=INCIDENT, asset_id="metropt3_apu", results=RESULTS,
                        versions={"llm": "claude-opus-5-5", "skills": {}, "profile": "p" * 64})
    assert validate_brief(fb, RESULTS) == []
    assert fb["checks_run"] == ["R-ab12cd34-2"] and fb["recommended_action"]["type"] == "data_quality_check"
    exhausted = fallback_brief(brief_id="B-ab12cd34-3", incident_id=INCIDENT, asset_id="metropt3_apu",
                               results=RESULTS, versions=fb["versions"], state="budget_exhausted",
                               reason="12 tool calls used")
    assert validate_brief(exhausted, RESULTS) == [] and "12 tool calls" in exhausted["summary"]


@pytest.mark.parametrize("written, value, ok", [("0.19", 0.1889, True), ("92", 0.92, True), ("0.2", 0.1889, True),
                                                ("0.18", 0.1889, False), ("7", 7.12, True), ("7.1", 7.12, True),
                                                ("6.5", 7.12, False)])
def test_numbers_match_within_their_rounding(written, value, ok):
    assert number_supported(written, [value]) is ok


def test_validation_does_not_change_the_brief():
    b = good_brief()
    before = copy.deepcopy(b)
    validate_brief(b, RESULTS)
    assert b == before
