# Tool server

The agent's six read-only tools, answered as of the incident's opening (T15a;
AGT-01 to AGT-05, AGT-09, AGT-10, AGT-12).

```bash
python services/mcp_server/server.py --profile profiles/metropt3_apu/asset_profile.yaml \
    [--store "$PDM_STORE"] [--host 0.0.0.0 --port 8765]
```

Serves the tools over the Model Context Protocol (the `mcp` package's
MCPServer, streamable HTTP at `/mcp`). When `PDM_TOKEN_AGENT` is set, every
request needs `Authorization: Bearer $PDM_TOKEN_AGENT` (401 otherwise). Metrics
are on port 9105 (`pdm_tool_calls_total{tool,outcome}`,
`pdm_tool_call_seconds{tool}`).

`tools.py` is the server without a transport. The agent runner calls the same
`ToolServer` in process; the MCP endpoint is how another client (or the agent
across the stack's network) reaches it. Either way the code that answers is
the same.

## The tools

| Tool | Arguments | Answers |
|---|---|---|
| `get_telemetry` | `signals` (1-8 profile names), `hours_before` (default 3, at most 168), `max_points` (10-500, default 120) | per signal: unit, plausible range, first/last/min/max/mean and a downsampled series; the largest recording gap |
| `attribute_anomaly` | `top` (1-21, default 5) | the opening window's score and threshold, and its top features: share of the score, value, healthy median, z against healthy operation |
| `get_maintenance_history` | `days_before` (1-730, default 180) | failure reports and repairs from events.yaml, earlier incidents with their last decision, work orders sent |
| `search_knowledge` | `query`, `k` (1-5, default 3) | BM25 hits from corpus/, each as a quoted data block with doc_id, offsets, licence, url and SHA-256 |
| `run_check` | `check_ids` (optional; all when absent) | the asset's checks registry (C1-C11) on the data up to the opening |
| `device_health` | none | the latest decoded drive and controller status before the opening (G8 stretch; usually none) |

Over MCP every tool also takes `incident_id` and `trace_id` (the agent run), and
optionally `arm` (`full`, or `retrieval_only` for the study arm, which has
`search_knowledge` alone).

The run's context is a resource, not a seventh tool:
`pdm://incidents/{incident_id}/runs/{trace_id}/{arm}/context`. The agent
runtime reads it before the model's first turn; it opens the run, stores the
`incident_context` result as `R-<run>-1` and returns it with the run's tool
list. Runs are kept in memory; after a restart the next call for a trace
carries on from the results already stored (ids continue, the context is not
stored twice).

## Rules the server enforces

- **Allowlist (AGT-02).** Only the tools of the run's arm exist. A call to
  anything else, or with arguments its input schema refuses (unknown fields,
  out-of-range values, unknown signals or checks), is refused with a reason,
  logged and counted under `outcome="rejected"`. Nothing refused is stored.
- **As of the opening (AGT-04).** Every tool reads only what was recorded
  before the incident's `as_of`: telemetry and checks through
  `pdm_common.asof`, the maintenance history only records dated before it. The
  tests open an incident on 28 May 2020 and check that F2's report (30 May
  06:00) and repair (30 May 12:00) are absent, then ask again on the evening of
  30 May and find them.
- **Relative times.** Results give times as minutes before the opening, not
  dates (`--dates` turns this off). MetroPT-3 is public; a model that has read
  about it could recognise a failure by its date.
- **Stored results (AGT-03).** Each result is written to `tool_results`
  (insert-only) with the id `R-<run>-<n>` before the model sees it, so a brief
  can cite it and a reviewer can open it. The run's first result is
  `incident_context`: the incident, the gate's numbers, the NE 107 status and
  the last prognosis made before the opening, with the survival model's skill
  and whether it meets the 0.15 bar (it does not; the result says to report
  the probabilities as context). Briefs cite this result for the prognosis
  (BRF-05).
- **Documents are data (AGT-09).** Knowledge passages come back inside
  `<<<DOCUMENT ...>>>` / `<<<END DOCUMENT>>>` with a reminder line; the corpus
  sanitiser removes markup and invisible characters, and any run of three or
  more angle brackets inside a passage is escaped, so a passage cannot close
  its own block.

## Tests

`tests/test_mcp_tools.py` opens a real incident by running a synthetic
excursion through the in-process pipeline (eval/pipeline.py), then calls every
tool, checks the refusals, the as-of reads and the stored results, lists the
tools through the MCP SDK in process, and finally serves the endpoint with
uvicorn on a free port to check the bearer token and a call over streamable
HTTP.
