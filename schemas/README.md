# schemas

JSON Schema (2020-12) for every message, tool result and brief (T17; FRD section 9).
`pdm_common.schemas.validate(name, doc)` returns the problems; tests/test_schemas.py
runs the services in process and puts every message they publish through its schema.

| Schema | Topic or table | Publisher | Documented in |
|---|---|---|---|
| telemetry_json_v1 | pdm/\<asset>/telemetry | replayer, south | services/replayer/README.md |
| window_v1 | pdm/\<asset>/windows, windows | ingest | services/ingest/README.md |
| score_v1 | pdm/\<asset>/scores, scores | detector | services/detector/README.md |
| prognosis_v1 | pdm/\<asset>/prognosis (retained), prognoses | prognostics | services/prognostics/README.md |
| incident_v1 | pdm/\<asset>/incidents | gate | services/gate/README.md |
| status_v1 | pdm/\<asset>/status (retained), status_log | gate | services/gate/README.md |
| drift_v1 | pdm/_sys/drift (retained), drift_alerts | drift | services/drift/README.md |
| device_event_v1 | pdm/\<asset>/device, device_events | south (G8 stretch) | services/south/README.md |
| tool_result_v1 | tool_results | MCP tool server | services/mcp_server/README.md |
| brief_v1 | briefs | agent | below |

## brief_v1

What the agent must produce and `pdm_common.brief.validate_brief` checks:

- every claim and counter-claim cites one or more results this agent run produced
  for this incident (BRF-02); so do the recommended action and `checks_run`;
- `prognosis` cites the stored prognosis (the run's `incident_context` result) and
  holds no number of its own (BRF-05);
- every number written in a claim appears in a result that claim cites, within the
  rounding it was written with: "0.19" stands for 0.1889, "92" for a share of 0.92
  (BRF-06);
- hypotheses are tagged process, instrumentation, device or data_quality with an
  ordinal likelihood (BRF-04); the only probabilities in a brief are the cited
  prognosis values;
- `evidence_state` is sufficient, insufficient or budget_exhausted; an insufficient
  brief lists its checks and recommends a data-quality check or monitoring, never an
  inspection (BRF-03);
- the work order has a title, priority, tasks and safety notes and stays a draft
  until a reviewer approves it (BRF-07).

A brief that fails gets one repair turn; after that the agent stores
`fallback_brief`, an insufficient-evidence brief with no claims.
