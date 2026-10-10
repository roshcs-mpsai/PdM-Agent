"""What each dashboard page shows, computed from the API's JSON (T18, T34; HIL-01 to HIL-04, HIL-07
to HIL-09, NFR-09).

Pure functions, so the content of every page is tested without Streamlit:

* the asset list (HIL-01): NE 107 status and marker colour, P(24/72/168 h) each with its interval
  (``fmt_p``: "0.12 (0.05-0.31)", never a probability without its interval), the survival model's
  skill note, open incidents and last update, in the API's order (highest P(72 h) first);
* the asset detail (HIL-02): its header and the table views of its charts (charts.py draws them);
* the review screen (HIL-03, HIL-04): the incident queue; a brief with every citation resolved to
  its stored tool result, the prognosis read from the cited context result, the recommended action,
  the checks run, a banner for insufficient and budget_exhausted briefs; the draft work order as form
  fields, the edited order and its diff against the draft, the decision request body; work-order
  status as the connector left it (INT-04);
* the audit (HIL-08): the decision rebuilt, whether the evidence still hashes to the recorded value
  and whether the incident's events replay to its stored state;
* the study (HIL-09): the consent text, an item under its blind label, the answer body, and the
  evaluators' table of results.

Text from the API that goes into Markdown (asset ids, causes, briefs, reasons) passes through ``md``
first. A brief is written by a language model and can quote retrieved text, so a link or an image in
it must show as text and never render or load.
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import re
from typing import Optional

from pdm_common.status import marker
from pdm_common.timeutil import iso_utc, to_utc
from pdm_common.workorders import approved_text

HORIZONS = ("24", "72", "168")
SKILL_NOTE = "the survival model does not beat a constant hazard; read the probabilities as context"
MARKER_HEX = {"green": "#0ca30c", "amber": "#fab219", "red": "#d03b3b", "grey": "#898781"}
BANNERS = {
    "insufficient": "Insufficient evidence: the agent could not support a diagnosis. It recommends a data-quality "
                    "check or monitoring, not an inspection; look at the data before any work on the equipment.",
    "budget_exhausted": "Partial brief: the agent's budget ran out before it finished the diagnosis. Treat the "
                        "hypotheses as incomplete and check the evidence yourself.",
}
ACTION_TYPES = {"inspection": "Inspection", "sensor_check": "Sensor check", "data_quality_check": "Data-quality check",
                "monitor": "Monitor"}
PRIORITIES = ("low", "medium", "high", "urgent")
WORK_ORDER_FIELDS = ("title", "asset", "priority", "tasks", "safety_notes")
REASON_MAX = 2000
WORK_ORDER_STATUS = {"queued": "queued, waiting for its first send", "sent": "sent",
                     "failed": "failed, the connector will retry", "blocked": "blocked, a person has to look at it"}
QUEUES = {"awaiting review": {"state": "awaiting_review"}, "open, no brief yet": {"state": "open"},
          "not closed": {"open_only": True}, "all": {}}
SPANS = {"1 day": 1, "7 days": 7, "30 days": 30}
RATINGS = {1: "1, not useful", 2: "2", 3: "3", 4: "4", 5: "5, very useful"}
STUDY_DECISIONS = {"act": "Act on it", "dismiss": "Dismiss it", "unsure": "Unsure"}
ARM_NAMES = {"raw": "raw alert", "retrieval_only": "retrieval-only brief", "full": "agent brief"}
SIGN_IN_TEXT = ("Sign in with your own API token in the sidebar. The token stays in this browser session and "
                "is sent only to the API; your name comes back from the API and is recorded on every "
                "decision you make.")
CONSENT_TEXT = (
    "This study compares ways of presenting an alarm to a reviewer. You will see a series of alarms, one at a "
    "time, each under a letter: A, B or C. For each one, rate how useful the information is for deciding what "
    "to do, from 1 (not useful) to 5 (very useful), and say whether you would act on it, dismiss it, or are "
    "unsure.\n\n"
    "What is recorded: that you agreed to take part, and when; for each item, when it was first shown, your "
    "rating, your choice, and the seconds from showing to answer. Nothing you answer changes an incident, a "
    "brief or a work order.\n\n"
    "Your answers are stored under a pseudonym (P- and eight characters) made from your sign-in name and a "
    "secret the study keeps. Your name is not stored with them, and results are reported in aggregate only.\n\n"
    "Taking part is voluntary. You can stop at any time and carry on later, or not at all. Answers you have "
    "already given are kept.")
THANK_YOU = "Thank you. You have answered every item of this session; there is nothing more to do."

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$:])")


class FormError(ValueError):
    """A form that cannot be sent as it is; the message says what to fix."""


# --- formatting ------------------------------------------------------------------------------------

def md(text) -> str:
    """``text`` with every Markdown character escaped, so it shows as written."""
    return "" if text is None else _MD_SPECIAL.sub(r"\\\1", str(text))


def fmt_p(horizon: Optional[dict], digits: int = 2) -> str:
    """'0.12 (0.05-0.31)': a probability with its interval; without both bounds, no probability (NFR-09)."""
    if not horizon or horizon.get("p") is None:
        return "n/a"
    lower, upper = horizon.get("lower"), horizon.get("upper")
    if lower is None or upper is None:
        return "n/a (no interval)"
    return f"{horizon['p']:.{digits}f} ({lower:.{digits}f}-{upper:.{digits}f})"


def fmt_time(value) -> str:
    """'2020-06-01T00:24:00Z' -> '2020-06-01 00:24 UTC'."""
    if not value:
        return "n/a"
    try:
        return to_utc(value).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return str(value)


def fmt_num(value, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def ne107_marker(state: Optional[str]) -> dict:
    """{state, label, colour, hex}: the NE 107 state as text and its marker (grey before any status)."""
    if not state:
        return {"state": None, "label": "no status yet", "colour": "grey", "hex": MARKER_HEX["grey"]}
    colour = marker(state)
    return {"state": state, "label": state.replace("_", " "), "colour": colour, "hex": MARKER_HEX[colour]}


def dot_html(hex_colour: str) -> str:
    """The marker dot (the state is always written next to it)."""
    return f"<span style='color:{hex_colour};font-size:1.6rem;line-height:1'>&#9679;</span>"


def skill_note(prognosis: Optional[dict]) -> Optional[str]:
    """The note shown with every probability when the survival model misses its skill bar."""
    skill = (prognosis or {}).get("skill") or {}
    return SKILL_NOTE if skill.get("meets_bar") is False else None


# --- the asset list and detail (HIL-01, HIL-02) ------------------------------------------------------

def asset_rows(assets: list, incidents: list = ()) -> list[dict]:
    """One row per asset, in the API's order; ``incidents``: GET /incidents?open=1, newest first."""
    open_ids: dict = {}
    for inc in incidents or ():
        if inc.get("state") != "closed":
            open_ids.setdefault(inc.get("asset_id"), []).append(inc.get("incident_id"))
    rows = []
    for a in assets or ():
        prognosis = a.get("prognosis") or {}
        horizons = prognosis.get("horizons") or {}
        m = ne107_marker(a.get("ne107"))
        rows.append({"asset_id": a.get("asset_id"), "ne107": m["state"], "status": m["label"], "colour": m["colour"],
                     "hex": m["hex"], "cause": a.get("ne107_cause"),
                     **{f"p{h}": fmt_p(horizons.get(h)) for h in HORIZONS},
                     "prognosis_at": fmt_time(prognosis.get("ts")) if prognosis else "n/a",
                     "skill_note": skill_note(prognosis),
                     "open_incidents": int(a.get("open_incidents") or 0),
                     "incidents": open_ids.get(a.get("asset_id"), []),
                     "last_update": fmt_time(a.get("last_update"))})
    return rows


