"""Work orders: approved text, hash, queue and the checks before sending (T22; INT-04, INT-06, HIL-06).

pdm_common.workorders is shared by the approval API, which queues a work order
with its decision, and the connector, which asks send_problems before every
POST. These tests hold the contract both rely on.
"""
import datetime as dt
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import workorder_sim  # noqa: E402
from pdm_common import workorders  # noqa: E402
from pdm_common.hashing import canonical_json  # noqa: E402
from pdm_common.store import Store  # noqa: E402

NOW = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc)
ASSET = "test_asset"
IID = "INC-test_asset-20200715T0030Z"


def work_order(**changes) -> dict:
    wo = {"title": "Leak survey on the air path", "asset": ASSET, "priority": "high",
          "tasks": ["Ultrasonic leak survey of fittings, hoses and drains", "Pressure-decay test section by section"],
          "safety_notes": ["Isolate, lock out and vent to zero before loosening anything"], "status": "draft"}
    wo.update(changes)
    return wo


def decision(n=1, **changes) -> dict:
    d = {"decision_id": f"D-{n:012x}", "brief_id": f"B-{n}", "incident_id": IID, "reviewer": "ashok",
         "action": "approve", "reason": "the evidence supports a leak", "work_order": work_order(), "diff": "",
         "evidence_sha256": "e" * 64, "versions": {"llm": "pinned"}, "trace_id": f"T-{n}",
         "at": NOW - dt.timedelta(minutes=2)}
    d.update(changes)
    return d


@pytest.fixture()
def store():
    s = Store("sqlite://")
    s.init_schema()
    return s


@pytest.fixture()
def queued(store):
    """A work order queued the way the API does it, and its decision as the store returns it."""
    d = decision()
    with store.transaction():
        store.insert("decisions", d)
        row = workorders.queue(store, d, ASSET)
    return row, store.select("decisions", {"decision_id": d["decision_id"]})[0]


# --- the approved text and its hash --------------------------------------------------------------

def test_approved_text_is_the_five_fields_and_never_the_status():
    assert workorders.approved_text(work_order()) == {k: work_order()[k] for k in workorders.APPROVED_FIELDS}
    assert "status" not in workorders.approved_text(work_order())
    assert workorders.approved_text({"title": "Only a title"}) == {
        "title": "Only a title", "asset": None, "priority": None, "tasks": None, "safety_notes": None}


def test_text_sha256_ignores_key_order_and_the_draft_status():
    wo = work_order()
    reordered = dict(reversed(list(wo.items())))
    no_status = {k: v for k, v in wo.items() if k != "status"}
    digest = workorders.text_sha256(wo)
    assert digest == workorders.text_sha256(reordered) == workorders.text_sha256(no_status)
    assert digest == workorders.text_sha256(dict(wo, status="approved"))
    assert digest == hashlib.sha256(canonical_json(workorders.approved_text(wo)).encode()).hexdigest()
    assert len(digest) == 64 and digest == digest.lower()
    for field, value in [("title", "Replace the compressor"), ("asset", "other_asset"), ("priority", "low"),
                         ("tasks", wo["tasks"][:1]), ("safety_notes", [])]:
        assert workorders.text_sha256(dict(wo, **{field: value})) != digest, field


def test_wo_id_follows_the_decision_id():
    assert workorders.wo_id_for("D-0123456789ab") == "WO-0123456789ab"
    for bad in ("D-0123", "X-0123456789ab", "D-0123456789abc", "D-0123456789az", "", None, 12):
        with pytest.raises(ValueError):
            workorders.wo_id_for(bad)


# --- the body ----------------------------------------------------------------------------------------

