"""Reviewer decisions and their audit (T20; HIL-03, HIL-05, HIL-06, HIL-08, PR-15).

``decide`` records one decision on the current brief of an incident awaiting review:

* approve: the draft work order as the agent wrote it;
* edit: the reviewer's version of the work order, with a unified diff against the draft;
* reject: no work order; the incident moves rejected -> closed.

Every decision needs a reason. The decision row is insert-only and carries the brief, the
reviewer, the action, the approved text and diff, the SHA-256 of the evidence snapshot the
reviewer saw (the brief and every result it cites), the versions and the agent's trace id. In
the same transaction the incident moves (awaiting_review -> approved or rejected) and, for an
approve or edit, the work order is queued (pdm_common.workorders.queue). There is no other way
to create a work order (HIL-06).

``audit`` rebuilds a past decision from the store (HIL-08): the decision, the brief, the
evidence snapshot rebuilt now and whether it still hashes to what was approved, the trace, the
incident's history and whether it replays to the stored state, and the work order.
"""
from __future__ import annotations

import copy
import datetime as dt
import difflib
import hashlib
import json
import uuid
from typing import Optional

from pdm_common import incidents as lifecycle
from pdm_common import workorders
from pdm_common.brief import cited
from pdm_common.hashing import canonical_json
from pdm_common.schemas import load_schema

import queries

ACTIONS = ("approve", "edit", "reject")
API_VERSION = "pdm-api 1.0"
REASON_MAX = 2000