def status_line(row: dict) -> str:
    """'**asset** · maintenance required (cause)' in Markdown."""
    cause = f" ({md(row['cause'])})" if row.get("cause") else ""
    return f"**{md(row['asset_id'])}** · {md(row['status'])}{cause}"


def probability_line(row: dict) -> str:
    """'P(24 h) 0.29 (0.00-0.81) · P(72 h) ... · P(168 h) ...', 90 % intervals in brackets."""
    return " · ".join(f"P({h} h) {row[f'p{h}']}" for h in HORIZONS) + " (90 % intervals)"


def activity_line(row: dict) -> str:
    n = row["open_incidents"]
    return f"{n} open incident{'' if n == 1 else 's'} · last update {row['last_update']}"


def asset_header(asset: dict) -> dict:
    """The asset detail's header: the list row plus the latest score against the threshold."""
    row = asset_rows([asset])[0]
    row["score"] = (f"latest score {fmt_num(asset.get('score'))} against the threshold "
                    f"{fmt_num(asset.get('threshold'))}" if asset.get("score") is not None else "no score yet")
    row["status_at"] = fmt_time(asset.get("status_at"))
    row["detector_run_id"] = asset.get("detector_run_id")
    alert = asset.get("latest_alert") or {}
    row["latest_alert"] = fmt_time(alert.get("window_end")) if alert else None
    return row


