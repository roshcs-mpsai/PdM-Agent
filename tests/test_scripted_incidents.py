"""The scripted-incident harness (T28; EVL-08, PR-12, PR-13): the rules policy, the runner and the audit.

The library itself runs on the replayed campaign (eval/run_incidents.py, about 45 minutes to build
the store); these tests run the same code on a synthetic excursion so they stay fast: a small
library with a gate incident, a control opened as the gate would, and the runaway.
"""
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
for sub in ("eval", "services/agent", "services/mcp_server", "tests"):
    sys.path.insert(0, str(ROOT / sub))

import audit  # noqa: E402
import run_incidents  # noqa: E402
from pipeline import Pipeline  # noqa: E402
from policy import RulesModel  # noqa: E402
from pdm_common.brief import validate_brief  # noqa: E402
from pdm_common.store import Store  # noqa: E402
from test_pipeline import synthetic  # noqa: E402

PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"


# --- the policy's brief, case by case --------------------------------------------------------------

def check(cid, kind, result, measured):
    return {"id": cid, "check": f"check_{cid.lower()}", "kind": kind, "result": result, "measured": measured,
            "rule": "r", "points_to": "p", "threshold": None, "unit": None, "value": None}


def seen_with(checks, status="maintenance_required"):
    ctx = {"result_id": "R-aaaaaaaa-1", "tool": "incident_context",
           "output": {"incident": {"incident_id": "INC-x"}, "status": {"ne107": status},
                      "prognosis": {"available": False}}}
    rc = {"result_id": "R-aaaaaaaa-2", "tool": "run_check", "output": {"results": checks}}
    aa = {"result_id": "R-aaaaaaaa-3", "tool": "attribute_anomaly",
          "output": {"top_features": [{"feature": "load_share_6h", "share_of_score": 0.412}]}}
    return {"incident_context": [ctx], "run_check": [rc], "attribute_anomaly": [aa]}


def results_of(seen):
    return {d["result_id"]: dict(d, schema="tool_result_v1", incident_id="INC-x") for docs in seen.values()
            for d in docs}


def finished(brief):
    return dict(brief, schema="brief_v1", brief_id="B-aaaaaaaa-1", incident_id="INC-x", asset_id="a",
                versions={"llm": "rules", "skills": {}, "profile": "p"},
                work_order=dict(brief["work_order"], status="draft", asset="a"))


@pytest.mark.parametrize("checks, state, lead, action", [
    ([check("C9", "instrument", "fail", "panel and reservoir differ by 0.6 bar for 40 min"),
      check("C1", "data_quality", "pass", "0 of 60 points")], "sufficient", "instrumentation", "sensor_check"),
    ([check("C4", "fault_signature", "fail", "no rest for 2.41 h (limit 0.19)"),
      check("C6", "fault_signature", "fail", "oil at 78.4 degC (limit 70.85)"),
      check("C1", "data_quality", "pass", "0 of 60 points")], "sufficient", "process", "inspection"),
    ([check("C2", "data_quality", "fail", "data resumed 31 min before the alarm after a 15 h gap"),
      check("C6", "fault_signature", "pass", "oil at 36.5 degC")], "sufficient", "data_quality", "data_quality_check"),
    ([check("C4", "fault_signature", "fail", "no rest for 2.41 h"),
      check("C1", "data_quality", "pass", "0 of 60 points")], "insufficient", "none", "monitor"),
    ([check("C6", "fault_signature", "pass", "oil at 36.5 degC"),
      check("C3", "data_quality", "pass", "0 interpolated points")], "insufficient", "none", "monitor"),
])
def test_the_policy_follows_the_triage_rules_and_its_briefs_validate(checks, state, lead, action):
    seen = seen_with(checks)
    brief = finished(RulesModel().brief(seen, ["run_check", "attribute_anomaly"]))
    assert brief["evidence_state"] == state and audit.lead_category(brief) == lead
    assert brief["recommended_action"]["type"] == action
    assert validate_brief(brief, results_of(seen), incident_id="INC-x") == []


def test_an_urgent_order_needs_a_failure_status():
    checks = [check("C4", "fault_signature", "fail", "no rest"), check("C6", "fault_signature", "fail", "hot"),
              check("C1", "data_quality", "pass", "ok")]
    assert RulesModel().brief(seen_with(checks, "failure"), [])["work_order"]["priority"] == "urgent"
    assert RulesModel().brief(seen_with(checks), [])["work_order"]["priority"] == "high"


