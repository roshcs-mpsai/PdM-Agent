"""The REST work-order connector (T22; INT-04 to INT-06, HIL-06).

Runs services/connector against the simulated work-order system
(tools/workorder_sim.py) through starlette's TestClient: no server, no network
and an injected clock, so retries and backoff are checked without waiting.
Incidents move through the real lifecycle and decisions are queued the way the
approval API queues them (pdm_common.workorders.queue, in the decision's
transaction).
"""
import datetime as dt
import json
import logging
import os
import signal
import sys
from pathlib import Path

import pytest

httpx = pytest.importorskip("httpx", reason="the connector and starlette's TestClient need httpx")
TestClient = pytest.importorskip("starlette.testclient").TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "connector"))
sys.path.insert(0, str(ROOT / "tools"))

import connector as connector_service  # noqa: E402
import workorder_sim  # noqa: E402
from pdm_common import incidents as lifecycle  # noqa: E402
from pdm_common import metrics, workorders  # noqa: E402
from pdm_common.hashing import canonical_json  # noqa: E402
from pdm_common.store import Store  # noqa: E402

URL = "http://testserver/api/work-orders"
TOKEN = "test-token"
NOW = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc)
ASSET = "test_asset"
IID = "INC-test_asset-20200715T0030Z"
IID2 = "INC-test_asset-20200801T0000Z"
LONG_AGO = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)


def at(seconds: float) -> dt.datetime:
    return NOW + dt.timedelta(seconds=seconds)


# --- metrics -------------------------------------------------------------------------------------

def value(name, **labels) -> float:
    return metrics.REGISTRY.value(name, **labels) or 0.0


def counters() -> dict:
    out = {o: value("pdm_work_order_posts_total", outcome=o) for o in connector_service.OUTCOMES}
    out.update({r: value("pdm_work_orders_blocked_total", reason=r) for r in connector_service.BLOCK_REASONS})
    return out


def delta(before: dict) -> dict:
    """The counters that moved since ``before``: posts by outcome, blocks by reason."""
    after = counters()
    return {k: after[k] - before[k] for k in after if after[k] != before[k]}


# --- the world: store, incidents, decisions, the endpoint -------------------------------------------

@pytest.fixture()
def store():
    s = Store("sqlite://")
    s.init_schema()
    return s


PATHS = {
    "awaiting_review": [("diagnosing", "agent"), ("awaiting_review", "agent")],
    "approved": [("diagnosing", "agent"), ("awaiting_review", "agent"), ("approved", "reviewer:ashok")],
    "rejected": [("diagnosing", "agent"), ("awaiting_review", "agent"), ("rejected", "reviewer:ashok")],
}


def incident(store, iid=IID, state="approved", asset=ASSET) -> str:
    """An incident moved through the real lifecycle to ``state``."""
    lifecycle.open_incident(store, {"incident_id": iid, "asset_id": asset, "opened_at": "2020-07-15T00:30:00Z"},
                            cause="gate passed (test)", at=NOW - dt.timedelta(hours=3))
    for to, actor in PATHS[state]:
        lifecycle.transition(store, iid, to, actor, f"test: {to}", at=NOW - dt.timedelta(hours=1))
    return iid


def decision(n=1, iid=IID, action="approve", decided_at=None, **changes) -> dict:
    d = {"decision_id": f"D-{n:012x}", "brief_id": f"B-{n}", "incident_id": iid, "reviewer": "ashok",
         "action": action, "reason": "the evidence supports a leak",
         "work_order": {"title": "Leak survey on the air path", "asset": ASSET, "priority": "high",
                        "tasks": ["Ultrasonic leak survey of fittings, hoses and drains",
                                  "Pressure-decay test section by section"],
                        "safety_notes": ["Isolate, lock out and vent to zero before loosening anything"],
                        "status": "draft"},
         "diff": "", "evidence_sha256": "e" * 64, "versions": {"llm": "pinned"}, "trace_id": f"T-{n}",
         "at": decided_at or NOW - dt.timedelta(minutes=2)}
    d.update(changes)
    return d


