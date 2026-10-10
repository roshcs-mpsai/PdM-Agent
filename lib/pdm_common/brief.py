"""The brief validator (T17; BRF-01 to BRF-07).

A brief is valid only if every claim stands on a stored tool result of the
same agent run. ``validate_brief`` checks, in order:

1. the brief_v1 schema (BRF-01, BRF-04, BRF-07: categories, likelihoods,
   action types and a draft work order with tasks and safety notes);
2. every citation -- in claims, counter-claims, the prognosis, the
   recommended action and checks_run -- names a result produced in this run
   for this incident (BRF-02);
3. the prognosis cites a result holding the stored prognosis, and the brief
   carries no probability of its own (BRF-05);
4. every number written in a claim appears in one of the results that claim
   cites, within the rounding it was written with (BRF-06);
5. an insufficient brief lists the checks it ran and recommends a data-quality
   check or monitoring, never an inspection (BRF-03). In the study's
   retrieval-only arm the agent has no run_check, so ``require_checks=False``
   drops the list requirement and nothing else.

``fallback_brief`` builds the insufficient-evidence brief the agent falls back
to after one failed repair: it cites only results that exist and makes no claim.
"""
from __future__ import annotations

import math
import re
from typing import Iterable

from pdm_common.schemas import validate

NUMBER = re.compile(r"(?<![A-Za-z0-9_.\-/])-?\d+(?:\.\d+)?(?![A-Za-z0-9_])")
PROGNOSIS_TOOLS = ("incident_context",)


def _citations(brief: dict) -> list[tuple[str, list[str]]]:
    """(where, cited ids) for every place the brief cites."""
    out = []
    for h in brief.get("hypotheses") or []:
        for kind in ("claims", "against"):
            for i, c in enumerate(h.get(kind) or []):
                out.append((f"{h.get('id')}.{kind}[{i}]", list(c.get("cites") or [])))
    out.append(("prognosis", list((brief.get("prognosis") or {}).get("cites") or [])))
    out.append(("recommended_action", list((brief.get("recommended_action") or {}).get("cites") or [])))
    out.append(("checks_run", list(brief.get("checks_run") or [])))
    return out


def cited(brief: dict) -> list[str]:
    """Every result id the brief cites, once each, in order of first appearance."""
    out: list[str] = []
    for _, ids in _citations(brief):
        for rid in ids:
            if rid not in out:
                out.append(rid)
    return out


def _numbers_in(value) -> list[float]:
    """Every number in a result's output: numeric fields and numbers written inside strings."""
    found = []
    if isinstance(value, bool) or value is None:
        return found
    if isinstance(value, (int, float)):
        if math.isfinite(value):
            found.append(float(value))
    elif isinstance(value, str):
        found += [float(m) for m in NUMBER.findall(value)]
    elif isinstance(value, dict):
        for v in value.values():
            found += _numbers_in(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            found += _numbers_in(v)
    return found


def _decimals(text: str) -> int:
    return len(text.split(".", 1)[1]) if "." in text else 0


def number_supported(text: str, candidates: Iterable[float]) -> bool:
    """Does a number as written (e.g. '0.19', '92') match a candidate within its rounding?
    A percentage may stand for a share ('92' for 0.92)."""
    written = float(text)
    tol = 0.5 * 10 ** (-_decimals(text)) + 1e-9
    for c in candidates:
        for v in (c, 100 * c):
            if abs(v - written) <= tol or abs(round(v, _decimals(text)) - written) <= 1e-9:
                return True
    return False


def validate_brief(brief: dict, results: dict, *, incident_id: str | None = None,
                   require_checks: bool = True) -> list[str]:
    """Problems with ``brief``; [] when valid. ``results``: result_id -> stored tool
    result (tool_result_v1) of this run. ``require_checks``: an insufficient brief must
    list run_check results (False only for a run without run_check, the retrieval-only arm)."""
    problems = validate("brief_v1", brief)
    if problems:
        return problems
    if incident_id is not None and brief["incident_id"] != incident_id:
        problems.append(f"/incident_id: brief is for {brief['incident_id']}, not {incident_id}")
    for where, cites in _citations(brief):
        for rid in cites:
            r = results.get(rid)
            if r is None:
                problems.append(f"{where}: cites {rid}, which this run did not produce")
            elif r.get("incident_id") != brief["incident_id"]:
                problems.append(f"{where}: cites {rid}, a result of another incident")
    # BRF-05: the prognosis comes from the stored prognosis, never from the model
    for rid in brief["prognosis"]["cites"]:
        r = results.get(rid)
        if r is not None and (r.get("tool") not in PROGNOSIS_TOOLS or not (r.get("output") or {}).get("prognosis")):
            problems.append(f"prognosis: {rid} is not a stored prognosis")
    # BRF-06: numbers in claims must come from the results they cite
    for h in brief["hypotheses"]:
        for kind in ("claims", "against"):
            for i, c in enumerate(h.get(kind) or []):
                pool = [x for rid in c["cites"] if rid in results for x in _numbers_in(results[rid].get("output"))]
                for n in NUMBER.findall(c["text"]):
                    if not number_supported(n, pool):
                        problems.append(f"{h['id']}.{kind}[{i}]: {n} appears in none of {', '.join(c['cites'])}")
    # BRF-03: an insufficient brief recommends a data-quality check or monitoring and lists its checks
    state = brief["evidence_state"]
    if state == "insufficient":
        if brief["recommended_action"]["type"] not in ("data_quality_check", "monitor"):
            problems.append("recommended_action: an insufficient brief recommends a data-quality check or monitoring")
        if require_checks and not brief["checks_run"]:
            problems.append("checks_run: an insufficient brief lists the checks it ran")
    if state == "sufficient" and not brief["hypotheses"]:
        problems.append("hypotheses: a sufficient brief states at least one hypothesis")
    checks = [rid for rid in brief["checks_run"] if rid in results and results[rid].get("tool") != "run_check"]
    for rid in checks:
        problems.append(f"checks_run: {rid} is not a run_check result")
    return problems


def fallback_brief(*, brief_id: str, incident_id: str, asset_id: str, results: dict, versions: dict,
                   state: str = "insufficient", reason: str = "") -> dict:
    """The brief of last resort: no claims, the checks that ran, a data-quality check (BRF-02, BRF-03)."""
    checks = [rid for rid, r in results.items() if r.get("tool") == "run_check"]
    context = [rid for rid, r in results.items() if r.get("tool") in PROGNOSIS_TOOLS]
    cites = checks or context
    summary = ("The evidence gathered did not support a diagnosis" if state == "insufficient"
               else "Partial brief: the agent ran out of budget before it could finish a diagnosis")
    if reason:
        summary += f" ({reason})"
    return {
        "schema": "brief_v1", "brief_id": brief_id, "incident_id": incident_id, "asset_id": asset_id,
        "evidence_state": state, "summary": summary + ". A reviewer should look at the data before any inspection.",
        "hypotheses": [],
        "prognosis": {"cites": context[:1]},
        "recommended_action": {"type": "data_quality_check",
                               "text": "Check the recorded data around the alarm (gaps, restarts, interpolated points, "
                                       "sensor agreement) and keep the asset under observation.",
                               "cites": cites[:3]},
        "work_order": {"title": f"Review data quality for {incident_id}", "asset": asset_id, "priority": "low",
                       "tasks": ["Review the alarm's data and the checks that ran",
                                 "Decide whether an inspection is needed"],
                       "safety_notes": ["No work on the equipment is requested by this order."],
                       "status": "draft"},
        "checks_run": checks,
        "versions": versions,
    }
