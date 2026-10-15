"""Gate service (T12b; INC-01 to INC-07): streaming gate, incident lifecycle, NE 107.

The streaming gate is held to the batch gate_passes/incidents the harness uses,
on synthetic alert patterns and, with the evaluated scores present, on the
whole 2020 campaign at the frozen threshold.
"""
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "gate"))
sys.path.insert(0, str(ROOT / "services" / "ingest"))
sys.path.insert(0, str(ROOT / "services" / "replayer"))

import gate as gate_service  # noqa: E402
from pdm_common import incidents as lifecycle  # noqa: E402
from pdm_common.checks import load_registry  # noqa: E402
from pdm_common.events import load_events  # noqa: E402
from pdm_common.gate import StreamingGate, gate, gate_params  # noqa: E402
from pdm_common.hashing import canonical_json  # noqa: E402
from pdm_common.profile import events_file, load_profile  # noqa: E402
from pdm_common.status import StatusInputs, ne107  # noqa: E402
from pdm_common.store import Store  # noqa: E402

PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
T0 = dt.datetime(2020, 6, 1, tzinfo=dt.timezone.utc)
THRESHOLD = 5.0


@pytest.fixture(scope="module")
def profile():
    return load_profile(PROFILE_PATH)


@pytest.fixture(scope="module")
def events(profile):
    return load_events(events_file(profile, PROFILE_PATH))


@pytest.fixture()
def store():
    s = Store("sqlite://")
    s.init_schema()
    return s


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture()
def service(profile, events, store):
    return gate_service.GateService(profile, "p" * 64, events=events, registry=load_registry(profile, ROOT),
                                    store=store, clock=Clock())


def score(service, minute, value):
    end = T0 + dt.timedelta(minutes=minute)
    doc = {"schema": "score_v1", "asset": service.asset, "window_start": (end - dt.timedelta(minutes=10)).isoformat(),
           "window_end": end.isoformat().replace("+00:00", "Z"), "score": value, "threshold": THRESHOLD,
           "alert": value > THRESHOLD, "detector_run_id": "bundle-test", "errors": {}}
    return service.handle_score(canonical_json(doc))


def feed(service, values, start=0):
    outs = []
    for i, v in enumerate(values):
        outs += score(service, start + i, v)
    return outs


def incident_messages(outs, service):
    return [json.loads(o.payload) for o in outs if o.topic == service.topics["incidents"]]


def status_of(outs, service):
    return [json.loads(o.payload) for o in outs if o.topic == service.topics["status"]][-1]


# --- the streaming gate equals the batch gate ----------------------------------------------------

@pytest.mark.parametrize("pattern", ["spike", "sustained", "two_excursions", "random"])
def test_streaming_gate_opens_what_the_batch_gate_opens(profile, pattern):
    params = gate_params(profile)
    n = 24 * 60
    end = (np.arange(n) * 60 * 10**9).astype(np.int64)
    alerts = np.zeros(n, bool)
    if pattern == "spike":
        alerts[100] = True
    elif pattern == "sustained":
        alerts[100:200] = True
    elif pattern == "two_excursions":
        alerts[100:160] = alerts[220:280] = True           # an hour apart: one incident (INC-03)
    else:
        rng = np.random.default_rng(3)
        alerts = rng.random(n) < 0.6
        end = np.sort(rng.choice(np.arange(3 * n), n, replace=False)).astype(np.int64) * 60 * 10**9
    opens, _, closes = gate(end, alerts, params)
    sg = StreamingGate(params)
    events = [e for t, a in zip(end, alerts) for e in sg.add(t, float(a), a)] + sg.close_due(np.iinfo(np.int64).max)
    assert [e.at_ns for e in events if e.kind == "open"] == list(opens)
    assert [e.at_ns for e in events if e.kind == "close"] == list(closes)
    if pattern == "spike":
        assert len(opens) == 0 and [e.kind for e in events] == ["suppress"]
    if pattern == "two_excursions":
        assert len(opens) == 1