def test_the_policy_calls_the_core_steps_in_order_then_submits():
    m = RulesModel(signals=["SIG_A", "SIG_B"])
    tools = [{"name": n} for n in ("get_telemetry", "attribute_anomaly", "get_maintenance_history",
                                    "search_knowledge", "run_check", "device_health", "submit_brief")]
    seen = seen_with([check("C1", "data_quality", "pass", "ok")])
    first = {"role": "user", "content": [{"type": "text", "text": json.dumps(seen["incident_context"][0])}]}
    messages, order = [first], []
    for _ in range(6):
        turn = m.create("s", tools, messages)
        use = turn.tool_uses[0]
        order.append(use["name"])
        if use["name"] == "submit_brief":
            break
        doc = {"run_check": seen["run_check"][0], "attribute_anomaly": seen["attribute_anomaly"][0]}.get(
            use["name"], {"result_id": f"R-aaaaaaaa-{len(order) + 3}", "tool": use["name"], "output": {}})
        messages += [{"role": "assistant", "content": turn.content},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"],
                                                   "content": json.dumps(doc)}]}]
    assert order == ["run_check", "attribute_anomaly", "get_telemetry", "get_maintenance_history", "submit_brief"]


# --- the runner and the audit on a synthetic store --------------------------------------------------

@pytest.fixture(scope="module")
def base(tmp_path_factory):
    folder = tmp_path_factory.mktemp("incidents")
    store = Store(f"sqlite:///{folder}/base.db")
    store.init_schema()
    pipe = Pipeline(PROFILE_PATH, store, compute_every=60)
    pipe.feed(synthetic(pipe.profile, 200, anomalous_after=60))
    pipe.flush()
    opened = store.select("incidents")[0]["opened_at"]
    library = {"schema": "incidents_v1", "asset": "metropt3_apu", "core_tools": list(run_incidents_core()),
               "optional_tools": ["search_knowledge", "device_health"],
               "incidents": [
                   {"id": "T01", "kind": "gate", "as_of": opened,
                    "expect": {"evidence_state": ["insufficient", "sufficient"], "lead": ["none", "process"]}},
                   {"id": "T02", "kind": "healthy_control", "as_of": "2020-06-01T02:30:00Z", "open": "as_gate",
                    "expect": {"evidence_state": ["insufficient"], "lead": ["none"]}},
                   {"id": "T03", "kind": "runaway", "as_of": "2020-06-01T03:00:00Z", "open": "as_gate",
                    "model": "runaway", "expect": {"outcome": ["budget_exhausted"]}},
                   {"id": "T04", "kind": "gate", "as_of": "2020-06-01T03:10:00Z",
                    "expect": {"evidence_state": ["insufficient"]}}]}
    (folder / "library.yaml").write_text(yaml.safe_dump(library))
    return folder


def run_incidents_core():
    return ("run_check", "attribute_anomaly", "get_telemetry", "get_maintenance_history")


def test_a_run_of_the_library_and_its_audit(base):
    out = base / "run"
    args = run_incidents.parse_args(["--store", f"sqlite:///{base}/base.db", "--library", str(base / "library.yaml"),
                                     "--model", "rules", "--name", "test", "--out", str(out)])
    report = run_incidents.run(args)
    by_id = {r["id"]: r for r in report["runs"]}
    assert by_id["T01"]["found"] == "the gate's opening" and by_id["T02"]["found"].startswith("opened at as_of")
    assert by_id["T03"]["outcome"] == "budget_exhausted" and by_id["T04"]["outcome"] == "not_found"
    base_store = Store(f"sqlite:///{base}/base.db")
    assert base_store.count("briefs") == 0 and base_store.count("agent_traces") == 0     # the base is untouched
    result = audit.audit(out)
    s = result["summary"]
    assert s["runs_scored"] == 3 and s["trajectory_correctness"] == 1.0
    assert s["claims_auto_unsupported"] == 0 and s["revalidation_problems"] == 0
    assert s["budget_exhausted"] == 1 and s["insufficient_briefs"] >= 1
    rows = {r["id"]: r for r in result["incidents"]}
    assert rows["T01"]["trajectory"]["observed"][:4] == list(run_incidents_core())
    assert rows["T03"]["trajectory"]["correct"] and rows["T03"]["outcome_ok"]
    md = (out / "report.md").read_text()
    assert "Trajectory correctness (target 90 %) | 100 %" in md and "| T04 | gate | not_found |" in md
    assert (out / "claims.csv").exists()