def span_bounds(last_update: Optional[str], days: int) -> tuple[Optional[str], Optional[str]]:
    """(from, to) of the last ``days`` days up to the asset's last update (stream time)."""
    if not last_update:
        return None, None
    end = to_utc(last_update)
    return iso_utc(end - dt.timedelta(days=days)), iso_utc(end)


def attribution_rows(latest_alert: Optional[dict]) -> list[dict]:
    return [{"feature": a.get("feature"), "share of the score": f"{100 * (a.get('share') or 0):.1f} %",
             "squared error": fmt_num(a.get("squared_error"))} for a in (latest_alert or {}).get("attribution") or []]


def score_rows(series: dict) -> list[dict]:
    """The score chart as a table, newest first."""
    s = (series or {}).get("scores") or {}
    rows = [{"window end": fmt_time(t), "score": fmt_num(sc), "threshold": fmt_num(th), "alert": "yes" if a else "no"}
            for t, sc, th, a in zip(s.get("window_end") or [], s.get("score") or [], s.get("threshold") or [],
                                    s.get("alert") or [])]
    return rows[::-1]


def prognosis_rows(series: dict) -> list[dict]:
    """The probability chart as a table, newest first, each probability with its interval."""
    p = (series or {}).get("prognoses") or {}

    def value(h: str, k: str, i: int):
        values = (p.get(h) or {}).get(k) or []
        return values[i] if i < len(values) else None
    rows = []
    for i, t in enumerate(p.get("ts") or []):
        rows.append({"time": fmt_time(t), **{f"P({h} h)": fmt_p({k: value(h, k, i) for k in ("p", "lower", "upper")})
                                             for h in HORIZONS}})
    return rows[::-1]


# --- the review screen (HIL-03, HIL-04) ----------------------------------------------------------

def incident_rows(incidents: list) -> list[dict]:
    """The queue as a table."""
    out = []
    for inc in incidents or ():
        brief = inc.get("brief") or {}
        decision = inc.get("decision") or {}
        wo = inc.get("work_order") or {}
        summary = brief.get("summary") or ""
        out.append({"incident": inc.get("incident_id"), "asset": inc.get("asset_id"),
                    "opened": fmt_time(inc.get("opened_at")), "state": (inc.get("state") or "").replace("_", " "),
                    "evidence": (brief.get("evidence_state") or "no brief yet").replace("_", " "),
                    "summary": summary if len(summary) <= 120 else summary[:117] + "...",
                    "decision": f"{decision['action']} by {decision['reviewer']}" if decision else "",
                    "work order": wo.get("status") or ""})
    return out


def incident_header(detail: dict) -> dict:
    inc = detail.get("incident") or {}
    events = detail.get("events") or []
    opened = next((e for e in events if e.get("to_state") == "open" and e.get("from_state") is None), None)
    return {"incident_id": inc.get("incident_id"), "asset_id": inc.get("asset_id"),
            "opened": fmt_time(inc.get("opened_at")), "state": inc.get("state"),
            "state_label": (inc.get("state") or "").replace("_", " "),
            "cause": opened.get("cause") if opened else None}