class DecisionError(ValueError):
    """A decision that cannot be recorded; ``status`` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def evidence_snapshot(store, brief_row: dict) -> dict:
    """What the reviewer decided on: the brief, every result it cites, and the incident."""
    doc = brief_row["document"]
    inc = store.one("SELECT incident_id, asset_id, opened_at, as_of, gate FROM incidents WHERE incident_id = ?",
                    [brief_row["incident_id"]], table="incidents")
    res = queries.results(store, cited(doc))
    return {"brief": doc, "results": {rid: {k: r[k] for k in ("tool", "arguments", "as_of", "output")}
                                      for rid, r in sorted(res.items())},
            "incident": inc}


def snapshot_sha256(snapshot: dict) -> str:
    return hashlib.sha256(canonical_json(snapshot).encode()).hexdigest()


def _work_order_problems(work_order) -> list[str]:
    """The edited work order against brief_v1's work-order definition."""
    from jsonschema import Draft202012Validator
    schema = load_schema("brief_v1")
    wo_schema = dict(schema["$defs"]["work_order"], **{"$defs": schema["$defs"]})
    errors = Draft202012Validator(wo_schema).iter_errors(work_order)
    return [f"/work_order/{'/'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in errors]


def _diff(draft: dict, edited: dict) -> str:
    a = json.dumps(workorders.approved_text(draft), indent=1, sort_keys=True).splitlines()
    b = json.dumps(workorders.approved_text(edited), indent=1, sort_keys=True).splitlines()
    return "\n".join(difflib.unified_diff(a, b, "draft", "approved", lineterm=""))


def decide(store, brief_id: str, reviewer, action: str, reason: str, work_order: Optional[dict] = None,
           at=None) -> dict:
    """Record a decision; returns {decision, work_order}. DecisionError when it cannot be made."""
    if action not in ACTIONS:
        raise DecisionError(f"action is one of {', '.join(ACTIONS)}", 422)
    reason = (reason or "").strip()
    if not reason:
        raise DecisionError("every decision needs a reason", 422)
    if len(reason) > REASON_MAX:
        raise DecisionError(f"the reason is longer than {REASON_MAX} characters", 422)
    at = at or dt.datetime.now(dt.timezone.utc)
    brief = store.one("SELECT * FROM briefs WHERE brief_id = ?", [brief_id], table="briefs")
    if brief is None:
        raise DecisionError(f"no brief {brief_id}", 404)
    if not brief["valid"]:
        raise DecisionError(f"{brief_id} failed validation; decide on the incident's current brief")
    if brief["arm"] != queries.LIVE_ARM:
        raise DecisionError(f"{brief_id} is a study brief; decisions are on the live brief")
    current = queries.current_brief(store, brief["incident_id"])
    if current is None or current["brief_id"] != brief_id:
        raise DecisionError(f"{brief_id} is not the current brief of {brief['incident_id']}")
    draft = brief["document"]["work_order"]
    if action == "edit":
        if not isinstance(work_order, dict):
            raise DecisionError("an edit carries the edited work order", 422)
        work_order = dict(copy.deepcopy(work_order), status="draft")
        work_order.setdefault("asset", draft.get("asset"))
        problems = _work_order_problems(work_order)
        if problems:
            raise DecisionError("; ".join(problems), 422)
        if workorders.text_sha256(work_order) == workorders.text_sha256(draft):
            raise DecisionError("the edited work order is the draft; approve it instead", 422)
        approved, diff = work_order, _diff(draft, work_order)
    elif action == "approve":
        if work_order is not None:
            raise DecisionError("an approve takes the draft as it is; send an edit to change it", 422)
        approved, diff = copy.deepcopy(draft), ""
    else:
        approved, diff = None, ""
    if approved is not None:
        approved["status"] = "approved"
    snapshot = evidence_snapshot(store, brief)
    decision = {"decision_id": f"D-{uuid.uuid4().hex[:12]}", "brief_id": brief_id,
                "incident_id": brief["incident_id"], "reviewer": reviewer.name, "action": action, "reason": reason,
                "work_order": approved, "diff": diff, "evidence_sha256": snapshot_sha256(snapshot),
                "versions": dict(brief["document"].get("versions") or {}, api=API_VERSION),
                "trace_id": brief["trace_id"], "at": at}
    queued = None
    with store.transaction():
        state = store.one("SELECT state, asset_id FROM incidents WHERE incident_id = ?", [brief["incident_id"]])
        if state is None or state["state"] != "awaiting_review":
            raise DecisionError(f"{brief['incident_id']} is {state['state'] if state else 'missing'}, "
                                f"not awaiting review")
        store.insert("decisions", decision)
        to = "rejected" if action == "reject" else "approved"
        lifecycle.transition(store, brief["incident_id"], to, reviewer.actor,
                             f"decision {decision['decision_id']} ({action}): {reason}"[:500], at=at,
                             expect="awaiting_review")
        if action == "reject":
            lifecycle.transition(store, brief["incident_id"], "closed", "system",
                                 f"rejected by {reviewer.name}", at=at, expect="rejected")
        else:
            queued = workorders.queue(store, store.select("decisions", {"decision_id": decision["decision_id"]})[0],
                                      state["asset_id"], at)
    return {"decision": store.select("decisions", {"decision_id": decision["decision_id"]})[0], "work_order": queued}


def audit(store, decision_id: str) -> Optional[dict]:
    """HIL-08: a past decision rebuilt from the store, with integrity checks."""
    decision = store.one("SELECT * FROM decisions WHERE decision_id = ?", [decision_id], table="decisions")
    if decision is None:
        return None
    brief = store.one("SELECT * FROM briefs WHERE brief_id = ?", [decision["brief_id"]], table="briefs")
    snapshot = evidence_snapshot(store, brief) if brief else None
    now_sha = snapshot_sha256(snapshot) if snapshot else None
    events = lifecycle.history(store, decision["incident_id"])
    stored = store.one("SELECT state FROM incidents WHERE incident_id = ?", [decision["incident_id"]])
    try:
        replayed = lifecycle.replay_state(events)
    except lifecycle.IllegalTransition as exc:
        replayed = f"broken: {exc}"
    return {"decision": decision, "brief": brief["document"] if brief else None,
            "evidence": snapshot, "evidence_sha256_now": now_sha,
            "evidence_matches": now_sha == decision["evidence_sha256"],
            "trace": queries.trace_summary(store, decision["trace_id"]),
            "incident": {"incident_id": decision["incident_id"], "state": stored["state"] if stored else None,
                         "state_from_events": replayed, "events": events,
                         "consistent": bool(stored) and replayed == stored["state"]},
            "work_order": (store.select("work_orders", {"decision_id": decision_id}) or [None])[0]}
