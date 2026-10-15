"""Accepting an agent's brief into the record (T16, T20; BRF-01 to BRF-07, INC-05).

A brief enters the store only through ``accept_brief``, whether the agent runs in
process (services/agent, its direct sink) or posts it to the API
(``POST /incidents/{id}/briefs``). It:

1. loads the results the run stored (tool_results ``R-<run>-<n>``, where ``<run>`` is
   the first eight hex digits of the SHA-256 of the run's trace id);
2. validates the brief against them (pdm_common.brief.validate_brief: schema,
   citations to this run only, the cited prognosis, numbers, evidence states);
3. stores it in ``briefs`` either way, with ``valid`` and the problems, so a rejected
   draft stays in the audit record next to its repair;
4. for a valid brief, moves the incident diagnosing -> awaiting_review in the same
   transaction. A brief for an incident that is not in diagnosing is refused.

A study run (``move=False``: the retrieval-only arm on an incident the live agent
already diagnosed, T34) is validated and stored the same way and moves nothing.

The rest of the agent's records -- its claim on the incident (open -> diagnosing, once:
INC-06), the hand-back when the model is unreachable, the checkpoint after each step --
are written by the functions below, which the agent's direct sink and the API share.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import time

from pdm_common import incidents as lifecycle
from pdm_common.brief import validate_brief
from pdm_common.timeutil import to_utc

RESULT_ID = re.compile(r"^R-([0-9a-f]{8})-([0-9]+)$")
ARMS = ("full", "retrieval_only")


def run_token(trace_id: str) -> str:
    """The run part of a result id: the first 8 hex digits of SHA-256(trace_id)."""
    return hashlib.sha256(trace_id.encode()).hexdigest()[:8]


def run_results(store, incident_id: str, trace_id: str) -> dict:
    """{result_id: tool_result_v1} of one agent run, in the order they were stored."""
    token = run_token(trace_id)
    rows = store.query("SELECT * FROM tool_results WHERE incident_id = ? AND result_id LIKE ?",
                       [incident_id, f"R-{token}-%"], table="tool_results")
    rows.sort(key=lambda r: int(r["result_id"].rsplit("-", 1)[1]))
    return {r["result_id"]: dict(r, schema="tool_result_v1") for r in rows}


def accept_brief(store, brief: dict, *, trace_id: str, arm: str, at=None, move: bool = True) -> list[str]:
    """Validate ``brief`` against its run's stored results and store it. Returns the problems
    ([] when it was accepted and, with ``move``, the incident moved to awaiting_review)."""
    if arm not in ARMS:
        return [f"arm: {arm!r} is not one of {', '.join(ARMS)}"]
    incident_id = brief.get("incident_id") if isinstance(brief, dict) else None
    if not incident_id:
        return ["/incident_id: missing"]
    if not isinstance(brief.get("brief_id"), str) or not brief["brief_id"].startswith("B-"):
        return ["/brief_id: missing, or not a brief id (B-...); nothing was stored"]
    if not trace_id:
        return ["trace_id: missing; a brief belongs to an agent run"]
    results = run_results(store, incident_id, trace_id)
    problems = validate_brief(brief, results, incident_id=incident_id, require_checks=arm != "retrieval_only")
    at = at or dt.datetime.now(dt.timezone.utc)
    brief_id = brief.get("brief_id")
    with store.transaction():
        row = store.one("SELECT state FROM incidents WHERE incident_id = ?", [incident_id])
        if row is None:
            return [f"/incident_id: no incident {incident_id}"]
        if move and not problems and row["state"] != "diagnosing":
            problems = [f"/incident_id: {incident_id} is {row['state']}, not diagnosing; the brief was not accepted"]
        elif move and not problems and holder(store, incident_id) != trace_id:
            problems = [f"/incident_id: {incident_id} is held by another agent run; the brief was not accepted"]
        if store.select("briefs", {"brief_id": brief_id}, columns="brief_id"):
            return [f"/brief_id: {brief_id} is already stored"]
        store.insert("briefs", {"brief_id": brief_id, "incident_id": incident_id, "document": brief,
                                "evidence_state": brief.get("evidence_state"), "valid": not problems,
                                "validation": problems, "trace_id": trace_id, "arm": arm, "created_at": at})
        if move and not problems:
            lifecycle.transition(store, incident_id, "awaiting_review", "agent",
                                 f"brief {brief_id} ({brief['evidence_state']})", at=at, expect="diagnosing")
    return problems


def latest_brief(store, incident_id: str, *, valid_only: bool = True) -> dict | None:
    """The newest (valid) brief row of an incident."""
    sql = "SELECT * FROM briefs WHERE incident_id = ?"
    params: list = [incident_id]
    if valid_only:
        sql += " AND valid = ?"
        params.append(True if store.dialect == "postgres" else 1)
    return store.one(sql + " ORDER BY created_at DESC LIMIT 1", params, table="briefs")


# --- the agent's run records ---------------------------------------------------------------------
#
# A run holds an incident from its claim (the event "agent run <trace> started", open ->
# diagnosing) until the incident leaves diagnosing. Only the holder may hand it back or move it
# on, so a run that lost a claim race, or was given up for stale, cannot reopen or diagnose an
# incident another run now holds.

CLAIM_CAUSE = "agent run {trace_id} started"


def _busy(exc: Exception) -> bool:
    """SQLite's answer when another connection is writing: worth a retry, not a failure."""
    return type(exc).__name__ == "OperationalError" and "locked" in str(exc).lower()


