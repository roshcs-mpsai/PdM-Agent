"""The four triage rule sets of the Oct 2-4 A/B test, kept as evidence only (T36).

Each rule reads the check results of one alarm and says whether it *would*
dismiss it:

* A1, motor-off rule: dismiss when load was commanded with the motor off (C1);
* A2, data-quality rule: dismiss on any failed data-quality check (C1-C3);
* A3, rule with a veto: A2, unless a fault-signature check (C4-C8) also fails;
* A4, refill-rate rule: A3, except that after a restart (C2) the refill rate
  (C11) replaces the continuous-load veto (C4). Designed after reading the
  LLM's answers on the same incidents, so its figures are in-sample.

Decision of Oct 3, 2026: no rule removes an alarm. Every incident goes to the
agent and to a reviewer; these verdicts travel with it as evidence (the
incident's ``triage`` field and the run_check tool), and the policy that
would act on them stays ``none``. notebooks/detector_selection_v7.2.ipynb
Sections 8.3-8.7 hold the A/B test.
"""
from __future__ import annotations

POLICY = "none"            # the frozen pipeline dismisses nothing
DATA_QUALITY = ("C1", "C2", "C3")
FAULT_SIGNATURE = ("C4", "C5", "C6", "C7", "C8")
RULES = {
    "A1": "dismiss when load was commanded with the motor off (C1 fails)",
    "A2": "dismiss on any failed data-quality check (C1-C3)",
    "A3": "dismiss on a failed data-quality check unless a fault-signature check (C4-C8) also fails",
    "A4": "A3, except after a restart (C2) a slow refill (C11) vetoes and continuous load (C4) does not",
}


def _failing(results: dict, ids) -> list[str]:
    return [i for i in ids if results.get(i) == "fail"]


def verdicts(results: dict) -> dict:
    """{rule: (would dismiss, the checks that decided it)} from {check id: result}."""
    dq = _failing(results, DATA_QUALITY)
    vetoes = _failing(results, FAULT_SIGNATURE)
    restart = results.get("C2") == "fail"
    a4_vetoes = [c for c in vetoes if not (restart and c == "C4")] + _failing(results, ["C11"])
    return {
        "A1": (results.get("C1") == "fail", _failing(results, ["C1"])),
        "A2": (bool(dq), dq),
        "A3": (bool(dq) and not vetoes, dq + vetoes),
        "A4": (bool(dq) and not a4_vetoes, dq + a4_vetoes),
    }


def triage_evidence(check_results) -> dict:
    """The rules' verdicts on one alarm, for the incident record and the reviewer."""
    results = {r.id: r.result for r in check_results} if check_results and hasattr(check_results[0], "id") \
        else dict(check_results)
    out = {"policy": POLICY, "acted_on": False, "rules": {}}
    for rule, (dismiss, decided_by) in verdicts(results).items():
        out["rules"][rule] = {"would_dismiss": bool(dismiss), "decided_by": decided_by, "rule": RULES[rule]}
    return out