def test_payload_is_a_body_the_endpoint_accepts():
    d = decision(at="2026-10-10T13:58:00+02:00")
    body = workorders.payload(d, ASSET)
    assert set(body) == set(workorder_sim.REQUIRED) | {"brief_id"}
    assert workorder_sim.problems(body) == []
    assert body["approval_id"] == d["decision_id"] and body["wo_id"] == "WO-000000000001"
    assert (body["incident_id"], body["asset_id"], body["approved_by"], body["brief_id"]) == (IID, ASSET, "ashok", "B-1")
    assert body["approved_at"] == "2026-10-10T11:58:00Z"                     # UTC with Z
    assert body["text_sha256"] == workorders.text_sha256(d["work_order"])
    assert {k: body[k] for k in ("title", "priority", "tasks", "safety_notes")} == \
        {k: work_order()[k] for k in ("title", "priority", "tasks", "safety_notes")}


def test_payload_refuses_an_incomplete_decision():
    for changes in ({"reviewer": None}, {"at": None}, {"work_order": None}, {"decision_id": "D-1"}):
        with pytest.raises(ValueError):
            workorders.payload(decision(**changes), ASSET)
    with pytest.raises(ValueError):
        workorders.payload(decision(), "")


# --- the queue ---------------------------------------------------------------------------------------

def test_queue_inserts_a_queued_row_with_its_body_and_hash(store, queued):
    row, d = queued
    assert row == store.select("work_orders")[0]
    assert (row["wo_id"], row["decision_id"], row["incident_id"], row["asset_id"]) == \
        ("WO-000000000001", "D-000000000001", IID, ASSET)
    assert (row["status"], row["attempts"], row["reference"], row["sent_at"], row["last_error"]) == \
        ("queued", 0, None, None, None)
    assert row["next_attempt_at"] == row["updated_at"] == "2026-10-10T11:58:00Z"   # due from the decision time
    assert row["body"] == workorders.payload(d, ASSET)
    assert row["text_sha256"] == row["body"]["text_sha256"] == workorders.text_sha256(d["work_order"])


def test_queue_takes_an_explicit_due_time(store):
    d = decision(action="edit", work_order=work_order(title="Edited by the reviewer"))
    store.insert("decisions", d)
    row = workorders.queue(store, d, ASSET, at=NOW + dt.timedelta(minutes=5))
    assert row["next_attempt_at"] == "2026-10-10T12:05:00Z" and row["body"]["title"] == "Edited by the reviewer"


@pytest.mark.parametrize("action", ["reject", None, "approved"])
def test_queue_refuses_anything_but_approve_or_edit(store, action):
    d = decision(action=action)
    store.insert("decisions", d)
    with pytest.raises(ValueError, match="only an approve or edit"):
        workorders.queue(store, d, ASSET)
    assert store.count("work_orders") == 0


def test_queue_commits_and_rolls_back_with_the_decision(store):
    """The API inserts the decision and queues its work order in one transaction."""
    d = decision()
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.insert("decisions", d)
            workorders.queue(store, d, ASSET)
            raise RuntimeError("the API failed after queueing")
    assert store.count("decisions") == 0 and store.count("work_orders") == 0
    with store.transaction():
        store.insert("decisions", d)
        workorders.queue(store, d, ASSET)
    assert store.count("decisions") == 1 and store.count("work_orders") == 1
    with pytest.raises(Exception):                         # one work order per decision
        workorders.queue(store, d, ASSET)
    assert store.count("work_orders") == 1


# --- before sending ----------------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["approved", "sent"])
def test_an_untouched_approved_work_order_may_be_sent(queued, state):
    row, d = queued
    assert workorders.send_problems(row, d, state) == []


def _reasons(problems):
    return [workorders.problem_reason(p) for p in problems]


@pytest.mark.parametrize("case", ["no_decision", "reject", "other_incident", "not_approved", "no_incident",
                                  "no_text", "no_reviewer", "other_decision"])
def test_no_approval_blocks(queued, case):
    """HIL-06: no path sends a work order without an approval record."""
    row, d = queued
    state = "approved"
    if case == "no_decision":
        d = None
    elif case == "reject":
        d = dict(d, action="reject")
    elif case == "other_incident":
        d = dict(d, incident_id="INC-test_asset-20200801T0000Z")
    elif case == "not_approved":
        state = "awaiting_review"
    elif case == "no_incident":
        state = None
    elif case == "no_text":
        d = dict(d, work_order=None)
    elif case == "no_reviewer":
        d = dict(d, reviewer=None)
    else:
        d = dict(d, decision_id="D-0000000000ff")
    problems = workorders.send_problems(row, d, state)
    assert problems and _reasons(problems)[0] == "no_approval", problems
    assert all(p.endswith(".") and len(p) < 200 for p in problems)