def approve(store, n=1, iid=IID, due=None, **kwargs):
    """What the approval API does: the decision and its queued work order in one transaction."""
    d = decision(n, iid, **kwargs)
    with store.transaction():
        store.insert("decisions", d)
        row = workorders.queue(store, d, ASSET, at=due)
    return d, row


def hand_made_row(store, d, **changes) -> dict:
    """A work_orders row written straight into the store, as if the API had been bypassed."""
    body = workorders.payload(d, ASSET)
    row = {"wo_id": body["wo_id"], "decision_id": d["decision_id"], "incident_id": d["incident_id"],
           "asset_id": ASSET, "body": body, "text_sha256": body["text_sha256"], "status": "queued", "attempts": 0,
           "next_attempt_at": NOW, "updated_at": NOW}
    row.update(changes)
    store.insert("work_orders", row)
    return row


def sim_client(fail_first=0) -> TestClient:
    return TestClient(workorder_sim.create_app(token=TOKEN, fail_first=fail_first, clock=lambda: NOW))


def auth(key: str) -> dict:
    return {"Authorization": "Bearer " + TOKEN, "Idempotency-Key": key}


def orders(sim) -> list:
    """The orders the endpoint holds."""
    response = sim.get(URL, headers={"Authorization": "Bearer " + TOKEN})
    assert response.status_code == 200
    return response.json()["orders"]


def make(store, client, **kwargs):
    token = kwargs.pop("token", TOKEN)
    return connector_service.Connector(store, URL, token, client=client, clock=lambda: NOW, **kwargs)


def wo(store, wo_id=None) -> dict:
    rows = store.select("work_orders", {"wo_id": wo_id} if wo_id else None)
    assert len(rows) == 1
    return rows[0]


def state(store, iid=IID) -> str:
    return store.one("SELECT state FROM incidents WHERE incident_id = ?", [iid])["state"]