def claim(store, incident_id: str, trace_id: str, tries: int = 6) -> bool:
    """open -> diagnosing for this run; False when the incident is not open (another run has it)."""
    if not trace_id:
        raise ValueError("a claim needs the run's trace id: without it no run holds the incident")
    for attempt in range(tries):
        try:
            lifecycle.transition(store, incident_id, "diagnosing", "agent", CLAIM_CAUSE.format(trace_id=trace_id),
                                 expect="open")
            return True
        except lifecycle.IllegalTransition:
            return False
        except Exception as exc:
            if not _busy(exc) or attempt == tries - 1:
                raise
            time.sleep(0.05 * 2 ** attempt)
    return False


def holder(store, incident_id: str) -> str | None:
    """The trace id of the run holding the incident, or None when no run holds it."""
    last = store.one("SELECT to_state, cause FROM incident_events WHERE incident_id = ? ORDER BY id DESC LIMIT 1",
                     [incident_id])
    if not last or last["to_state"] != "diagnosing":
        return None
    m = re.fullmatch(r"agent run (.+) started", last["cause"] or "")
    return m.group(1) if m else None


def release(store, incident_id: str, trace_id: str, cause: str) -> bool:
    """diagnosing -> open: the run could not finish (the model was unreachable). Only the run
    holding the incident may hand it back; False otherwise."""
    with store.transaction():
        if holder(store, incident_id) != trace_id:
            return False
        return lifecycle.release(store, incident_id, f"agent run {trace_id}: {cause}")


def save_checkpoint(store, state: dict) -> None:
    """The run's state after a step, replacing the previous checkpoint of the same run."""
    row = {"incident_id": state["incident_id"], "arm": state["arm"], "node": state["node"], "state": state,
           "updated_at": dt.datetime.now(dt.timezone.utc)}
    if store.update("agent_checkpoints", {"trace_id": state["trace_id"]}, row) == 0:
        store.insert("agent_checkpoints", dict(row, trace_id=state["trace_id"]))


def unfinished(store, asset_id: str | None = None) -> list[dict]:
    """Checkpoints of runs that stopped before writing their trace (the agent died mid-run) and still
    hold their incident: the runs to resume."""
    rows = store.query("SELECT c.* FROM agent_checkpoints c WHERE c.node <> ? AND NOT EXISTS "
                       "(SELECT 1 FROM agent_traces t WHERE t.trace_id = c.trace_id) ORDER BY c.updated_at",
                       ["end"], table="agent_checkpoints")
    if asset_id is not None:
        ids = {r["incident_id"] for r in store.query("SELECT incident_id FROM incidents WHERE asset_id = ?",
                                                     [asset_id])}
        rows = [r for r in rows if r["incident_id"] in ids]
    return [r for r in rows if holder(store, r["incident_id"]) == r["trace_id"]]


def stale_runs(store, asset_id: str, older_than_s: float, now=None) -> list[dict]:
    """Incidents still in diagnosing more than ``older_than_s`` after a run claimed them: the run
    stopped or hung, or ended without moving the incident (its brief and its hand-back both failed
    to be written). ``state`` is the checkpoint to resume from, when the run left one past its
    first step and wrote no trace; otherwise None, and the incident is to be handed back."""
    now = to_utc(now) if now is not None else dt.datetime.now(dt.timezone.utc)
    out = []
    for inc in store.query("SELECT incident_id FROM incidents WHERE asset_id = ? AND state = ?",
                           [asset_id, "diagnosing"]):
        trace_id = holder(store, inc["incident_id"])
        if trace_id is None:
            continue
        claimed = store.one("SELECT at FROM incident_events WHERE incident_id = ? ORDER BY id DESC LIMIT 1",
                            [inc["incident_id"]], table="incident_events")
        if (now - to_utc(claimed["at"])).total_seconds() < older_than_s:
            continue
        finished = store.one("SELECT 1 AS done FROM agent_traces WHERE trace_id = ?", [trace_id]) is not None
        cp = None if finished else store.one("SELECT state FROM agent_checkpoints WHERE trace_id = ? AND node NOT "
                                             "IN (?, ?)", [trace_id, "start", "end"], table="agent_checkpoints")
        out.append({"incident_id": inc["incident_id"], "trace_id": trace_id, "claimed_at": claimed["at"],
                    "finished": finished, "state": cp["state"] if cp else None})
    return out


def outage_tries(store, incident_id: str) -> list[dict]:
    """The runs on an incident that ended without a brief because the model (or the agent) failed."""
    return store.query("SELECT trace_id, outcome, finished_at FROM agent_traces WHERE incident_id = ? AND "
                       "outcome IN (?, ?) ORDER BY finished_at", [incident_id, "model_unavailable", "error"],
                       table="agent_traces")
