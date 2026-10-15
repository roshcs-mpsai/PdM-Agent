"""Work orders: the approved text, its hash and the queue (T22; INT-04 to INT-06, HIL-06).

The approval API and the connector share this module. The API queues a work
order in the same transaction as the decision that approves it; from then on
only the connector (services/connector) changes the row.

* **Approved text.** The title, asset, priority, tasks and safety notes of the
  decision's work order (``APPROVED_FIELDS``); the draft's ``status`` is not
  part of it. ``text_sha256`` is the SHA-256 of its canonical JSON (sorted
  keys, no whitespace), so the order of the keys does not change it.
* **Queue.** ``queue`` writes one work_orders row per approve or edit
  decision, with the POST body fixed at that moment (``payload``). The
  connector sends that stored body unchanged, so every retry is the same
  request under the same idempotency key, the decision id.
* **Before every send** the connector calls ``send_problems``. HIL-06: the
  decision exists, approves (approve or edit), is for the row's incident, and
  the incident is approved (or already sent, for a resend after a lost
  response). INT-06: the row's text hash is the hash of the approved text, the
  body still carries that text, and every other body field is what the
  decision says. Each problem is one sentence whose first word is its reason,
  ``no_approval`` or ``tampered`` (``problem_reason``).

Statuses of a work_orders row: ``queued`` (waiting for its first send),
``failed`` (a send failed; it will be retried), ``sent``, ``blocked`` (refused
or given up; a person has to look at it).
"""
from __future__ import annotations

import hashlib
import re

from pdm_common.hashing import canonical_json
from pdm_common.timeutil import iso_utc, to_utc

APPROVED_FIELDS = ("title", "asset", "priority", "tasks", "safety_notes")
BODY_TEXT_FIELDS = ("title", "priority", "tasks", "safety_notes")      # the approved text as the body carries it

QUEUED, FAILED, SENT, BLOCKED = "queued", "failed", "sent", "blocked"
STATUSES = (QUEUED, FAILED, SENT, BLOCKED)
DUE_STATUSES = (QUEUED, FAILED)
APPROVING = ("approve", "edit")
SENDABLE_STATES = ("approved", "sent")          # "sent": a resend after a lost response
REASONS = ("no_approval", "tampered")

_DECISION_ID = re.compile(r"^D-([0-9a-fA-F]{12})$")


def approved_text(work_order: dict) -> dict:
    """The fields a reviewer approves, missing ones as None; never the draft's status."""
    work_order = work_order or {}
    return {name: work_order.get(name) for name in APPROVED_FIELDS}


def text_sha256(work_order: dict) -> str:
    """SHA-256 (hex) of the canonical JSON of the approved text."""
    return hashlib.sha256(canonical_json(approved_text(work_order)).encode("utf-8")).hexdigest()


def wo_id_for(decision_id: str) -> str:
    """'D-0123456789ab' -> 'WO-0123456789ab': one work order per decision."""
    match = _DECISION_ID.match(decision_id) if isinstance(decision_id, str) else None
    if match is None:
        raise ValueError(f"not a decision id: {decision_id!r} (expected D- and 12 hex digits)")
    return f"WO-{match.group(1)}"


def payload(decision: dict, asset_id: str) -> dict:
    """The POST body for an approving decision: the endpoint's required fields plus brief_id.

    The text comes from the decision's work order as the reviewer approved it
    (edited or not); ``asset_id`` is the incident's asset. ValueError when the
    decision lacks what a work order needs."""
    work_order = decision.get("work_order")
    missing = [k for k in ("decision_id", "incident_id", "reviewer", "at") if not decision.get(k)]
    if not isinstance(work_order, dict):
        missing.append("work_order")
    if missing:
        raise ValueError(f"decision {decision.get('decision_id')} has no {', '.join(missing)}")
    if not isinstance(asset_id, str) or not asset_id:
        raise ValueError(f"work order for decision {decision['decision_id']} has no asset id")
    return {
        "approval_id": decision["decision_id"],
        "wo_id": wo_id_for(decision["decision_id"]),
        "incident_id": decision["incident_id"],
        "asset_id": asset_id,
        "title": work_order.get("title"),
        "priority": work_order.get("priority"),
        "tasks": work_order.get("tasks"),
        "safety_notes": work_order.get("safety_notes"),
        "text_sha256": text_sha256(work_order),
        "approved_by": decision["reviewer"],
        "approved_at": iso_utc(to_utc(decision["at"])),
        "brief_id": decision.get("brief_id"),
    }