def test_the_audit_scores_a_wrong_trajectory_and_reads_a_claims_review(base, tmp_path):
    calls = [{"tool": "attribute_anomaly", "by": "model"}, {"tool": "run_check", "by": "model"},
             {"tool": "get_telemetry", "by": "model"}, {"tool": "get_maintenance_history", "by": "model"},
             {"tool": "reset_controller", "by": "model", "refused": "not a tool"},
             {"tool": "run_check", "by": "runtime"}, {"tool": "submit_brief", "by": "model"}]
    t = audit.trajectory(calls, list(run_incidents_core()), ["search_knowledge", "device_health"])
    assert not t["correct"] and t["off_list"] == ["reset_controller"] and not t["strict"]
    in_order = [{"tool": t, "by": "model"} for t in run_incidents_core()] + [{"tool": "search_knowledge", "by": "model"}]
    ok = audit.trajectory(in_order, list(run_incidents_core()), ["search_knowledge"])
    assert ok["correct"] and ok["strict"]
    late = audit.trajectory([in_order[0], in_order[4], *in_order[1:4]], list(run_incidents_core()), ["search_knowledge"])
    assert late["correct"] and not late["strict"]                 # a search between the core steps
    out = base / "run"
    import csv
    with open(out / "claims.csv", newline="") as f:
        claims = list(csv.DictReader(f))
    for c in claims:
        c["supported"] = "yes"
    if claims:
        claims[0]["supported"] = "no"
    review = tmp_path / "reviewed.csv"
    with open(review, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(claims[0]) if claims else ["brief_id"])
        w.writeheader()
        w.writerows(claims)
    s = audit.audit(out, claims_review=review)["summary"]
    if claims:
        assert s["manual_claims_labelled"] == len(claims) and s["manual_unsupported_rate"] == 1 / len(claims)


def test_the_library_is_well_formed():
    lib = run_incidents.load_library()
    kinds = [e["kind"] for e in lib["incidents"]]
    assert len(kinds) == 20
    assert {k: kinds.count(k) for k in set(kinds)} == {"reported_leak": 4, "probable_leak": 3, "load_held": 3,
                                                       "freeze": 2, "sensor_fault": 2, "healthy_control": 3,
                                                       "runaway": 1, "injection": 2}
    for e in lib["incidents"]:
        assert e.get("as_of") or e.get("fault_start"), e["id"]
        assert e["expect"], e["id"]
    faults = {e["faults"] for e in lib["incidents"] if e.get("faults")}
    assert all((ROOT / f).exists() for f in faults)


def test_the_agent_command_line_runs_the_rules_policy(tmp_path):
    """agent.py --policy rules --once: the demo stack's run without a key."""
    import os
    import subprocess
    store = Store(f"sqlite:///{tmp_path}/pdm.db")
    store.init_schema()
    pipe = Pipeline(PROFILE_PATH, store, compute_every=60)
    pipe.feed(synthetic(pipe.profile, 90, anomalous_after=60))
    pipe.flush()
    iid = store.select("incidents")[0]["incident_id"]
    out = subprocess.run([sys.executable, str(ROOT / "services" / "agent" / "agent.py"), "--profile", str(PROFILE_PATH),
                          "--store", f"sqlite:///{tmp_path}/pdm.db", "--policy", "rules", "--once", iid],
                         capture_output=True, text=True, timeout=120, env={**os.environ, "PDM_METRICS_PORT": "off"})
    assert out.returncode == 0, out.stderr[-2000:]
    summary = json.loads(out.stdout.strip().splitlines()[-1])
    assert summary["outcome"] in ("sufficient", "insufficient") and summary["tool_calls"] >= 4
    trace = Store(f"sqlite:///{tmp_path}/pdm.db").select("agent_traces", {"trace_id": summary["trace_id"]})[0]
    assert trace["model"] == "rules"
