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

The agent's run (claim, release and the trace) is recorded by the agent; this module
is the part the API shares with it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re

from pdm_common import incidents as lifecycle
from pdm_common.brief import validate_brief

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
