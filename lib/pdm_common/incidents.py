"""The incident lifecycle (T12b; INC-05, INC-06, HIL-05).

An incident moves through fixed states, and every move is a row in the
insert-only incident_events table with who made it, when and why:

    open -> diagnosing -> awaiting_review -> approved -> sent -> closed
                                           -> rejected -> closed

Only the gate opens an incident; only the agent moves it to diagnosing and to
awaiting_review (insufficient and budget-exhausted briefs too, so the planner
always sees the alarm); only a reviewer approves or rejects; only the
connector marks an approved order sent. A sent incident closes when its alarm
episode is over: the gate closes it when the episode ends, or the connector at
once if the episode ended before the order went out. Anything else is refused
(``IllegalTransition``). The agent may also hand a claimed incident back,
diagnosing -> open, when the language model cannot be reached (``release``):
the incident stays on the dashboard as a raw alert and the next try claims it
again (FRD section 5, model outage). The state is changed with a compare-and-set inside
the same transaction as its event row, so two agents triggered by the same
incident cannot both claim it (INC-06).

The gate's episode -- the span within which new alerts attach to the
incident -- is separate: it closes after a quiet hold-off
(``episode_closed_at``) without changing the lifecycle state.
"""
from __future__ import annotations

import datetime as dt

STATES = ("open", "diagnosing", "awaiting_review", "approved", "rejected", "sent", "closed")
FINAL = ("closed",)
TRANSITIONS = {
    ("open", "diagnosing"): ("agent",),
    ("diagnosing", "awaiting_review"): ("agent",),
    ("diagnosing", "open"): ("agent",),                 # released: the language model was unavailable
    ("awaiting_review", "approved"): ("reviewer",),
    ("awaiting_review", "rejected"): ("reviewer",),
    ("approved", "sent"): ("connector",),
    ("rejected", "closed"): ("system", "reviewer"),
    ("sent", "closed"): ("system", "connector"),
}


class IllegalTransition(ValueError):
    pass


def role(actor: str) -> str:
    """'reviewer:ashok' -> 'reviewer'."""
    return actor.split(":", 1)[0]


def check(from_state: str | None, to_state: str, actor: str) -> None:
    if to_state not in STATES:
        raise IllegalTransition(f"unknown state {to_state!r}")
    allowed = TRANSITIONS.get((from_state, to_state))
    if allowed is None:
        raise IllegalTransition(f"{from_state} -> {to_state} is not a transition of the incident lifecycle")
    if role(actor) not in allowed:
        raise IllegalTransition(f"{actor} may not move an incident {from_state} -> {to_state} "
                                f"(only {', '.join(allowed)})")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def open_incident(store, row: dict, *, actor: str = "gate", cause: str, at=None) -> bool:
    """Insert a new incident in state open with its first event. False if the id exists
    (the same replay opened it before)."""
    at = at or row.get("opened_at") or _now()
    with store.transaction():
        if store.select("incidents", {"incident_id": row["incident_id"]}, columns="incident_id"):
            return False
        store.insert("incidents", dict(row, state="open"))
        store.insert("incident_events", {"incident_id": row["incident_id"], "from_state": None,
                                         "to_state": "open", "actor": actor, "at": at, "cause": cause})
    return True


def transition(store, incident_id: str, to_state: str, actor: str, cause: str, at=None,
               expect: str | None = None) -> str:
    """Move an incident; returns the state it left. ``expect``: refuse unless the
    incident is in this state now (the compare in compare-and-set)."""
    with store.transaction():
        row = store.one("SELECT state FROM incidents WHERE incident_id = ?", [incident_id])
        if row is None:
            raise IllegalTransition(f"no incident {incident_id}")
        current = row["state"]
        if expect is not None and current != expect:
            raise IllegalTransition(f"{incident_id} is {current}, not {expect}")
        check(current, to_state, actor)
        changed = store.update("incidents", {"incident_id": incident_id, "state": current}, {"state": to_state})
        if changed == 0:
            raise IllegalTransition(f"{incident_id} changed state while moving to {to_state}")
        store.insert("incident_events", {"incident_id": incident_id, "from_state": current, "to_state": to_state,
                                         "actor": actor, "at": at or _now(), "cause": cause})
    return current


def claim(store, incident_id: str, agent: str = "agent", at=None) -> bool:
    """The agent's claim (INC-06): open -> diagnosing, once. False when another run has it."""
    try:
        transition(store, incident_id, "diagnosing", agent, "agent run started", at=at, expect="open")
        return True
    except IllegalTransition:
        return False


def release(store, incident_id: str, cause: str, agent: str = "agent", at=None) -> bool:
    """The agent hands a claimed incident back (diagnosing -> open) so a later try can claim it.
    False when the incident is not in diagnosing."""
    try:
        transition(store, incident_id, "open", agent, cause, at=at, expect="diagnosing")
        return True
    except IllegalTransition:
        return False


def history(store, incident_id: str) -> list[dict]:
    return store.select("incident_events", {"incident_id": incident_id}, order="id")


def replay_state(events: list[dict]) -> str | None:
    """The state the event log implies; the audit rebuild checks it equals the stored one."""
    state = None
    for e in events:
        if e["from_state"] != state:
            raise IllegalTransition(f"event log broken at {e}: expected from {state}")
        state = e["to_state"]
    return state
