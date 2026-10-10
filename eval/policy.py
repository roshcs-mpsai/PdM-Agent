"""A deterministic stand-in for the language model: the triage skill's rules as code (T28).

``RulesModel`` answers the agent runtime the way the model would (``create(system, tools,
messages)`` returns the next turn), but each choice is a fixed rule over what the run has seen:

1. run_check, every check; 2. attribute_anomaly; 3. get_telemetry for the signals behind the top
features; 4. get_maintenance_history; 5. search_knowledge when a check failed; 6. device_health
when the motor did not follow the load command (C1-style data-quality failure); then the brief.

The brief follows skills/shared/incident_triage and brief_writing: an instrument check failing
makes instrumentation likely (sensor check); two fault signatures with clean data make a process
fault likely (inspection); a data-quality failure without a fault signature makes data quality
likely (data-quality check); anything less is insufficient. Claims quote the check results'
measured text and the attribution's shares, so every number comes from a cited result.

It serves three purposes: dry runs of the scripted-incident library without a model key (the
harness, the audits and the journeys run end to end on it), the baseline the live model's
trajectories and briefs are compared with, and the demo stack without a key
(``agent.py --policy rules``). It is not a diagnosis model: it never reads the telemetry it
fetches, and its hypotheses come from the checks alone.

``runaway=True`` gives the scripted runaway of PR-12: it keeps calling tools and never submits.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Optional, Sequence

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent"))

from llm import Turn  # noqa: E402

CORE = ("run_check", "attribute_anomaly", "get_telemetry", "get_maintenance_history")

TASKS = {
    "inspection": (["Survey the air path for leaks with the system pressurised (fittings, hoses, valves, drains)",
                    "Isolate, vent and repair what the survey finds",
                    "Confirm normal load and rest cycles in the data after the repair"],
                   ["Isolate, lock out and vent to zero before loosening anything",
                    "Let hot surfaces cool before work"]),
    "sensor_check": (["Compare the suspect sensor with a calibrated reference",
                      "Check the sensor's wiring and connector",
                      "Recalibrate or replace the sensor if it disagrees"],
                     ["Isolate and vent the instrument line before removing a sensor"]),
    "data_quality_check": (["Review the recorded data around the alarm (gaps, restarts, interpolated points)",
                            "Decide whether an inspection is needed"],
                           ["No work on the equipment is requested by this order."]),
    "monitor": (["Keep the asset under observation and review the next alarms",
                 "Decide whether an inspection is needed if the score stays high"],
                ["No work on the equipment is requested by this order."]),
}
TITLES = {"inspection": "Leak survey on the air production unit", "sensor_check": "Check the suspect sensor",
          "data_quality_check": "Review the alarm data", "monitor": "Watch the asset"}
PRIORITY = {"inspection": "high", "sensor_check": "medium", "data_quality_check": "low", "monitor": "low"}


def _results(messages: list) -> dict:
    """{tool: [result documents]} from the conversation (the first message holds the context)."""
    out: dict = {}
    for m in messages:
        if m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            text = b.get("content") if b.get("type") == "tool_result" else b.get("text")
            if not isinstance(text, str) or b.get("is_error"):
                continue
            try:
                doc = json.loads(text)
            except ValueError:
                continue
            if isinstance(doc, dict) and "result_id" in doc and "tool" in doc:
                out.setdefault(doc["tool"], []).append(doc)
    return out


def _called(messages: list) -> list[str]:
    return [b.get("name") for m in messages if m.get("role") == "assistant"
            for b in m.get("content") or [] if b.get("type") == "tool_use"]


def _hid(n: int) -> str:
    """A hypothesis id (brief_v1: H and a number)."""
    return f"H{n}"


def _sentence(text: str) -> str:
    text = " ".join(str(text).split())
    return text if text.endswith(".") else text + "."


@dataclass
class RulesModel:
    """The rules as a model. ``signals``: the profile's analog signal names (to map attributed
    features to the signals behind them); ``runaway``: never submit."""
    signals: Sequence[str] = ()
    runaway: bool = False
    query: Optional[str] = None                 # a fixed knowledge query (the injection cases)
    name: str = "rules"
    calls: list = field(default_factory=list)

    def create(self, system: str, tools: list, messages: list) -> Turn:
        offered = [t["name"] for t in tools]
        self.calls.append({"tools": offered, "turn": len(self.calls) + 1})
        blocks = self.step(offered, messages)
        for k, b in enumerate(blocks):
            if b.get("type") == "tool_use":
                b.setdefault("id", f"toolu_rules_{len(self.calls):02d}_{k}")
        usage = {"input_tokens": len(json.dumps(messages[-1:])) // 4, "output_tokens": len(json.dumps(blocks)) // 4,
                 "cache_read_input_tokens": 0 if len(self.calls) == 1 else len(system) // 4,
                 "cache_creation_input_tokens": len(system) // 4 if len(self.calls) == 1 else 0}
        stop = "tool_use" if any(b.get("type") == "tool_use" for b in blocks) else "end_turn"
        return Turn(content=blocks, stop_reason=stop, usage=usage, model=self.name)

    # -- choosing the next call -------------------------------------------------------------------
    def step(self, offered: list[str], messages: list) -> list[dict]:
        seen = _results(messages)
        called = _called(messages)
        if self.runaway:
            hours = 1 + len(called) % 24
            return [{"type": "tool_use", "name": "get_telemetry",
                     "input": {"signals": list(self.signals[:1]) or ["unknown"], "hours_before": hours}}]
        if "submit_brief" not in called:
            nxt = self._next_tool(offered, called, seen)
            if nxt is not None:
                return [nxt]
        # the brief, or the same brief again after a refusal (the runtime falls back if it fails twice)
        return [{"type": "tool_use", "name": "submit_brief", "input": self.brief(seen, offered)}]

    def _next_tool(self, offered: list[str], called: list[str], seen: dict) -> Optional[dict]:
        def use(name, **arguments):
            return {"type": "tool_use", "name": name, "input": arguments}
        checks = self._checks(seen)
        if "run_check" in offered and "run_check" not in called:
            return use("run_check")
        if "attribute_anomaly" in offered and "attribute_anomaly" not in called:
            return use("attribute_anomaly", top=5)
        if "get_telemetry" in offered and "get_telemetry" not in called:
            return use("get_telemetry", signals=self._signals(seen), hours_before=3)
        if "get_maintenance_history" in offered and "get_maintenance_history" not in called:
            return use("get_maintenance_history", days_before=30)
        failed = [c for c in checks if c.get("result") == "fail"]
        wants_search = bool(failed) or self.query is not None or "run_check" not in offered
        if "search_knowledge" in offered and "search_knowledge" not in called and wants_search:
            return use("search_knowledge", query=self._query(failed, seen), k=3)
        motor = [c for c in failed if c.get("kind") == "data_quality" and "motor" in str(c.get("check"))]
        if "device_health" in offered and "device_health" not in called and motor:
            return use("device_health")
        return None

    def _signals(self, seen: dict) -> list[str]:
        names = sorted(self.signals, key=len, reverse=True)
        out: list[str] = []
        for doc in seen.get("attribute_anomaly", [])[-1:]:
            for f in doc["output"].get("top_features") or []:
                match = next((s for s in names if f["feature"].startswith(s)), None)
                if match and match not in out:
                    out.append(match)
        for s in self.signals:                        # nothing attributable to a signal: the first two
            if len(out) >= 2:
                break
            if s not in out:
                out.append(s)
        return out[:4] or ["unknown"]

    def _query(self, failed: list[dict], seen: dict) -> str:
        if self.query:
            return self.query
        words = [str(c.get("check", "")).replace("_", " ").replace(".", " ") for c in failed[:2]]
        if not words:
            ctx = (seen.get("incident_context") or [{}])[0].get("output") or {}
            words = [str(c.get("check", "")).replace("_", " ").replace(".", " ")
                     for c in ctx.get("checks_at_opening") or [] if c.get("result") == "fail"][:2]
        return " ".join(words) or "anomaly score above threshold compressor"

    @staticmethod
    def _checks(seen: dict) -> list[dict]:
        docs = seen.get("run_check") or []
        return list(docs[-1]["output"].get("results") or []) if docs else []

    # -- the brief ---------------------------------------------------------------------------------
    def brief(self, seen: dict, offered: list[str]) -> dict:
        ctx = seen["incident_context"][0]
        check_docs = seen.get("run_check") or []
        checks_id = check_docs[-1]["result_id"] if check_docs else None
        checks = self._checks(seen)
        by_kind = {k: [c for c in checks if c.get("kind") == k and c.get("result") == "fail"]
                   for k in ("data_quality", "fault_signature", "instrument")}
        passed_dq = [c for c in checks if c.get("kind") == "data_quality" and c.get("result") == "pass"]
        attribution = (seen.get("attribute_anomaly") or [None])[-1]
        status = ((ctx["output"].get("status") or {}).get("ne107"))

        def claim(c):
            return {"text": _sentence(f"{c['id']} ({str(c['check']).replace('_', ' ')}) {c['result']}: "
                                      f"{c.get('measured') or ''}"), "cites": [checks_id]}

        def attribution_claims(n=2):
            if not attribution or not (attribution["output"].get("top_features")):
                return []
            out = []
            for f in attribution["output"]["top_features"][:n]:
                share = f.get("share_of_score")
                if share is None:
                    continue
                out.append({"text": f"The feature {f['feature']} carries {share:.2f} of the anomaly score in the "
                                    f"opening window.", "cites": [attribution["result_id"]]})
            return out

        hypotheses, state, action = [], "insufficient", "data_quality_check"
        if by_kind["instrument"]:
            hypotheses.append({"id": _hid(1), "category": "instrumentation", "statement": "A sensor reads wrong",
                               "likelihood": "likely", "claims": [claim(c) for c in by_kind["instrument"]]})
            if by_kind["fault_signature"]:
                hypotheses.append({"id": _hid(2), "category": "process", "statement": "A developing process fault",
                                   "likelihood": "possible",
                                   "claims": [claim(c) for c in by_kind["fault_signature"]]})
            state, action = "sufficient", "sensor_check"
        elif len(by_kind["fault_signature"]) >= 2 and not by_kind["data_quality"]:
            h = {"id": _hid(1), "category": "process", "statement": "An air leak keeps the compressor loaded",
                 "likelihood": "likely", "claims": [claim(c) for c in by_kind["fault_signature"]]}
            if passed_dq:                          # clean data around the alarm supports it
                h["claims"] += [claim(c) for c in passed_dq[:1]]
            hypotheses.append(h)
            state, action = "sufficient", "inspection"
        elif by_kind["data_quality"] and not by_kind["fault_signature"]:
            hypotheses.append({"id": _hid(1), "category": "data_quality", "statement": "The alarm comes from the data",
                               "likelihood": "likely", "claims": [claim(c) for c in by_kind["data_quality"]]})
            state, action = "sufficient", "data_quality_check"
        else:
            if by_kind["fault_signature"]:
                hypotheses.append({"id": _hid(1), "category": "process", "statement": "A developing process fault",
                                   "likelihood": "possible",
                                   "claims": [claim(c) for c in by_kind["fault_signature"]]})
            if by_kind["data_quality"]:
                hypotheses.append({"id": _hid(len(hypotheses) + 1), "category": "data_quality",
                                   "statement": "The alarm comes partly from the data", "likelihood": "possible",
                                   "claims": [claim(c) for c in by_kind["data_quality"]]})
            if not hypotheses and attribution_claims():
                hypotheses.append({"id": _hid(1), "category": "process", "statement": "Operation unlike the healthy "
                                   "reference, cause not shown", "likelihood": "possible",
                                   "claims": attribution_claims()})
            action = "monitor" if checks and not by_kind["data_quality"] and passed_dq else "data_quality_check"
        priority = PRIORITY[action]
        if action == "inspection" and status == "failure":
            priority = "urgent"
        cites = [checks_id] if checks_id else [ctx["result_id"]]
        tasks, safety = TASKS[action]
        summary = {
            ("sufficient", "inspection"): "Fault-signature checks fail on clean data, the pattern of an air leak. "
                                          "Survey the air path.",
            ("sufficient", "sensor_check"): "An instrument check fails: a sensor disagrees with its partner. Check "
                                            "the sensor before any repair.",
            ("sufficient", "data_quality_check"): "A data-quality check fails and no fault signature does: the alarm "
                                                  "most likely comes from the data. Review the recorded data.",
        }.get((state, action), "The results do not point to one cause. "
                               + ("Keep the asset under observation." if action == "monitor"
                                  else "Review the recorded data before any inspection."))
        return {"evidence_state": state, "summary": summary, "hypotheses": hypotheses,
                "prognosis": {"cites": [ctx["result_id"]]},
                "recommended_action": {"type": action, "text": TASKS[action][0][0] + ".", "cites": cites},
                "work_order": {"title": TITLES[action], "priority": priority, "tasks": copy.deepcopy(tasks),
                               "safety_notes": copy.deepcopy(safety)},
                "checks_run": [d["result_id"] for d in check_docs]}
