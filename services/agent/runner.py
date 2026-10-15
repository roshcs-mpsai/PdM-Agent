"""The agent's bounded loop (T16; AGT-06 to AGT-08, AGT-11, AGT-12, BRF-02, BRF-03, INC-06).

One run diagnoses one incident in these steps. The runtime decides which step comes next and
writes a checkpoint after each one, so an agent that restarts mid-run carries on from the last
step instead of starting over (the role the FRD gives LangGraph's checkpointer):

    start      claim the incident (open -> diagnosing, once: INC-06), open the tool run, read the
               stored incident_context and write the first message
    model      check the budget, call the model once, keep its answer exactly as received
    tools      run the answer's tool calls in order; submit_brief goes to validation
    exhausted  the budget ran out before a brief was accepted: a partial brief, budget_exhausted
    fallback   the brief failed validation twice, or the model declined or refused the request:
               the fallback brief (insufficient, no claims, the checks that ran)
    released   the model could not be reached: the incident goes back to open, to be tried again
    done       write the trace

The model chooses what to look at; the runtime decides whether it may (AGT-08):

* tool calls: at most ``tool_calls`` calls of the six tools (refused calls count; submit_brief
  does not). A call over the budget is answered with a refusal that asks for the brief, and the
  brief that follows is flagged budget_exhausted;
* model turns, wall time and tokens (input not read from cache, cache writes, output): when one
  runs out before a brief is accepted, the runtime writes the partial brief itself;
* the brief: validated against the run's stored results; one repair turn, then the fallback
  (BRF-02). In the full arm the runtime runs every check itself before a fallback that has none,
  so the planner always gets the deterministic checks (BRF-03).

The model's answers are kept exactly as received, thinking blocks included, and the trace holds
the system prompt, the tool definitions, every message and every tool call, so a run can be
audited without calling the model again (AGT-11).
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Callable, Optional

import llm
import prompts
from toolsets import ToolRefused
from pdm_common import metrics, tracing
from pdm_common.brief import fallback_brief
from pdm_common.skills import versions as skill_versions
from pdm_common.timeutil import iso_utc

log = logging.getLogger("agent")

NODES = ("start", "model", "tools", "exhausted", "fallback", "released", "done")
OUTCOMES = ("sufficient", "insufficient", "budget_exhausted", "fallback", "model_unavailable", "claimed_elsewhere",
            "error")
SUMMARY_MAX = 1200


@dataclass(frozen=True)
class Budget:
    tool_calls: int = 12
    turns: int = 15
    wall_s: float = 180.0
    tokens: int = 50_000

    @classmethod
    def from_config(cls, cfg: dict) -> "Budget":
        return cls(**{k: cfg[k] for k in ("tool_calls", "turns", "wall_s", "tokens") if k in cfg})


def new_trace_id() -> str:
    return f"T-{uuid.uuid4().hex[:16]}"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Runner:
    """One agent configuration: the model, the tools, where records go and the budget.
    ``run(incident_id)`` diagnoses one incident; ``resume(state)`` carries on a stopped run."""

    def __init__(self, *, tools, model, sink, profile: dict, profile_sha256: str, skills: list,
                 budget: Budget = Budget(), arm: str = "full", strict_tools: bool = True,
                 survival_run: Optional[str] = None, study: bool = False,
                 clock: Callable[[], float] = time.monotonic):
        self.tools, self.model, self.sink = tools, model, sink
        self.asset = profile["asset"]["id"]
        self.profile_sha256 = profile_sha256
        self.skills = skill_versions(skills)
        self.budget = budget
        self.arm = arm
        self.survival_run = survival_run
        self.study = study                     # a study run: no claim, no lifecycle move (T34)
        self.clock = clock
        self.system = prompts.system_prompt(skills, asdict(budget))
        self.system_sha256 = prompts.prompt_sha256(self.system)
        specs = tools.specs(arm) + [prompts.submit_tool_spec()]
        self.tool_names = [s["name"] for s in tools.specs(arm)]
        self.tool_defs = llm.tool_definitions(specs, strict_tools)
        self.m = metrics.service_metrics("agent")
        self._run = None

    # -- running -------------------------------------------------------------------------------------
    def initial_state(self, incident_id: str, trace_id: Optional[str] = None, without_model: str = "") -> dict:
        return {"incident_id": incident_id, "trace_id": trace_id or new_trace_id(), "arm": self.arm,
                "node": "start", "started_at": iso_utc(_now()), "elapsed_s": 0.0, "turns": 0, "tool_calls": 0,
                "tokens": {"counted": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
                "messages": [], "calls": [], "model_turns": [], "brief_seq": 0, "repairs_left": 1,
                "budget_hit": None, "outcome": None, "brief_id": None, "brief_by": None, "context_id": None,
                "context": None, "fallback_reason": None, "error": None, "without_model": without_model,
                "study": self.study}

    def run(self, incident_id: str, trace_id: Optional[str] = None, *, without_model: str = "") -> dict:
        """Diagnose one incident. ``without_model``: skip the model and write the fallback brief
        with this reason (the model stayed unreachable through every try)."""
        return self._loop(self.initial_state(incident_id, trace_id, without_model))

    def resume(self, state: dict) -> dict:
        """Carry on a run from its last checkpoint (the agent restarted mid-run)."""
        state = copy.deepcopy(state)
        if state["node"] not in ("start", "done"):
            self._run = self.tools.open(state["incident_id"], state["trace_id"], state["arm"])
        log.info("resuming run %s on %s at step %s", state["trace_id"], state["incident_id"], state["node"],
                 extra={"incident_id": state["incident_id"], "trace_id": state["trace_id"]})
        return self._loop(state)

    def _loop(self, state: dict) -> dict:
        began = self.clock() - state["elapsed_s"]
        self._began = began
        self.m.agent_runs_in_progress.inc()
        try:
            with tracing.incident_trace(state["incident_id"], name="agent run", trace_id=state["trace_id"],
                                        arm=self.arm, model=self.model.name) as span:
                while state["node"] != "end":
                    node = state["node"]
                    try:
                        with tracing.span(f"agent {node}"):
                            nxt = getattr(self, f"_{node}")(state)
                    except Exception as exc:          # a bug must not leave the incident claimed and hidden
                        log.exception("run %s failed in step %s", state["trace_id"], node)
                        state["error"] = f"{type(exc).__name__}: {exc}"
                        state["outcome"] = "error"
                        if node == "done":            # the trace could not be written: nothing more to try
                            break
                        nxt = "done" if node == "released" else "released"
                    state["elapsed_s"] = round(self.clock() - began, 3)
                    state["node"] = nxt
                    if nxt != "end" and state["outcome"] != "claimed_elsewhere":
                        try:
                            self.sink.checkpoint(state)
                        except Exception:                  # recovery data: its loss must not stop the run
                            log.warning("run %s: the checkpoint after %s was not written", state["trace_id"], node,
                                        exc_info=True)
                span.set(outcome=state["outcome"], brief_id=state["brief_id"], tool_calls=state["tool_calls"],
                         turns=state["turns"], tokens=state["tokens"]["counted"])
        finally:
            self.m.agent_runs_in_progress.dec()
        self.m.agent_runs_total.labels(arm=self.arm, outcome=state["outcome"] or "error").inc()
        self.m.agent_run_seconds.labels(arm=self.arm).observe(state["elapsed_s"])
        return state

    def _elapsed(self) -> float:
        return self.clock() - self._began

    # -- steps ---------------------------------------------------------------------------------------
    def _start(self, state: dict) -> str:
        if not self.study:
            if not self.sink.claim(state["incident_id"], state["trace_id"]):
                state["outcome"] = "claimed_elsewhere"
                log.info("incident %s is not open; another run has it", state["incident_id"])
                return "end"
            state["claimed"] = True
        self._run = self.tools.open(state["incident_id"], state["trace_id"], self.arm)
        context = self.tools.context(self._run)
        state["context_id"] = context["result_id"]
        state["context"] = context["output"]
        if state["without_model"]:
            state["fallback_reason"] = state["without_model"]
            return "fallback"
        state["messages"] = [prompts.first_message(context, brief_id=self._brief_id(state, 1),
                                                   asset_id=self.asset, tools=self.tool_names)]
        return "model"

    def _model(self, state: dict) -> str:
        hit = self._spent(state)
        if hit:
            state["budget_hit"] = hit
            return "exhausted"
        try:
            turn = self.model.create(self.system, self.tool_defs, state["messages"],
                                     time_left=self.budget.wall_s - self._elapsed())
        except llm.DeadlineReached as exc:
            state["budget_hit"] = "wall time"
            state["error"] = str(exc)
            return "exhausted"
        except llm.ModelUnavailable as exc:
            state["error"] = str(exc)
            return "released"
        except llm.ModelError as exc:
            state["error"] = str(exc)
            state["fallback_reason"] = f"the language model API refused the request: {exc}"
            return "fallback"
        state["turns"] += 1
        usage = turn.usage or {}
        t = state["tokens"]
        t["counted"] += llm.counted_tokens(usage)
        t["input"] += int(usage.get("input_tokens") or 0)
        t["output"] += int(usage.get("output_tokens") or 0)
        t["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
        t["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)
        state["model_turns"].append(turn.record())
        state["messages"].append({"role": "assistant", "content": turn.content})
        if turn.stop_reason == "refusal":
            state["fallback_reason"] = "the model declined to answer"
            return "fallback"
        if turn.stop_reason == "model_context_window_exceeded":
            state["budget_hit"] = "context window"
            return "exhausted"
        if turn.tool_uses:
            state["cut_off"] = turn.stop_reason == "max_tokens"
            return "tools"
        state["messages"].append({"role": "user", "content": [{"type": "text", "text": prompts.NUDGE_CUT
                                  if turn.stop_reason == "max_tokens" else prompts.NUDGE_SUBMIT}]})
        return "model"

    def _tools(self, state: dict) -> str:
        answer = state["messages"][-1]["content"]
        replies = []
        for block in [b for b in answer if b.get("type") == "tool_use"]:
            name, args, use_id = block.get("name"), block.get("input") or {}, block.get("id")
            started = time.monotonic()
            if state.pop("cut_off", False) or state.get("_cut"):
                state["_cut"] = True
                replies.append(_reply(use_id, "Refused: this call was cut off at the output limit; call it again.",
                                      error=True))
                self._record(state, name, args, refused="cut off at the output limit", started=started)
                continue
            if name == prompts.SUBMIT:
                verdict, text = self._submit(state, args, started)
                if verdict == "accepted":
                    return "done"
                if verdict == "fallback":
                    return "fallback"
                replies.append(_reply(use_id, text, error=True))
                continue
            if state["tool_calls"] >= self.budget.tool_calls:
                state["budget_hit"] = "tool_calls"
                replies.append(_reply(use_id, prompts.budget_refusal(self.budget.tool_calls), error=True))
                self._record(state, name, args, refused="tool-call budget spent", started=started)
                continue
            state["tool_calls"] += 1
            with tracing.tool_call(name, args) as span:
                try:
                    result = self.tools.call(self._run, name, args)
                except ToolRefused as exc:
                    replies.append(_reply(use_id, f"Refused: {exc}", error=True))
                    self._record(state, name, args, refused=str(exc), started=started)
                    continue
                except Exception as exc:                 # a tool bug is reported to the model, not fatal
                    log.exception("tool %s failed", name)
                    replies.append(_reply(use_id, f"The tool failed ({type(exc).__name__}); do not retry it.",
                                          error=True))
                    self._record(state, name, args, refused=f"failed: {type(exc).__name__}: {exc}", started=started)
                    continue
                span.set(result_ids=[result["result_id"]])
            replies.append(_reply(use_id, _preview(result)))
            self._record(state, name, args, result_id=result["result_id"], started=started)
        state.pop("_cut", None)
        state["messages"].append({"role": "user", "content": replies})
        return "model"

    def _exhausted(self, state: dict) -> str:
        return self._runtime_brief(state, "budget_exhausted", f"the {state['budget_hit']} budget was spent")

    def _fallback(self, state: dict) -> str:
        return self._runtime_brief(state, "insufficient", state["fallback_reason"] or "no valid brief")

    def _released(self, state: dict) -> str:
        reason = state["error"] or "the language model could not be reached"
        if state.get("claimed"):                      # only the run holding the incident hands it back
            self.sink.release(state["incident_id"], state["trace_id"],
                              f"{reason}; the incident stays open for the next try")
        if state["outcome"] != "error":
            state["outcome"] = "model_unavailable"
        log.warning("run %s released %s: %s", state["trace_id"], state["incident_id"], reason,
                    extra={"incident_id": state["incident_id"], "trace_id": state["trace_id"]})
        return "done"

    def _done(self, state: dict) -> str:
        self.sink.trace(self.trace_row(state))
        state["context"] = None
        log.info("run %s on %s: %s, brief %s, %d tool calls, %d turns, %d tokens, %.1f s", state["trace_id"],
                 state["incident_id"], state["outcome"], state["brief_id"], state["tool_calls"], state["turns"],
                 state["tokens"]["counted"], state["elapsed_s"],
                 extra={"incident_id": state["incident_id"], "trace_id": state["trace_id"], "outcome": state["outcome"]})
        return "end"

    # -- briefs --------------------------------------------------------------------------------------
    def _submit(self, state: dict, args, started: float) -> tuple[str, str]:
        state["brief_seq"] += 1
        brief = self._complete(state, args)
        problems = self.sink.submit(brief, trace_id=state["trace_id"], arm=self.arm, move=not self.study)
        self._record(state, prompts.SUBMIT, {"brief_id": brief["brief_id"]}, started=started,
                     problems=problems or None, brief_id=brief["brief_id"])
        if not problems:
            state["brief_id"], state["brief_by"] = brief["brief_id"], "model"
            state["outcome"] = brief["evidence_state"]
            return "accepted", ""
        stage = "draft" if state["repairs_left"] > 0 else "repair"
        self.m.brief_rejections_total.labels(stage=stage).inc()
        if state["repairs_left"] > 0:
            state["repairs_left"] -= 1
            return "repair", prompts.repair_message(problems)
        state["fallback_reason"] = f"the brief failed validation twice ({problems[0]})"
        return "fallback", ""

    def _complete(self, state: dict, args) -> dict:
        """The model's brief with the runtime's fields: ids, versions, the draft status, and the
        budget flag when the run hit its budget before the brief."""
        brief = copy.deepcopy(args) if isinstance(args, dict) else {}
        brief.update(schema="brief_v1", brief_id=self._brief_id(state, state["brief_seq"]),
                     incident_id=state["incident_id"], asset_id=self.asset, versions=self._versions(state))
        if isinstance(brief.get("work_order"), dict):
            brief["work_order"]["status"] = "draft"
            brief["work_order"].setdefault("asset", self.asset)
        if state["budget_hit"] and brief.get("evidence_state") in ("sufficient", "insufficient"):
            note = f"Partial brief: the {state['budget_hit']} budget ran out before the diagnosis was finished. "
            brief["evidence_state"] = "budget_exhausted"
            brief["summary"] = (note + str(brief.get("summary") or ""))[:SUMMARY_MAX]
        return brief

    def _runtime_brief(self, state: dict, evidence_state: str, reason: str) -> str:
        """A brief written by the runtime (no claims). In the full arm it first runs every check
        itself when the run has none, so the brief lists them (BRF-03)."""
        results = self._run.results if self._run is not None else {}
        if self.arm == "full" and not any(r.get("tool") == "run_check" for r in results.values()):
            started = time.monotonic()
            try:
                result = self.tools.call(self._run, "run_check", {})
                self._record(state, "run_check", {}, result_id=result["result_id"], started=started, by="runtime")
            except Exception as exc:
                self._record(state, "run_check", {}, refused=f"failed: {exc}", started=started, by="runtime")
        state["brief_seq"] += 1
        brief = fallback_brief(brief_id=self._brief_id(state, state["brief_seq"]), incident_id=state["incident_id"],
                               asset_id=self.asset, results=self._run.results, versions=self._versions(state),
                               state=evidence_state, reason=reason)
        problems = self.sink.submit(brief, trace_id=state["trace_id"], arm=self.arm, move=not self.study)
        self._record(state, prompts.SUBMIT, {"brief_id": brief["brief_id"]}, problems=problems or None,
                     brief_id=brief["brief_id"], by="runtime", started=time.monotonic())
        if problems:                         # must not happen; never leave the incident hidden
            log.error("the runtime's own brief was refused: %s", problems)
            state["error"] = f"runtime brief refused: {problems[0]}"
            state["outcome"] = "error"
            return "released"
        state["brief_id"], state["brief_by"] = brief["brief_id"], "runtime"
        state["outcome"] = "budget_exhausted" if evidence_state == "budget_exhausted" else "fallback"
        return "done"

    # -- helpers -------------------------------------------------------------------------------------
    def _spent(self, state: dict) -> Optional[str]:
        if state["turns"] >= self.budget.turns:
            return "model turns"
        if self._elapsed() >= self.budget.wall_s:
            return "wall time"
        if state["tokens"]["counted"] >= self.budget.tokens:
            return "token"
        return None

    def _brief_id(self, state: dict, n: int) -> str:
        from pdm_common.diagnosis import run_token
        return f"B-{run_token(state['trace_id'])}-{n}"

    def _versions(self, state: dict) -> dict:
        ctx = state.get("context") or {}
        prognosis = ctx.get("prognosis") or {}
        return {"llm": self.model.name, "detector_run": (ctx.get("incident") or {}).get("detector_run_id"),
                "survival_run": prognosis.get("model_run_id") or self.survival_run, "skills": self.skills,
                "profile": self.profile_sha256, "prompt": self.system_sha256[:12]}

    @staticmethod
    def _record(state: dict, tool: str, arguments, *, started: float, result_id=None, refused=None, by="model",
                **extra) -> None:
        state["calls"].append(dict({"n": len(state["calls"]) + 1, "tool": tool, "arguments": arguments,
                                    "result_id": result_id, "refused": refused, "by": by,
                                    "seconds": round(time.monotonic() - started, 4)}, **extra))

    def trace_row(self, state: dict) -> dict:
        return {"trace_id": state["trace_id"], "incident_id": state["incident_id"], "arm": self.arm,
                "model": self.model.name, "skills": self.skills,
                "messages": {"system": self.system, "system_sha256": self.system_sha256, "tools": self.tool_defs,
                             "conversation": state["messages"], "turns": state["model_turns"]},
                "tool_calls": state["calls"],
                "budget": {"limits": asdict(self.budget),
                           "used": {"tool_calls": state["tool_calls"], "turns": state["turns"],
                                    "wall_s": state["elapsed_s"], "tokens": state["tokens"]["counted"]},
                           "tokens": state["tokens"], "hit": state["budget_hit"]},
                "outcome": state["outcome"], "started_at": state["started_at"], "finished_at": iso_utc(_now())}


def _reply(tool_use_id: str, text: str, error: bool = False) -> dict:
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}
    if error:
        block["is_error"] = True
    return block


def _preview(result: dict) -> str:
    return json.dumps({"result_id": result["result_id"], "tool": result["tool"], "output": result["output"]},
                      sort_keys=True, ensure_ascii=False)