def banner_text(evidence_state: Optional[str]) -> Optional[str]:
    """The banner of an insufficient or budget_exhausted brief; None for a sufficient one."""
    return BANNERS.get(evidence_state) if evidence_state else None


def citation(cite_id: str, results: dict) -> dict:
    """A citation and the stored tool result it points at (None when it is not in ``results``)."""
    result = (results or {}).get(cite_id)
    tool = result.get("tool") if result else None
    return {"id": cite_id, "tool": tool, "result": result,
            "title": f"{cite_id} · {tool}" if result else f"{cite_id} · not loaded"}


def context_prognosis(result: Optional[dict]) -> dict:
    """The prognosis as the cited incident_context result holds it, each probability with its interval."""
    prognosis = ((result or {}).get("output") or {}).get("prognosis") or {}
    if not prognosis.get("available"):
        return {"available": False, "text": prognosis.get("reason") or "no prognosis is cited",
                "cells": {}, "made": None, "note": None}
    horizons = prognosis.get("horizons_h") or {}
    minutes = prognosis.get("made_minutes_before_opening")
    made = (f"made {minutes:g} min before the opening" if isinstance(minutes, (int, float)) else
            f"made {fmt_time(prognosis.get('made_at'))}" if prognosis.get("made_at") else None)
    note = SKILL_NOTE if prognosis.get("meets_skill_bar") is False else None
    return {"available": True, "text": None, "cells": {h: fmt_p(horizons.get(h)) for h in HORIZONS},
            "made": made, "note": note}


def check_rows(cite_ids: list, results: dict) -> list[dict]:
    """The checks a brief lists as run, one row per check, from the run_check results it cites."""
    rows = []
    for rid in cite_ids or ():
        result = (results or {}).get(rid)
        if result is None:
            rows.append({"cited as": rid, "check": "(result not loaded)", "kind": "", "result": "", "measured": ""})
            continue
        for c in (result.get("output") or {}).get("results") or []:
            rows.append({"cited as": rid, "check": f"{c.get('id')} {c.get('check')}", "kind": c.get("kind"),
                         "result": c.get("result"), "measured": c.get("measured")})
    return rows


def brief_view(document: dict, results: dict, banner: Optional[str] = None) -> dict:
    """A brief as the review screen shows it. ``results``: {cited id: stored result}; ``banner``: the
    evidence state to flag (default: the brief's own, when it is not sufficient)."""
    doc = document or {}
    state = doc.get("evidence_state")

    def cites(ids):
        return [citation(i, results) for i in ids or ()]

    def claims(items):
        return [{"text": c.get("text"), "citations": cites(c.get("cites"))} for c in items or ()]

    prognosis_ids = (doc.get("prognosis") or {}).get("cites") or []
    context = next((results[i] for i in prognosis_ids
                    if (results or {}).get(i) and results[i].get("tool") == "incident_context"), None)
    action = doc.get("recommended_action") or {}
    return {"brief_id": doc.get("brief_id"), "evidence_state": state,
            "banner": banner_text(banner if banner is not None else (state if state != "sufficient" else None)),
            "summary": doc.get("summary") or "",
            "hypotheses": [{"id": h.get("id"),
                            "title": " · ".join(str(x).replace("_", " ") for x in
                                                (h.get("id"), h.get("category"), h.get("likelihood")) if x),
                            "statement": h.get("statement"), "claims": claims(h.get("claims")),
                            "against": claims(h.get("against"))} for h in doc.get("hypotheses") or ()],
            "prognosis": dict(context_prognosis(context), citations=cites(prognosis_ids)),
            "action": {"type": ACTION_TYPES.get(action.get("type"), action.get("type")), "text": action.get("text"),
                       "citations": cites(action.get("cites"))},
            "checks": check_rows(doc.get("checks_run"), results),
            "work_order": doc.get("work_order") or {}}


def incident_brief(detail: dict) -> Optional[dict]:
    """The brief of GET /incidents/{id} with its cited results; None before the agent's brief."""
    row = detail.get("brief")
    if not row:
        return None
    return brief_view(row.get("document") or {}, detail.get("cited_results") or {}, detail.get("banner"))