def queue(store, decision: dict, asset_id: str, at=None) -> dict:
    """Insert the work_orders row for an approve or edit decision; returns the row as stored.

    Call it inside the transaction that inserts the decision. It opens no
    transaction of its own (Store.transaction would join the caller's anyway),
    so the decision and its work order commit together or not at all. The row
    is due at ``at``, by default the decision's time. Any action other than
    approve or edit raises ValueError and writes nothing."""
    action = decision.get("action")
    if action not in APPROVING:
        raise ValueError(f"decision {decision.get('decision_id')} is {action!r}: only an approve or edit "
                         f"decision queues a work order")
    body = payload(decision, asset_id)
    when = to_utc(at if at is not None else decision["at"])
    store.insert("work_orders", {
        "wo_id": body["wo_id"], "decision_id": body["approval_id"], "incident_id": body["incident_id"],
        "asset_id": asset_id, "body": body, "text_sha256": body["text_sha256"], "status": QUEUED,
        "attempts": 0, "next_attempt_at": when, "last_error": None, "reference": None, "sent_at": None,
        "updated_at": when})
    return store.select("work_orders", {"wo_id": body["wo_id"]})[0]


def send_problems(row: dict, decision: dict | None, incident_state: str | None) -> list[str]:
    """Why ``row`` must not be sent; [] when it may be.

    ``decision`` is the decisions row the work order names (None when there is
    none) and ``incident_state`` the incident's current state (None when there
    is no incident). HIL-06 problems come first, then INT-06 ones."""
    decision_id, incident_id = row.get("decision_id"), row.get("incident_id")
    if decision is None:
        return [f"no_approval: there is no decision {decision_id}, so nothing approves this work order."]
    if decision.get("decision_id") != decision_id:
        return [f"no_approval: decision {decision.get('decision_id')} is not {decision_id}, "
                f"the decision this work order names."]
    found = []
    if decision.get("action") not in APPROVING:
        found.append(f"no_approval: decision {decision_id} is {decision.get('action')!r}, not approve or edit.")
    other_incident = decision.get("incident_id") != incident_id
    if other_incident:
        found.append(f"no_approval: decision {decision_id} is for incident {decision.get('incident_id')}, "
                     f"not {incident_id}.")
    if incident_state is None:
        found.append(f"no_approval: there is no incident {incident_id}.")
    elif incident_state not in SENDABLE_STATES:
        found.append(f"no_approval: incident {incident_id} is {incident_state}, not approved.")
    work_order = decision.get("work_order")
    if not isinstance(work_order, dict):
        found.append(f"no_approval: decision {decision_id} carries no work order text.")
    if other_incident or not isinstance(work_order, dict):
        return found                 # nothing on this row can be checked against that decision
    try:
        expected = payload(decision, row.get("asset_id"))
    except (TypeError, ValueError) as exc:
        return found + [f"no_approval: {exc}."]

    approved = expected["text_sha256"]
    body = row.get("body") if isinstance(row.get("body"), dict) else {}
    if row.get("text_sha256") != approved:
        found.append("tampered: the row's text_sha256 is not the hash of the approved text.")
    as_sent = dict({name: body.get(name) for name in BODY_TEXT_FIELDS}, asset=work_order.get("asset"))
    if text_sha256(as_sent) != approved:
        changed = [n for n in BODY_TEXT_FIELDS if canonical_json(body.get(n)) != canonical_json(work_order.get(n))]
        found.append("tampered: the body changes the approved text" + (f" ({', '.join(changed)})." if changed else "."))
    if body.get("approval_id") != decision_id:
        found.append(f"tampered: the body's approval_id is {body.get('approval_id')!r}, not {decision_id}.")
    if body.get("text_sha256") != approved:
        found.append("tampered: the body's text_sha256 is not the hash of the approved text.")
    if row.get("wo_id") != expected["wo_id"]:
        found.append(f"tampered: work order id {row.get('wo_id')} does not belong to decision {decision_id}.")
    checked = set(BODY_TEXT_FIELDS) | {"approval_id", "text_sha256"}
    other = sorted(k for k in set(body) | set(expected) if k not in checked and (
        (k in body) != (k in expected) or canonical_json(body.get(k)) != canonical_json(expected.get(k))))
    if other:
        found.append(f"tampered: the body changes what decision {decision_id} approved ({', '.join(other)}).")
    return found


def problem_reason(problem: str) -> str:
    """'tampered: the body ...' -> 'tampered': the reason label of a send problem."""
    return problem.split(":", 1)[0].strip()


__all__ = [
    "APPROVED_FIELDS", "APPROVING", "BLOCKED", "DUE_STATUSES", "FAILED", "QUEUED", "REASONS", "SENDABLE_STATES",
    "SENT", "STATUSES", "approved_text", "payload", "problem_reason", "queue", "send_problems", "text_sha256",
    "wo_id_for",
]
