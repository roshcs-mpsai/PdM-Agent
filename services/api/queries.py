"""What the API reads (T20; HIL-01 to HIL-04): plain functions over the store, so the routes
in main.py stay thin and the tests can call them directly.

Every probability goes out with its interval (NFR-09). The live review screens see the full
arm's briefs only; the study arm's briefs are served by the study endpoints (study.py), so the
reviewer is not shown both.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from pdm_common.brief import cited
from pdm_common.timeutil import iso_utc, to_utc

HORIZONS = ("24", "72", "168")
LIVE_ARM = "full"


def _true(store):
    return True if store.dialect == "postgres" else 1


def asset_ids(store) -> list[str]:
    rows = store.query("SELECT asset_id FROM scores UNION SELECT asset_id FROM incidents "
                       "UNION SELECT asset_id FROM prognoses")
    return sorted({r["asset_id"] for r in rows})


def _prognosis(row: Optional[dict], skill: Optional[dict]) -> Optional[dict]:
    if row is None:
        return None
    horizons = row["probabilities"] or {}
    return {"ts": row["ts"], "model_run_id": row["model_run_id"],
            "horizons": {h: horizons[h] for h in HORIZONS if h in horizons}, "interval": 0.9,
            "attribution": row.get("attribution"), "skill": skill}


def asset_summary(store, asset_id: str, skill: Optional[dict] = None) -> dict:
    status = store.latest("status_log", "at", asset_id)
    prognosis = store.latest("prognoses", "ts", asset_id)
    score = store.latest("scores", "window_end", asset_id)
    states = store.query("SELECT state, COUNT(*) AS n FROM incidents WHERE asset_id = ? GROUP BY state",
                         [asset_id])
    counts = {r["state"]: int(r["n"]) for r in states}
    return {"asset_id": asset_id,
            "ne107": status["to_status"] if status else None, "ne107_cause": status["cause"] if status else None,
            "status_at": status["at"] if status else None,
            "prognosis": _prognosis(prognosis, skill),
            "open_incidents": sum(n for s, n in counts.items() if s != "closed"),
            "incident_states": counts,
            "last_update": score["window_end"] if score else None,
            "score": score["score"] if score else None, "threshold": score["threshold"] if score else None,
            "alert": score["alert"] if score else None,
            "detector_run_id": score["detector_run_id"] if score else None}


def assets(store, skill_for=lambda asset: None) -> list[dict]:
    """HIL-01: every asset, highest P(72 h) first; assets without a prognosis last."""
    rows = [asset_summary(store, a, skill_for(a)) for a in asset_ids(store)]

    def key(r):
        p = ((r["prognosis"] or {}).get("horizons") or {}).get("72") or {}
        return (p.get("p") is None, -(p.get("p") or 0.0), r["asset_id"])
    return sorted(rows, key=key)


def asset_detail(store, asset_id: str, skill: Optional[dict] = None, top: int = 8) -> Optional[dict]:
    """HIL-02: the summary, the latest alert's per-feature attribution and the open incidents."""
    if asset_id not in asset_ids(store):
        return None
    out = asset_summary(store, asset_id, skill)
    alert = store.one("SELECT * FROM scores WHERE asset_id = ? AND alert = ? ORDER BY window_end DESC LIMIT 1",
                      [asset_id, _true(store)], table="scores")
    if alert:
        errors = alert["errors"] or {}
        total = sum(errors.values()) or 1.0
        out["latest_alert"] = {"window_end": alert["window_end"], "score": alert["score"],
                               "threshold": alert["threshold"],
                               "attribution": [{"feature": f, "share": e / total, "squared_error": e}
                                               for f, e in sorted(errors.items(), key=lambda kv: -kv[1])[:top]]}
    else:
        out["latest_alert"] = None
    out["incidents"] = incidents(store, asset_id=asset_id, open_only=True)
    return out


def series(store, asset_id: str, start=None, end=None, limit: int = 20_000) -> dict:
    """HIL-02: scores against the threshold and the prognoses, with bounds, over a span of stream
    time (default: the 7 days up to the latest score)."""
    last = store.latest("scores", "window_end", asset_id)
    end = to_utc(end) if end else (to_utc(last["window_end"]) if last else dt.datetime.now(dt.timezone.utc))
    start = to_utc(start) if start else end - dt.timedelta(days=7)
    scores = store.between("scores", "window_end", asset_id, iso_utc(start), iso_utc(end), limit=limit)
    progs = store.between("prognoses", "ts", asset_id, iso_utc(start), iso_utc(end), limit=limit)
    return {"asset_id": asset_id, "from": iso_utc(start), "to": iso_utc(end),
            "scores": {"window_end": [s["window_end"] for s in scores], "score": [s["score"] for s in scores],
                       "threshold": [s["threshold"] for s in scores], "alert": [s["alert"] for s in scores]},
            "prognoses": {"ts": [p["ts"] for p in progs],
                          **{h: {k: [((p["probabilities"] or {}).get(h) or {}).get(k) for p in progs]
                                 for k in ("p", "lower", "upper")} for h in HORIZONS}}}


