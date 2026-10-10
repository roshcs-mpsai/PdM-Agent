# Agent

Diagnoses each incident the gate opens and leaves a brief for a reviewer (T16, T17;
AGT-06 to AGT-08, AGT-11, AGT-12, BRF-01 to BRF-07, INC-06).

```bash
python services/agent/agent.py --profile profiles/metropt3_apu/asset_profile.yaml \
    [--store "$PDM_STORE" | --api http://localhost:8000] [--tools http://localhost:8765/mcp] \
    [--arm full|retrieval_only] [--config services/agent/config.yaml]
python services/agent/agent.py --profile ... --once INC-metropt3_apu-20200601T0024Z
python services/agent/agent.py --profile ... --study INC-... --arm retrieval_only
python services/agent/agent.py --profile ... --scripted services/agent/scripts/dry_run.json --once INC-...
```

Needs `ANTHROPIC_API_KEY`, unless `--scripted` replays fixed answers (a dry run: the
brief says no model read the evidence) or `--policy rules` runs the triage skill's rules
as code instead of a model (eval/policy.py; its briefs follow the checks, so a demo
without a key still shows different briefs for different incidents). With `--api` the agent writes only through the
HTTP API, with `PDM_TOKEN_AGENT`; with `--tools` it reaches the tools over MCP with the
same token. Without them it writes to the store and runs the tool code in process.
Metrics on port 9106.

## What a run does

| Step | |
|---|---|
| start | Claims the incident (open -> diagnosing, once: a second run finds it taken and stops). Opens the tool run; the tool server stores the incident context as result `R-<run>-1`. The first message holds that result and the ids to use. |
| model | Checks the budget, calls the model once, keeps its answer exactly as received (thinking blocks included). No tool call: one nudge to submit. |
| tools | Runs the answer's tool calls in order. Over the tool budget, a call is refused with a message asking for the brief. `submit_brief` goes to validation. |
| exhausted | A budget ran out before a brief was accepted: the runtime writes a partial brief, `budget_exhausted`. |
| fallback | The brief failed validation twice, or the model declined or the API refused the request: the runtime writes the fallback brief (insufficient, no claims). |
| released | The model could not be reached: the incident goes back to open and is tried again. |
| done | The trace is written to `agent_traces`. |

A checkpoint (`agent_checkpoints`) is written after every step; on start the service
resumes runs that stopped mid-way. Result ids continue from the store, so a resumed run
never reuses one.

## Rules the runtime enforces

- **Budgets** (`config.yaml`, AGT-08): 12 calls of the six tools (submit_brief not
  counted), 15 model turns, 180 s and 50,000 tokens. Tokens counted are input not read
  from the cache, cache writes and output, thinking included; cache reads are recorded
  but not counted, so the system prompt counts once. A brief submitted after the tool
  budget refused a call is flagged `budget_exhausted` and its summary starts "Partial
  brief"; when turns, time or tokens run out first, the runtime writes that partial brief.
- **Validation** (BRF-02): every brief goes through `pdm_common.diagnosis.accept_brief`
  (the API runs the same function). The runtime sets schema, brief_id, incident_id,
  asset_id, versions and the draft status; the model's text is never edited otherwise.
  One repair turn with the list of problems, then the fallback. Every submission is
  stored, valid or not.
- **Checks in every fallback** (BRF-03): in the full arm, a runtime brief on a run that
  has no run_check result first runs all the checks itself, so the planner gets them.
- **Model outage** (FRD section 5): after 2 retries inside the call, the run hands the
  incident back (diagnosing -> open, the incident stays on the dashboard as a raw
  alert). The service tries again after 5 and then 10 minutes; after the third failed
  try it writes the brief without the model (the checks and the context), so the
  alarm reaches a reviewer.
- **Study arm** (AGT-12): `--arm retrieval_only` offers search_knowledge alone, with
  the same system prompt and first message. `--study` writes such a brief for an
  incident the live agent already diagnosed, without claiming it or moving it.

## The model

`claude-opus-5-5` over the Messages API (`llm.py`, httpx; the anthropic SDK is not a
dependency). Decisions that follow from that model:

- **No temperature.** The model refuses any `temperature`, `top_p` or `top_k` other
  than the default (HTTP 400), so the FRD's "temperature 0" (AGT-11) cannot be set.
  Runs are audited from the stored transcript instead: `agent_traces` keeps the system
  prompt, the tool definitions, every message as sent and received, every tool call
  with its result id, the token use per turn and the budget.
- **Thinking is always on.** Its blocks arrive with an empty text and a signature; the
  runner echoes them back unchanged (the API refuses edited ones). `effort` in
  `config.yaml` (default `medium`) sets how much the model thinks. Thinking tokens are
  output tokens and count against the 50,000; if live runs end on the token budget,
  lower the effort or raise the budget at a sync, using the per-turn counts in the
  traces.
- **No forced tool choice.** `tool_choice` is `auto`: the system prompt says when to
  call submit_brief, and the runtime nudges once.
- **Strict tool use** (`strict_tools: true`): the inputs always match the tool schemas.
  Strict mode does not take length and number limits, so `llm.strict_schema` moves
  them into the description; the tool server and the validator still enforce them. If
  the API ever refuses a schema, set `strict_tools: false`.
- **Prompt caching.** The tools and system prompt (about 25 KB of skills) are cached;
  each request moves a breakpoint to the latest message.

## LangGraph

The FRD names LangGraph for the loop and its checkpointer. LangGraph is not installed in
the build environment (no package index), so the loop is the runner above, with its own
checkpoints. `graph.py` builds the same steps as a LangGraph graph (the runner's step
functions are the nodes; the step each returns is the edge), for use with LangGraph's
Postgres checkpointer. It is untested here; on a machine with langgraph,
`python -m pytest tests/test_agent.py -k langgraph` runs a scripted incident through it.

## Files

| File | |
|---|---|
| `agent.py` | the service: queue, worker thread, retries after an outage, resume, CLI |
| `runner.py` | the loop, budgets, validation and repair, runtime briefs, the trace |
| `llm.py` | the Messages API client, strict schemas, `ScriptedModel` for tests and dry runs |
| `prompts.py` | the system prompt (rules + skills), the first message, the submit_brief schema |
| `toolsets.py` | the tools in process or over MCP |
| `sinks.py` | the records straight to the store or through the API |
| `graph.py` | the LangGraph form of the loop |
| `config.yaml` | model, budgets, arm, outage waits |
| `scripts/dry_run.json` | a fixed script for a dry run without a key |

## Tests

`tests/test_agent.py` runs scripted models through the real runner, tool server and
store on incidents from a synthetic excursion: a full run and its trace, the claim, the
runaway (12 calls, 15 turns, a partial brief), the other budgets, one repair, the
fallback after two failures, the nudge, a refusal, the outage cycle with its waits and
the final brief without the model, an unexpected error, a resumed run, the study arm,
injected documents and an off-list call, the strict schemas, the API request (no
sampling parameters, caching, thinking echoed) and its retries, the tools over MCP. A
scripted model cannot show that the real model ignores injected instructions; that is
for T28's injection incidents with the pinned model.