@pytest.mark.skipif(not (ROOT / "eval/out/freeze/pca/scores.npz").exists()
                    or not (ROOT / "data/processed_v4/windows_10min.csv.gz").exists(),
                    reason="needs the frozen scores (tools/freeze.py) and data/processed_v4")
def test_streaming_gate_on_the_evaluated_campaign(profile):
    scores = np.load(ROOT / "eval/out/freeze/pca/scores.npz")["scores"]
    ends = pd.read_csv(ROOT / "data/processed_v4/windows_10min.csv.gz", usecols=["window_end"],
                       parse_dates=["window_end"])["window_end"].to_numpy().astype("datetime64[ns]").astype("int64")
    threshold = 5.262356611432426
    params = gate_params(profile)
    opens, _, closes = gate(ends, scores > threshold, params)
    sg = StreamingGate(params)
    events = [e for t, s in zip(ends, scores) for e in sg.add(t, s, s > threshold)]
    events += sg.close_due(np.iinfo(np.int64).max)
    assert [e.at_ns for e in events if e.kind == "open"] == list(opens)
    assert [e.at_ns for e in events if e.kind == "close"] == list(closes)


# --- the service ------------------------------------------------------------------------------------

def test_a_one_window_spike_opens_nothing_and_is_logged(service, store):
    outs = feed(service, [1.0] * 40 + [9.0] + [1.0] * 40)
    assert incident_messages(outs, service) == []
    assert store.count("incidents") == 0
    logged = store.select("gate_log")
    assert [r["decision"] for r in logged] == ["suppress"]
    assert logged[0]["params"]["min_fraction"] == 0.8 and len(logged[0]["trajectory"]) == 30


def test_a_sustained_excursion_opens_one_incident(service, store):
    outs = feed(service, [1.0] * 40 + [9.0] * 60 + [1.0] * 30)
    opened = incident_messages(outs, service)
    assert [m["event"] for m in opened] == ["opened"]
    m = opened[0]
    # 30 windows in the span, 80 % of them must alert: the 24th alerting window opens it
    assert m["at"] == (T0 + dt.timedelta(minutes=40 + 23)).isoformat().replace("+00:00", "Z")
    assert m["incident_id"] == "INC-metropt3_apu-20200601T0103Z" and m["as_of"] == m["at"]
    row = store.select("incidents")[0]
    assert row["state"] == "open" and row["opened_at"] == m["at"] and row["detector_run_id"] == "bundle-test"
    assert [e["to_state"] for e in lifecycle.history(store, m["incident_id"])] == ["open"]
    assert [r["decision"] for r in store.select("gate_log", order="id")] == ["open"]


def test_alerts_an_hour_later_attach_and_the_episode_closes_after_the_hold_off(service, store):
    outs = feed(service, [9.0] * 40 + [1.0] * 60 + [9.0] * 40)
    assert [m["event"] for m in incident_messages(outs, service)] == ["opened"]
    outs = feed(service, [1.0] * (6 * 60 + 30), start=140)   # the last pass is a few minutes after 139
    closed = [m for m in incident_messages(outs, service) if m["event"] == "episode_closed"]
    assert len(closed) == 1
    row = store.select("incidents")[0]
    assert row["episode_closed_at"] == closed[0]["at"] and row["state"] == "open"   # the lifecycle carries on
    decisions = [r["decision"] for r in store.select("gate_log", order="id")]
    assert decisions == ["open", "attach", "close"]