def current_brief(store, incident_id: str) -> Optional[dict]:
    """The latest valid brief of the live (full) arm."""
    return store.one("SELECT * FROM briefs WHERE incident_id = ? AND valid = ? AND arm = ? "
                     "ORDER BY created_at DESC LIMIT 1", [incident_id, _true(store), LIVE_ARM], table="briefs")


def incidents(store, *, state: Optional[str] = None, asset_id: Optional[str] = None, open_only: bool = False,
              limit: int = 500) -> list[dict]:
    """The incident queue, newest first, each with its current brief, decision and work order."""
    sql, params = "SELECT * FROM incidents WHERE 1 = 1", []
    if state:
        sql += " AND state = ?"
        params.append(state)
    if asset_id:
        sql += " AND asset_id = ?"
        params.append(asset_id)
    if open_only:
        sql += " AND state <> ?"
        params.append("closed")
    rows = store.query(sql + f" ORDER BY opened_at DESC LIMIT {int(limit)}", params, table="incidents")
    out = []
    for inc in rows:
        brief = current_brief(store, inc["incident_id"])
        decision = store.one("SELECT decision_id, action, reviewer, at FROM decisions WHERE incident_id = ? "
                             "ORDER BY at DESC LIMIT 1", [inc["incident_id"]], table="decisions")
        wo = store.one("SELECT wo_id, status, reference, last_error, attempts FROM work_orders WHERE incident_id = ? "
                       "ORDER BY updated_at DESC LIMIT 1", [inc["incident_id"]], table="work_orders")
        out.append({"incident_id": inc["incident_id"], "asset_id": inc["asset_id"], "opened_at": inc["opened_at"],
                    "state": inc["state"], "gate": inc["gate"],
                    "brief": {"brief_id": brief["brief_id"], "evidence_state": brief["evidence_state"],
                              "summary": brief["document"].get("summary")} if brief else None,
                    "decision": decision, "work_order": wo})
    return out


def results(store, ids: list[str]) -> dict:
    if not ids:
        return {}
    marks = ", ".join("?" * len(ids))
    rows = store.query(f"SELECT * FROM tool_results WHERE result_id IN ({marks})", ids, table="tool_results")
    return {r["result_id"]: {k: r[k] for k in ("result_id", "incident_id", "tool", "arguments", "as_of", "output",
                                               "created_at")} for r in rows}


def trace_summary(store, trace_id: Optional[str]) -> Optional[dict]:
    if not trace_id:
        return None
    t = store.one("SELECT trace_id, incident_id, arm, model, skills, tool_calls, budget, outcome, started_at, "
                  "finished_at FROM agent_traces WHERE trace_id = ?", [trace_id], table="agent_traces")
    if t is None:
        return None
    t["tool_calls"] = [{k: c.get(k) for k in ("n", "tool", "result_id", "refused", "by")} for c in t["tool_calls"] or []]
    return t


def incident_detail(store, incident_id: str) -> Optional[dict]:
    """HIL-03: the incident, its history, its live briefs (the current one with every citation
    expanded to its stored result), the agent's trace, decisions and work order."""
    inc = store.one("SELECT * FROM incidents WHERE incident_id = ?", [incident_id], table="incidents")
    if inc is None:
        return None
    events = store.select("incident_events", {"incident_id": incident_id}, order="id")
    briefs = [b for b in store.select("briefs", {"incident_id": incident_id}, order="created_at")
              if b["arm"] == LIVE_ARM]
    current = current_brief(store, incident_id)
    cited_results = results(store, cited(current["document"])) if current else {}
    return {"incident": inc, "events": events,
            "brief": current, "cited_results": cited_results,
            "drafts": [{k: b[k] for k in ("brief_id", "valid", "validation", "evidence_state", "created_at")}
                       for b in briefs],
            "trace": trace_summary(store, current["trace_id"]) if current else None,
            "decisions": store.select("decisions", {"incident_id": incident_id}, order="at"),
            "work_orders": store.select("work_orders", {"incident_id": incident_id}),
            "banner": None if not current or current["evidence_state"] == "sufficient" else current["evidence_state"]}


def work_orders(store, status: Optional[str] = None) -> list[dict]:
    """INT-04: every work order with its status, attempts and last error, newest first."""
    rows = store.select("work_orders", {"status": status} if status else None, order="updated_at DESC")
    return rows