class Scripted:
    """An httpx-compatible client that gives the answers it was handed, one per POST
    (an exception is raised), and keeps the requests."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class LoseFirstAnswer:
    """Passes every POST on to the endpoint, but loses the answer to the first one."""

    def __init__(self, client):
        self.client = client
        self.lost = False

    def post(self, url, **kwargs):
        response = self.client.post(url, **kwargs)
        if not self.lost:
            self.lost = True
            raise httpx.ReadTimeout("the answer was lost on the way back")
        return response


# --- INT-04: sent once, retried with backoff ------------------------------------------------------------

def test_an_approved_order_is_sent_once(store, caplog):
    caplog.set_level(logging.DEBUG)
    incident(store)
    d, row = approve(store)
    sim = sim_client()
    conn = make(store, sim)
    before = counters()
    assert conn.run_once(NOW) == {"due": 1, "sent": 1, "duplicate": 0, "retry": 0, "failed": 0, "blocked": 0,
                                  "skipped": 0, "error": 0, "pending": 0}
    sent = wo(store)
    assert (sent["status"], sent["reference"], sent["attempts"], sent["sent_at"], sent["last_error"]) == \
        ("sent", "WO-SIM-0001", 1, "2026-10-10T12:00:00Z", None)
    got = orders(sim)
    assert len(got) == 1 and got[0]["approval_id"] == d["decision_id"] and got[0]["order"] == row["body"]
    assert state(store) == "sent"
    last = lifecycle.history(store, IID)[-1]
    assert (last["from_state"], last["to_state"], last["actor"], last["at"]) == \
        ("approved", "sent", "connector", "2026-10-10T12:00:00Z")
    assert "WO-000000000001" in last["cause"] and "WO-SIM-0001" in last["cause"]
    assert delta(before) == {"sent": 1}
    assert value("pdm_work_orders_pending") == 0
    assert conn.run_once(at(3600))["due"] == 0 and len(orders(sim)) == 1         # nothing left to send
    assert TOKEN not in caplog.text and TOKEN not in json.dumps(conn.report())


def test_the_request_carries_the_token_and_the_idempotency_key(store):
    incident(store)
    d, row = approve(store)
    client = Scripted(httpx.Response(201, json={"reference": "R-1"}))
    make(store, client).run_once(NOW)
    [(url, request)] = client.requests
    assert url == URL
    assert request["headers"] == {"Authorization": "Bearer " + TOKEN, "Idempotency-Key": d["decision_id"],
                                  "Content-Type": "application/json"}
    assert request["content"] == canonical_json(row["body"]).encode()        # the stored body, byte for byte


def test_injected_failures_are_retried_with_a_growing_wait(store):
    incident(store)
    approve(store)
    sim = sim_client(fail_first=2)
    conn = make(store, sim)
    before = counters()
    assert conn.run_once(NOW)["retry"] == 1
    row = wo(store)
    assert (row["status"], row["attempts"], row["next_attempt_at"]) == ("failed", 1, "2026-10-10T12:00:05Z")
    assert row["last_error"].startswith("HTTP 503") and value("pdm_work_orders_pending") == 1
    assert conn.run_once(at(4))["due"] == 0                                    # not due before its time
    assert conn.run_once(at(5))["retry"] == 1
    row = wo(store)
    assert (row["status"], row["attempts"], row["next_attempt_at"]) == ("failed", 2, "2026-10-10T12:00:15Z")  # +10 s
    assert orders(sim) == [] and state(store) == "approved"
    assert conn.run_once(at(15))["sent"] == 1
    row = wo(store)
    assert (row["status"], row["attempts"], row["reference"], row["last_error"]) == ("sent", 3, "WO-SIM-0001", None)
    assert len(orders(sim)) == 1 and state(store) == "sent"
    assert delta(before) == {"retry": 2, "sent": 1}
    assert value("pdm_work_orders_pending") == 0


def test_the_wait_doubles_up_to_its_cap(store):
    incident(store)
    approve(store)
    client = Scripted(*[httpx.Response(503)] * 4, httpx.Response(201, json={"reference": "R-1"}))
    conn = make(store, client, backoff_s=5, backoff_max_s=15)
    t, waits = 0.0, []
    for _ in range(4):
        assert conn.run_once(at(t))["retry"] == 1
        nxt = (dt.datetime.fromisoformat(wo(store)["next_attempt_at"].replace("Z", "+00:00")) - NOW).total_seconds()
        waits.append(nxt - t)
        t = nxt
    assert waits == [5, 10, 15, 15]
    assert conn.run_once(at(t))["sent"] == 1


def test_after_max_attempts_the_order_is_blocked(store, caplog):
    caplog.set_level(logging.ERROR, logger="connector")
    incident(store)
    approve(store)
    sim = sim_client(fail_first=10)
    conn = make(store, sim, max_attempts=3)
    before = counters()
    assert conn.run_once(NOW)["retry"] == 1
    assert conn.run_once(at(5))["retry"] == 1
    assert conn.run_once(at(15))["failed"] == 1
    row = wo(store)
    assert (row["status"], row["attempts"]) == ("blocked", 3)
    assert row["last_error"].startswith("gave up after 3 attempts") and "503" in row["last_error"]
    assert delta(before) == {"retry": 2, "failed": 1}
    assert orders(sim) == [] and state(store) == "approved"
    assert "no answer after 3 attempts" in caplog.text
    assert conn.run_once(at(3600))["due"] == 0                                # a person has to look at it


@pytest.mark.parametrize("how", ["posted_before", "answer_lost", "incident_already_sent"])
def test_a_lost_answer_gets_the_same_order_back(store, how):
    """The order reached the endpoint but the row never heard: the retry gets 200 and the
    same reference, and the endpoint still holds one order."""
    incident(store)
    d, row = approve(store)
    sim = sim_client()
    before = counters()
    if how == "answer_lost":
        conn = make(store, LoseFirstAnswer(sim))
        assert conn.run_once(NOW)["retry"] == 1
        assert wo(store)["status"] == "failed" and "ReadTimeout" in wo(store)["last_error"]
        assert len(orders(sim)) == 1
        assert conn.run_once(at(5))["duplicate"] == 1
        assert delta(before) == {"retry": 1, "duplicate": 1}
    else:
        assert sim.post(URL, json=row["body"], headers=auth(d["decision_id"])).status_code == 201
        if how == "incident_already_sent":                 # the incident moved, the row's update was lost
            lifecycle.transition(store, IID, "sent", "connector", "test: sent before the row was updated")
        assert make(store, sim).run_once(NOW)["duplicate"] == 1
        assert delta(before) == {"duplicate": 1}
    row = wo(store)
    assert (row["status"], row["reference"]) == ("sent", "WO-SIM-0001")
    assert len(orders(sim)) == 1 and state(store) == "sent"
    assert [e["to_state"] for e in lifecycle.history(store, IID)].count("sent") == 1


def test_rows_are_taken_oldest_first(store):
    incident(store)
    incident(store, IID2)
    approve(store, 1, decided_at=NOW - dt.timedelta(minutes=1))
    approve(store, 2, IID2, decided_at=NOW - dt.timedelta(minutes=5))
    approve(store, 3, IID2, decided_at=NOW - dt.timedelta(minutes=3), due=at(3600))     # not due yet
    client = Scripted(httpx.Response(201, json={"reference": "R-1"}), httpx.Response(503))
    counts = make(store, client).run_once(NOW)
    assert [r["headers"]["Idempotency-Key"] for _, r in client.requests] == ["D-000000000002", "D-000000000001"]
    assert (counts["due"], counts["sent"], counts["retry"], counts["pending"]) == (2, 1, 1, 2)   # failed + queued
    assert value("pdm_work_orders_pending") == 2


@pytest.mark.parametrize("answer, status, outcome", [
    (httpx.Response(201, json={"reference": "R-1"}), "sent", "sent"),
    (httpx.Response(202, json={"reference": "R-1"}), "sent", "sent"),
    (httpx.Response(200, json={"reference": "R-1"}, headers={"Idempotent-Replayed": "true"}), "sent", "duplicate"),
    (httpx.Response(204), "sent", "sent"),
    (httpx.Response(400, json={"error": "bad request"}), "blocked", "blocked"),
    (httpx.Response(403, json={"error": "forbidden"}), "blocked", "blocked"),
    (httpx.Response(404), "blocked", "blocked"),
    (httpx.Response(422, json={"error": "not a work order"}), "blocked", "blocked"),
    (httpx.Response(307, headers={"Location": "http://elsewhere/"}), "blocked", "blocked"),
    (httpx.Response(408), "failed", "retry"),
    (httpx.Response(429), "failed", "retry"),
    (httpx.Response(500), "failed", "retry"),
    (httpx.Response(502, text="<html>bad gateway</html>"), "failed", "retry"),
    (httpx.ConnectError("connection refused"), "failed", "retry"),
    (httpx.ReadTimeout("timed out"), "failed", "retry"),
])
def test_each_answer_has_its_outcome(store, answer, status, outcome):
    incident(store)
    approve(store)
    before = counters()
    assert make(store, Scripted(answer)).run_once(NOW)[outcome] == 1
    row = wo(store)
    assert row["status"] == status and row["attempts"] == 1
    if status == "sent":
        reference = None if answer.status_code == 204 else "R-1"
        assert row["reference"] == reference and state(store) == "sent"
        assert delta(before) == {outcome: 1}
    elif status == "blocked":
        assert row["last_error"].startswith(f"HTTP {answer.status_code}") and state(store) == "approved"
        assert delta(before) == {"failed": 1, "rejected_by_endpoint": 1}
    else:
        assert row["next_attempt_at"] == "2026-10-10T12:00:05Z" and state(store) == "approved"
        assert delta(before) == {"retry": 1}


# --- INT-05: authentication ---------------------------------------------------------------------------

def test_a_refused_token_blocks_and_the_token_stays_out_of_logs(store, caplog):
    caplog.set_level(logging.DEBUG)
    incident(store)
    approve(store)
    sim = sim_client()
    wrong = "not-the-token"
    conn = make(store, sim, token=wrong)
    before = counters()
    assert conn.run_once(NOW)["blocked"] == 1
    row = wo(store)
    assert row["status"] == "blocked" and row["last_error"].startswith("HTTP 401")
    assert orders(sim) == [] and state(store) == "approved"
    assert delta(before) == {"failed": 1, "rejected_by_endpoint": 1}
    assert wrong not in caplog.text and wrong not in row["last_error"] and wrong not in json.dumps(conn.report())


def test_an_echoed_token_is_scrubbed_from_the_error(store):
    incident(store)
    approve(store)
    conn = make(store, Scripted(httpx.Response(400, text=f"you sent {TOKEN}, which we refuse")))
    conn.run_once(NOW)
    assert TOKEN not in wo(store)["last_error"] and "[token]" in wo(store)["last_error"]


def test_the_connector_will_not_start_without_a_token(store, monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="INT-05"):
        connector_service.Connector(store, URL, "", client=sim_client())
    with pytest.raises(ValueError, match="http"):
        connector_service.Connector(store, "localhost:8088/api/work-orders", TOKEN, client=sim_client())
    monkeypatch.delenv("PDM_WORKORDER_TOKEN", raising=False)
    monkeypatch.delenv("PDM_TOKEN_CONNECTOR", raising=False)
    monkeypatch.setattr(connector_service.logs, "setup", lambda *a, **k: None)
    db = tmp_path / "pdm.db"
    with pytest.raises(SystemExit, match="PDM_WORKORDER_TOKEN"):
        connector_service.main(["--once", "--store", f"sqlite:///{db}"])
    assert not db.exists()                                     # refused before the store was opened


# --- INT-06: the tamper check ----------------------------------------------------------------------------

@pytest.mark.parametrize("case", ["title", "row_hash", "rehashed", "extra_field", "asset"])
def test_a_changed_order_is_blocked_and_never_sent(store, caplog, case):
    """The stored order is edited after the approval: blocked as tampered, an error logged
    (the alert), nothing sent, the incident left approved."""
    caplog.set_level(logging.ERROR, logger="connector")
    incident(store)
    d, row = approve(store)
    body = dict(row["body"])
    if case == "title":
        body["title"] = "Replace the compressor"
        changes = {"body": body}
    elif case == "row_hash":
        changes = {"text_sha256": "0" * 64}
    elif case == "rehashed":                 # new text, and both hashes recomputed to match it
        body["tasks"] = ["Replace the compressor"]
        body["text_sha256"] = workorders.text_sha256(dict(d["work_order"], tasks=body["tasks"]))
        changes = {"body": body, "text_sha256": body["text_sha256"]}
    elif case == "extra_field":
        body["override"] = "skip the lock-out"
        changes = {"body": body}
    else:                                    # the order moved to another asset, row and body alike
        body["asset_id"] = "other_asset"
        changes = {"body": body, "asset_id": "other_asset"}
    store.update("work_orders", {"wo_id": row["wo_id"]}, changes)
    sim = sim_client()
    conn = make(store, sim)
    before = counters()
    assert conn.run_once(NOW)["blocked"] == 1
    blocked = wo(store)
    assert blocked["status"] == "blocked" and blocked["last_error"].startswith("tampered:"), blocked["last_error"]
    assert blocked["attempts"] == 0 and orders(sim) == [] and state(store) == "approved"
    assert delta(before) == {"tampered": 1}
    assert any(r.levelno == logging.ERROR and "blocked (tampered)" in r.getMessage() for r in caplog.records)
    assert conn.run_once(at(3600))["due"] == 0 and orders(sim) == []


# --- HIL-06: nothing goes out without an approval record ----------------------------------------------------

@pytest.mark.parametrize("case", ["no_decision", "no_decision_no_due_time", "reject", "not_approved",
                                  "other_incident"])
def test_no_approval_record_no_order(store, case):
    if case.startswith("no_decision"):
        incident(store)
        extra = {"next_attempt_at": None} if case == "no_decision_no_due_time" else {}
        hand_made_row(store, decision(), **extra)              # the decision itself was never stored
    elif case == "reject":
        incident(store, state="rejected")
        d = decision(action="reject")
        store.insert("decisions", d)
        with pytest.raises(ValueError):
            workorders.queue(store, d, ASSET)                  # the API's path refuses it...
        hand_made_row(store, d)                                 # ...so the row is written by hand
    elif case == "not_approved":
        incident(store, state="awaiting_review")
        approve(store)
    else:
        incident(store)
        incident(store, IID2)
        d = decision(iid=IID)
        store.insert("decisions", d)
        hand_made_row(store, d, incident_id=IID2)               # A's approval on B's work order
    sim = sim_client()
    before = counters()
    counts = make(store, sim).run_once(NOW)
    assert counts["blocked"] == 1 and counts["sent"] == 0
    row = wo(store)
    assert row["status"] == "blocked" and row["last_error"].startswith("no_approval:"), row["last_error"]
    assert orders(sim) == []
    assert delta(before) == {"no_approval": 1}
    assert "sent" not in [e["to_state"] for e in lifecycle.history(store, IID)]


# --- two connectors, crashes, conflicts ------------------------------------------------------------------

def test_a_conflicting_order_is_blocked_and_not_resent(store):
    """The endpoint already holds a different body under this approval: 409, blocked, and
    the connector does not try again on its own."""
    incident(store)
    d, row = approve(store)
    sim = sim_client()
    other = dict(row["body"], tasks=["Replace the compressor"])
    assert sim.post(URL, json=other, headers=auth(d["decision_id"])).status_code == 201
    conn = make(store, sim)
    before = counters()
    assert conn.run_once(NOW)["blocked"] == 1
    blocked = wo(store)
    assert (blocked["status"], blocked["attempts"]) == ("blocked", 1) and blocked["last_error"].startswith("HTTP 409")
    assert delta(before) == {"failed": 1, "conflict": 1}
    assert conn.run_once(at(3600))["due"] == 0
    assert [o["order"] for o in orders(sim)] == [other] and state(store) == "approved"


def test_a_row_changed_by_someone_else_is_skipped(store):
    incident(store)
    approve(store)
    sim = sim_client()
    conn = make(store, sim)
    stale = conn.due(NOW)[0]
    assert conn.run_once(NOW)["sent"] == 1
    assert conn.process(stale, NOW) == "skipped"              # another connector's copy of the row
    assert len(orders(sim)) == 1
    assert [e["to_state"] for e in lifecycle.history(store, IID)].count("sent") == 1

    incident(store, IID2)
    approve(store, 2, IID2)
    stale = conn.due(NOW)[0]
    store.update("work_orders", {"wo_id": stale["wo_id"]}, {"status": "blocked", "last_error": "held by a planner"})
    assert conn.process(stale, NOW) == "skipped"
    assert len(orders(sim)) == 1 and wo(store, stale["wo_id"])["last_error"] == "held by a planner"


def test_a_second_connector_skips_a_row_in_flight(store):
    incident(store)
    approve(store)
    sim = sim_client()
    other = make(store, sim)
    seen = []

    class Interleaved:
        def post(self, url, **kwargs):
            seen.append(other.run_once(NOW))                 # runs while this request is in flight
            return sim.post(url, **kwargs)

    assert make(store, Interleaved()).run_once(NOW)["sent"] == 1
    assert seen[0]["due"] == 0 and len(orders(sim)) == 1


def test_a_crash_mid_request_leaves_the_row_for_after_the_lease(store):
    incident(store)
    approve(store)

    class Crash:
        def post(self, url, **kwargs):
            raise RuntimeError("the process died here")

    assert make(store, Crash()).run_once(NOW)["error"] == 1
    row = wo(store)
    assert (row["status"], row["attempts"], row["next_attempt_at"]) == ("queued", 0, "2026-10-10T12:01:00Z")
    sim = sim_client()
    conn = make(store, sim)
    assert conn.run_once(at(59))["due"] == 0
    assert conn.run_once(at(60))["sent"] == 1 and len(orders(sim)) == 1


# --- the service ---------------------------------------------------------------------------------------

def _store_with_an_approved_order(tmp_path) -> str:
    url = f"sqlite:///{tmp_path / 'pdm.db'}"
    s = Store(url)
    s.init_schema()
    incident(s)
    approve(s, decided_at=LONG_AGO)                          # due by any wall clock
    s.close()
    return url


def test_main_runs_once(tmp_path, monkeypatch, capsys):
    url = _store_with_an_approved_order(tmp_path)
    app = workorder_sim.create_app(token=TOKEN, fail_first=0, clock=lambda: NOW)
    monkeypatch.setenv("PDM_WORKORDER_TOKEN", TOKEN)
    monkeypatch.setattr(connector_service, "default_client", lambda *a, **k: TestClient(app))
    monkeypatch.setattr(connector_service.logs, "setup", lambda *a, **k: None)
    assert connector_service.main(["--once", "--store", url, "--endpoint", URL]) == 0
    out = capsys.readouterr().out
    report = json.loads(out)
    assert (report["sent"], report["runs"], report["pending"]) == (1, 1, 0) and TOKEN not in out
    s = Store(url)
    assert wo(s)["status"] == "sent" and state(s) == "sent"
    s.close()
    assert len(orders(TestClient(app))) == 1


def test_sigterm_stops_the_loop_after_the_row_in_hand(tmp_path, monkeypatch, capsys):
    """The loop serves metrics, and SIGTERM (compose's stop) ends it once the current row is
    done; the signal handlers in place before are put back."""
    url = _store_with_an_approved_order(tmp_path)
    app = workorder_sim.create_app(token=TOKEN, fail_first=0, clock=lambda: NOW)

    class TermDuringPost:
        posts = 0

        def post(self, url, **kwargs):
            TermDuringPost.posts += 1
            os.kill(os.getpid(), signal.SIGTERM)
            return TestClient(app).post(url, **kwargs)

        def close(self):
            pass

    class Stuck(BaseException):
        """Ends main() from inside the test: not an Exception, so main() does not catch it."""

    real_run_once = connector_service.Connector.run_once
    runs = []

    def run_once_or_fail(self, now=None):
        """Never let a regression hang the suite: a second run means SIGTERM did not stop the
        loop; a run that posted nothing sends SIGTERM itself, and the asserts below fail."""
        runs.append(1)
        if len(runs) > 1:
            raise Stuck("the loop kept running after SIGTERM")
        try:
            return real_run_once(self, now)
        finally:
            if not TermDuringPost.posts:
                os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setenv("PDM_WORKORDER_TOKEN", TOKEN)
    monkeypatch.setenv("PDM_METRICS_PORT", "0")                # any free port
    monkeypatch.setattr(connector_service, "default_client", lambda *a, **k: TermDuringPost())
    monkeypatch.setattr(connector_service.Connector, "run_once", run_once_or_fail)
    monkeypatch.setattr(connector_service.logs, "setup", lambda *a, **k: None)

    def unhandled(signum, frame):
        raise Stuck("main() left SIGTERM unhandled")

    original = signal.signal(signal.SIGTERM, unhandled)
    try:
        code = connector_service.main(["--store", url, "--endpoint", URL, "--interval", "1"])
        assert signal.getsignal(signal.SIGTERM) is unhandled
    except Stuck as exc:
        pytest.fail(str(exc))
    finally:
        signal.signal(signal.SIGTERM, original)
    assert code == 0 and TermDuringPost.posts == 1
    report = json.loads(capsys.readouterr().out)
    assert (report["sent"], report["runs"]) == (1, 1)
    assert len(orders(TestClient(app))) == 1


def test_stop_takes_no_more_rows(store):
    incident(store)
    approve(store)
    conn = make(store, Scripted())
    conn.stop()
    counts = conn.run_once(NOW)
    assert counts["due"] == 1 and counts["pending"] == 1 and sum(counts[k] for k in connector_service.RUN_KEYS[1:]) == 0
    assert wo(store)["status"] == "queued"


def test_an_order_sent_after_the_episode_ended_closes_the_incident(store):
    incident(store)
    store.update("incidents", {"incident_id": IID}, {"episode_closed_at": "2020-07-15T09:00:00Z"})
    approve(store)
    assert make(store, sim_client()).run_once(NOW)["sent"] == 1
    assert state(store) == "closed"
    moves = [(e["from_state"], e["to_state"], e["actor"]) for e in lifecycle.history(store, IID)][-2:]
    assert moves == [("approved", "sent", "connector"), ("sent", "closed", "connector")]