def test_ne107_follows_alert_gate_incident_and_diagnosis(service, store):
    assert status_of(feed(service, [1.0] * 30), service)["ne107"] == "good"
    s = status_of(feed(service, [9.0] * 5, start=30), service)
    assert (s["ne107"], s["causes"]) == ("out_of_specification", ["alert active, gate not yet passed"])
    s = status_of(feed(service, [9.0] * 20, start=35), service)
    assert s["ne107"] == "maintenance_required" and s["causes"] == ["incident open, diagnosis pending"]
    iid = s["incident_id"]
    # the agent diagnoses an instrument fault
    lifecycle.claim(store, iid)
    store.insert("briefs", {"brief_id": "B-1", "incident_id": iid, "evidence_state": "sufficient", "valid": True,
                            "document": {"hypotheses": [{"category": "instrumentation", "likelihood": "likely"}]}})
    assert status_of(feed(service, [9.0], start=55), service)["ne107"] == "out_of_specification"
    changes = [(r["from_status"], r["to_status"]) for r in store.select("status_log", order="id")]
    assert changes == [(None, "good"), ("good", "out_of_specification"),
                       ("out_of_specification", "maintenance_required"),
                       ("maintenance_required", "out_of_specification")]


def test_no_data_turns_the_status_to_function_check(service):
    feed(service, [1.0] * 30)
    assert service.tick() == []
    service.clock.now += 301
    s = json.loads(service.tick()[0].payload)
    assert s["ne107"] == "function_check" and "no data" in s["causes"][0]


def test_out_of_range_and_drift_are_out_of_specification(service):
    window = {"schema": "window_v1", "asset": service.asset, "out_of_range": {"TP3": 2}, "features": {}}
    service.handle_window(canonical_json(window))
    assert status_of(feed(service, [1.0]), service)["causes"] == ["out-of-range reading on TP3"]
    service.handle_window(canonical_json(dict(window, out_of_range={})))
    service.handle_drift(canonical_json({"schema": "drift_v1", "asset": service.asset, "alert": True}))
    assert status_of(feed(service, [1.0], start=1), service)["causes"] == ["drift alert"]


def test_ne107_precedence():
    params = {"failure_p24_lower": 0.5, "maintenance_p72_upper": 0.5, "no_data_after": "5min"}
    assert ne107(StatusInputs(), params) == ("good", [])
    assert ne107(StatusInputs(p72_upper=0.7), params)[0] == "maintenance_required"
    assert ne107(StatusInputs(p72_upper=0.7, alert=True), params)[0] == "out_of_specification"
    assert ne107(StatusInputs(alert=True, no_data=True), params)[0] == "function_check"
    assert ne107(StatusInputs(no_data=True, p24_lower=0.6), params)[0] == "failure"
    # the continuous-load signature counts only inside an open incident
    assert ne107(StatusInputs(continuous_load=True), params)[0] == "good"
    assert ne107(StatusInputs(continuous_load=True, incident_open=True, episode_open=True), params)[0] == "failure"


def test_checks_and_triage_travel_with_the_incident(profile, service, store, spec_and_ingest):
    """The checks run on the data as of the opening, from the store, and land on the incident."""
    ingest, outs = spec_and_ingest(store)
    ends = [json.loads(o.payload)["end"] for o in outs]
    evidence = service._evidence(ends[-1])
    assert [c["id"] for c in evidence["checks"]] == [f"C{i}" for i in range(1, 12)]
    assert evidence["triage"]["policy"] == "none" and set(evidence["triage"]["rules"]) == {"A1", "A2", "A3", "A4"}
    by_id = {c["id"]: c for c in evidence["checks"]}
    assert by_id["C9"]["result"] == "pass" and by_id["C10"]["result"] == "inconclusive"


@pytest.fixture()
def spec_and_ingest(profile):
    import ingest as ingest_service
    import replayer
    from pdm_common.features import features_file, load_feature_spec
    spec = load_feature_spec(features_file(profile, PROFILE_PATH))

    def run(store):
        service = ingest_service.Ingest(profile, "p" * 64, spec=spec, store=store)
        outs = []
        for seq in range(90):
            values = {}
            for s in profile["signals"]["analog"]:
                lo, hi = s["range"]
                values[s["name"]] = lo + (hi - lo) * (0.3 + 0.001 * seq)
            values["Reservoirs"] = values["TP3"]
            for s in profile["signals"]["digital"]:
                values[s["name"]] = float(seq % 2)
            ts = dt.datetime(2020, 6, 1) + dt.timedelta(seconds=10 * seq)
            outs += service.handle(replayer.build_message(profile, seq, ts, values)[1])
        return service, outs + service.flush()
    return run