def work_order_lines(work_order: dict) -> list[str]:
    """A work order as Markdown lines (read-only display)."""
    wo = work_order or {}
    lines = [f"**{md(wo.get('title'))}** · priority {md(wo.get('priority'))}"]
    lines += [f"- task: {md(t)}" for t in wo.get("tasks") or ()]
    lines += [f"- safety: {md(s)}" for s in wo.get("safety_notes") or ()]
    return lines


def work_order_fields(work_order: dict) -> dict:
    """The draft as the editor's initial values: tasks and safety notes one per line."""
    wo = work_order or {}
    priority = wo.get("priority") if wo.get("priority") in PRIORITIES else PRIORITIES[0]
    return {"title": wo.get("title") or "", "priority": priority, "tasks": "\n".join(wo.get("tasks") or ()),
            "safety_notes": "\n".join(wo.get("safety_notes") or ())}


def _lines(text) -> list[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def work_order_from_form(draft: dict, title: str, priority: str, tasks: str, safety_notes: str) -> dict:
    """The work order as edited in place; the asset stays the draft's."""
    wo = {"title": (title or "").strip()}
    if (draft or {}).get("asset"):
        wo["asset"] = draft["asset"]
    wo.update(priority=priority, tasks=_lines(tasks), safety_notes=_lines(safety_notes))
    return wo


def _normal(work_order: dict) -> dict:
    text = approved_text(work_order)
    text["title"] = (text["title"] or "").strip()
    for key in ("tasks", "safety_notes"):
        text[key] = [str(t).strip() for t in text[key] or () if str(t).strip()]
    return text


def work_order_changed(draft: dict, edited: Optional[dict]) -> bool:
    """Whether the reviewer changed the approved text (title, asset, priority, tasks, safety notes)."""
    return edited is not None and _normal(draft or {}) != _normal(edited)


def work_order_problems(work_order: dict) -> list[str]:
    """What brief_v1's work-order schema would refuse, said plainly."""
    wo = work_order or {}
    problems = []
    if not (wo.get("title") or "").strip():
        problems.append("the title is empty")
    if wo.get("priority") not in PRIORITIES:
        problems.append(f"the priority is one of {', '.join(PRIORITIES)}")
    if not wo.get("tasks"):
        problems.append("list at least one task")
    if not wo.get("safety_notes"):
        problems.append("keep at least one safety note")
    return problems


def work_order_diff(draft: dict, edited: dict) -> str:
    """The unified diff the API will store with an edit (decisions.py writes it the same way)."""
    a = json.dumps(approved_text(draft), indent=1, sort_keys=True).splitlines()
    b = json.dumps(approved_text(edited), indent=1, sort_keys=True).splitlines()
    return "\n".join(difflib.unified_diff(a, b, "draft", "approved", lineterm=""))


def decision_body(action: str, reason: str, draft: Optional[dict] = None, edited: Optional[dict] = None) -> dict:
    """The body of POST /briefs/{id}/decisions; FormError when it would be refused."""
    if action not in ("approve", "edit", "reject"):
        raise FormError("Choose approve, edit or reject.")
    reason = (reason or "").strip()
    if not reason:
        raise FormError("Give a reason: every decision needs one.")
    if len(reason) > REASON_MAX:
        raise FormError(f"The reason is longer than {REASON_MAX} characters.")
    if action != "edit":
        return {"action": action, "reason": reason}
    if not work_order_changed(draft, edited):
        raise FormError("The work order is unchanged; approve the draft instead.")
    problems = work_order_problems(edited)
    if problems:
        raise FormError("Fix the work order: " + "; ".join(problems) + ".")
    return {"action": "edit", "reason": reason,
            "work_order": {k: edited[k] for k in WORK_ORDER_FIELDS if k in edited}}


def decision_rows(decisions: list) -> list[dict]:
    return [{"decision": d.get("decision_id"), "action": d.get("action"), "reviewer": d.get("reviewer"),
             "at": fmt_time(d.get("at")), "reason": d.get("reason")} for d in decisions or ()]


def decision_done(out: dict) -> str:
    """What the reviewer is told after a decision is recorded."""
    d = out.get("decision") or {}
    wo = out.get("work_order")
    text = f"Recorded {d.get('decision_id')}: {d.get('action')} by {d.get('reviewer')}."
    if wo:
        text += f" Work order {wo.get('wo_id')} is {WORK_ORDER_STATUS.get(wo.get('status'), wo.get('status'))}."
    else:
        text += " No work order: the incident is closed."
    return text


def work_order_rows(work_orders: list) -> list[dict]:
    """INT-04: every work order with its status, attempts and last error."""
    return [{"work order": w.get("wo_id"), "incident": w.get("incident_id"), "asset": w.get("asset_id"),
             "status": WORK_ORDER_STATUS.get(w.get("status"), w.get("status")), "attempts": w.get("attempts") or 0,
             "last error": w.get("last_error") or "", "reference": w.get("reference") or "",
             "updated": fmt_time(w.get("updated_at"))} for w in work_orders or ()]


def work_order_note(row: dict) -> tuple[str, str]:
    """(kind, text) for one work order: info, success, warning (failed) or error (blocked)."""
    wo_id, status = row.get("wo_id"), row.get("status")
    attempts = row.get("attempts") or 0
    if status == "sent":
        ref = f", reference {row['reference']}" if row.get("reference") else ""
        return "success", f"Work order {wo_id} was sent{ref}."
    if status == "failed":
        error = row.get("last_error") or "no error text"
        return "warning", f"Work order {wo_id} failed on attempt {attempts}: {error}. The connector will try again."
    if status == "blocked":
        return "error", (f"Work order {wo_id} is blocked: {row.get('last_error') or 'no error text'}. "
                         "Nothing more will be sent; a person has to look at it.")
    return "info", f"Work order {wo_id} is queued, waiting for its first send."


# --- the audit (HIL-08) --------------------------------------------------------------------------

def event_rows(events: list) -> list[dict]:
    return [{"at": fmt_time(e.get("at")), "from": e.get("from_state") or "", "to": e.get("to_state"),
             "by": e.get("actor"), "cause": e.get("cause")} for e in events or ()]


def audit_view(audit: dict) -> dict:
    """GET /decisions/{id} as the audit page shows it."""
    d = audit.get("decision") or {}
    inc = audit.get("incident") or {}
    trace = audit.get("trace") or {}
    evidence_ok, events_ok = bool(audit.get("evidence_matches")), bool(inc.get("consistent"))
    events = inc.get("events") or []
    return {
        "decision": {"decision_id": d.get("decision_id"), "action": d.get("action"), "reviewer": d.get("reviewer"),
                     "at": fmt_time(d.get("at")), "reason": d.get("reason"), "brief_id": d.get("brief_id"),
                     "incident_id": d.get("incident_id"), "trace_id": d.get("trace_id"),
                     "versions": d.get("versions") or {}},
        "work_order": d.get("work_order"), "diff": d.get("diff") or "",
        "evidence_ok": evidence_ok,
        "evidence_text": ("The evidence the reviewer saw (the brief, every result it cites and the incident) still "
                          "hashes to the recorded SHA-256." if evidence_ok else
                          f"The evidence no longer matches its record: recorded {d.get('evidence_sha256')}, "
                          f"rebuilt now {audit.get('evidence_sha256_now')}."),
        "events_ok": events_ok,
        "events_text": (f"The incident's {len(events)} events replay to its stored state, {inc.get('state')}."
                        if events_ok else f"The events replay to {inc.get('state_from_events')}, but the stored "
                                          f"state is {inc.get('state')}."),
        "events": event_rows(events),
        "trace": {"trace_id": trace.get("trace_id"), "outcome": trace.get("outcome"), "model": trace.get("model"),
                  "started": fmt_time(trace.get("started_at")), "finished": fmt_time(trace.get("finished_at")),
                  "calls": [{"n": c.get("n"), "tool": c.get("tool"), "result": c.get("result_id") or "",
                             "refused": c.get("refused") or "", "by": c.get("by")}
                            for c in trace.get("tool_calls") or ()]} if trace else None,
        "brief": brief_view(audit.get("brief") or {}, (audit.get("evidence") or {}).get("results") or {})
        if audit.get("brief") else None,
        "work_order_row": audit.get("work_order")}


# --- the study (HIL-09) ---------------------------------------------------------------------------

def alert_view(alert: dict) -> dict:
    """A study item's raw alert: the cause, the gate, the checks at the opening, the trajectory."""
    gate = (alert or {}).get("gate") or {}
    fraction = gate.get("min_fraction")
    gate_text = None
    if gate.get("alerting") is not None:
        gate_text = f"{gate['alerting']} of {gate.get('present')} windows over the threshold"
        if isinstance(fraction, (int, float)):
            gate_text += f"; the gate opens at {100 * fraction:.0f} %"
    return {"cause": (alert or {}).get("cause") or "the cause was not recorded", "gate": gate_text,
            "checks": [{"check": f"{c.get('id')} {c.get('check')}", "kind": c.get("kind"), "result": c.get("result"),
                        "measured": c.get("measured")} for c in (alert or {}).get("checks") or ()],
            "trajectory": (alert or {}).get("trajectory") or []}


def study_item_view(item: dict) -> dict:
    """GET /study/sessions/{id}/next as the participant sees it: a blind label, never an arm."""
    if item.get("done"):
        return {"done": True, "progress": f"{item.get('answered')} of {item.get('items')} answered"}
    inc = item.get("incident") or {}
    view = {"done": False, "order_no": item.get("order_no"),
            "progress": f"Item {item.get('order_no')} of {item.get('items')}",
            "label": f"Condition {item.get('label')}", "kind": item.get("kind"),
            "header": f"{inc.get('asset_id')} · alarm opened {fmt_time(inc.get('opened_at'))}"}
    if item.get("kind") == "alert":
        view["alert"] = alert_view(item.get("alert") or {})
    else:
        view["brief"] = brief_view(item.get("brief") or {}, item.get("evidence") or {}, item.get("banner"))
    return view


def study_answer_body(order_no: int, rating, decision) -> dict:
    """The body of POST /study/sessions/{id}/answers; FormError when something is missing."""
    if not isinstance(rating, int) or isinstance(rating, bool) or not 1 <= rating <= 5:
        raise FormError("Choose a rating from 1 to 5.")
    if decision not in STUDY_DECISIONS:
        raise FormError("Choose what you would do.")
    return {"order_no": int(order_no), "rating": rating, "decision": decision}


def unfinished_sessions(sessions: list) -> list[dict]:
    """The participant's sessions with items left, to carry on."""
    return [{"session_id": s["session_id"],
             "label": f"Started {fmt_time(s.get('started_at'))}: {s.get('answered')} of {s.get('items')} answered"}
            for s in sessions or () if not s.get("done")]


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def study_results_line(results: dict) -> str:
    r = results or {}
    return (f"{_count(r.get('sessions', 0), 'session')}, {_count(r.get('participants', 0), 'participant')}, "
            f"{_count(r.get('answers', 0), 'answer')}; by true arm, in aggregate.")


def study_results_rows(results: dict) -> list[dict]:
    """The evaluators' table: one row per true arm, raw alert first."""
    arms = (results or {}).get("arms") or {}
    rows = []
    for arm in [a for a in ARM_NAMES if a in arms] + sorted(a for a in arms if a not in ARM_NAMES):
        r = arms[arm]
        mean, median = r.get("mean_rating"), r.get("median_seconds")
        rows.append({"arm": ARM_NAMES.get(arm, arm), "answers": r.get("n", 0),
                     "mean rating": "n/a" if mean is None else f"{mean:.2f}",
                     "median seconds": "n/a" if median is None else f"{median:.1f}",
                     **{d: (r.get("decisions") or {}).get(d, 0) for d in STUDY_DECISIONS},
                     "dealt": r.get("dealt", 0), "shown": r.get("shown", 0)})
    return rows