def test_the_no_approval_sentences(queued):
    row, d = queued
    assert workorders.send_problems(row, None, "approved") == [
        "no_approval: there is no decision D-000000000001, so nothing approves this work order."]
    assert workorders.send_problems(row, dict(d, action="reject"), "rejected") == [
        "no_approval: decision D-000000000001 is 'reject', not approve or edit.",
        f"no_approval: incident {IID} is rejected, not approved."]


def _tamper(row, case):
    row = dict(row, body=dict(row["body"]))
    body = row["body"]
    if case == "row_hash":
        row["text_sha256"] = "0" * 64
    elif case == "title":
        body["title"] = "Replace the compressor"
    elif case == "tasks_and_hashes":          # the text changed and both hashes recomputed to match it
        body["tasks"] = ["Replace the compressor"]
        new = workorders.text_sha256(dict(work_order(), tasks=body["tasks"]))
        body["text_sha256"] = row["text_sha256"] = new
    elif case == "approval_id":
        body["approval_id"] = "D-0000000000ff"
    elif case == "body_hash":
        body["text_sha256"] = "0" * 64
    elif case == "approved_by":
        body["approved_by"] = "someone_else"
    elif case == "asset_id":
        body["asset_id"] = "other_asset"
    elif case == "extra_field":
        body["override"] = "skip the lock-out"
    elif case == "missing_field":
        del body["brief_id"]
    elif case == "wo_id":
        row["wo_id"] = "WO-0000000000ff"
    elif case == "no_body":
        row["body"] = None
    return row


TAMPER_CASES = ["row_hash", "title", "tasks_and_hashes", "approval_id", "body_hash", "approved_by", "asset_id",
                "extra_field", "missing_field", "wo_id", "no_body"]


@pytest.mark.parametrize("case", TAMPER_CASES)
def test_a_changed_order_is_tampered(queued, case):
    """INT-06: the order text is checked against the approved text by hash; every other
    body field must be what the decision says."""
    row, d = queued
    problems = workorders.send_problems(_tamper(row, case), d, "approved")
    assert problems and set(_reasons(problems)) == {"tampered"}, problems


def test_the_tampered_sentences(queued):
    row, d = queued
    assert workorders.send_problems(_tamper(row, "title"), d, "approved") == [
        "tampered: the body changes the approved text (title)."]
    assert workorders.send_problems(_tamper(row, "tasks_and_hashes"), d, "approved") == [
        "tampered: the row's text_sha256 is not the hash of the approved text.",
        "tampered: the body changes the approved text (tasks).",
        "tampered: the body's text_sha256 is not the hash of the approved text."]
    assert workorders.send_problems(_tamper(row, "extra_field"), d, "approved") == [
        "tampered: the body changes what decision D-000000000001 approved (override)."]
    assert workorders.send_problems(_tamper(row, "approved_by"), d, "approved") == [
        "tampered: the body changes what decision D-000000000001 approved (approved_by)."]


def test_no_approval_comes_before_tampered(queued):
    row, d = queued
    problems = workorders.send_problems(_tamper(row, "title"), dict(d, action="reject"), "approved")
    assert _reasons(problems) == ["no_approval", "tampered"]
    assert workorders.send_problems(_tamper(row, "title"), dict(d, reviewer=None), "approved") == [
        "no_approval: decision D-000000000001 has no reviewer."]


def test_problem_reason_reads_the_first_word():
    assert workorders.problem_reason("tampered: the body's title differs.") == "tampered"
    assert workorders.problem_reason("no_approval: there is no decision D-1.") == "no_approval"
    assert set(workorders.REASONS) == {"no_approval", "tampered"}