# --- the lifecycle ---------------------------------------------------------------------------------

def _incident(store, iid="INC-x"):
    lifecycle.open_incident(store, {"incident_id": iid, "asset_id": "a", "opened_at": "2020-06-01T00:00:00Z"},
                            cause="test")
    return iid


def test_the_lifecycle_refuses_illegal_moves(store):
    iid = _incident(store)
    with pytest.raises(lifecycle.IllegalTransition):
        lifecycle.transition(store, iid, "approved", "reviewer:ashok", "skip the agent")
    with pytest.raises(lifecycle.IllegalTransition, match="only agent"):
        lifecycle.transition(store, iid, "diagnosing", "reviewer:ashok", "not the agent")
    assert lifecycle.claim(store, iid)
    lifecycle.transition(store, iid, "awaiting_review", "agent", "brief B-1 submitted")
    with pytest.raises(lifecycle.IllegalTransition, match="not a transition"):
        lifecycle.transition(store, iid, "sent", "connector", "nothing was approved")
    lifecycle.transition(store, iid, "approved", "reviewer:gayatri", "approved")
    with pytest.raises(lifecycle.IllegalTransition, match="only connector"):
        lifecycle.transition(store, iid, "sent", "reviewer:ashok", "send it myself")
    lifecycle.transition(store, iid, "sent", "connector", "WO-1 accepted")
    lifecycle.transition(store, iid, "closed", "system", "work order sent")
    events = lifecycle.history(store, iid)
    assert [e["to_state"] for e in events] == ["open", "diagnosing", "awaiting_review", "approved", "sent", "closed"]
    assert lifecycle.replay_state(events) == store.select("incidents")[0]["state"] == "closed"
    with pytest.raises(Exception, match="insert-only"):
        store.execute("DELETE FROM incident_events")


def test_a_duplicate_trigger_yields_one_claim(store):
    iid = _incident(store)
    assert lifecycle.claim(store, iid, "agent:run-1") is True
    assert lifecycle.claim(store, iid, "agent:run-2") is False
    assert [e["actor"] for e in lifecycle.history(store, iid)] == ["gate", "agent:run-1"]


def test_reopening_the_same_incident_is_a_no_op(store):
    _incident(store, "INC-y")
    assert lifecycle.open_incident(store, {"incident_id": "INC-y", "asset_id": "a"}, cause="replay") is False
    assert store.count("incident_events") == 1


def test_the_opening_cause_reads_like_the_profile():
    assert [gate_service.short_duration(n * 10**9) for n in (1800, 21600, 86400, 90, 0)] == \
        ["30min", "6h", "1d", "90s", "0s"]


def test_a_sent_incident_closes_when_its_episode_ends(service, store):
    outs = feed(service, [1.0] * 40 + [9.0] * 60)
    iid = incident_messages(outs, service)[0]["incident_id"]
    for to, actor in (("diagnosing", "agent"), ("awaiting_review", "agent"), ("approved", "reviewer:rosh"),
                      ("sent", "connector")):
        lifecycle.transition(store, iid, to, actor, "test")
    status = status_of(feed(service, [1.0] * 5, start=100), service)
    assert status["incident_state"] == "sent" and status["ne107"] == "maintenance_required"
    outs = feed(service, [1.0] * (6 * 60 + 30), start=105)
    assert [m["event"] for m in incident_messages(outs, service)] == ["episode_closed"]
    assert store.select("incidents", {"incident_id": iid})[0]["state"] == "closed"
    assert lifecycle.history(store, iid)[-1]["actor"] == "system"
    assert status_of(outs, service)["ne107"] == "good"                     # nothing open: the asset is good again
