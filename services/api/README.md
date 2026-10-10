# API

The HTTP bridge between the store and its users: the dashboard reads through it and
reviewers decide through it; in the stack the agent writes through it too (T20;
HIL-01 to HIL-08, INT-05, FRD section 9).

```bash
python services/api/main.py [--store "$PDM_STORE"] [--host 0.0.0.0 --port 8000]
```

Starlette and uvicorn. The FRD names FastAPI, which could not be installed in the build
environment; the routes and roles are the FRD's, and moving them to FastAPI later is a
change of decorators. Metrics are on port 9107 with the other services' (`/metrics` and
`/health` there), which only the core network reaches; port 8000 serves `/health` and
the routes below.

## Sign-in and roles

Every request except `/health` and `/metrics` carries `Authorization: Bearer <token>`.
Tokens come from the environment: `PDM_TOKEN_<ROLE>` for a role, and
`PDM_TOKEN_<ROLE>_<NAME>` for one person, e.g. `PDM_TOKEN_REVIEWER_ROSH`. A reviewer
signs in to the dashboard with their own token and every decision records their name
(HIL-07). The API refuses to start with no token, a token under 32 characters, or the
same token in two variables. No token or an unknown one: 401; a known token on an
endpoint its role may not use: 403. Tokens are compared in constant time and never
logged or echoed.

| Method and path | Roles | |
|---|---|---|
| GET /health | none | liveness |
| GET /me | any | who the token belongs to |
| GET /assets | people | NE 107 status, P(24/72/168 h) with bounds and the model's skill, open incidents, last update; highest P(72 h) first (HIL-01) |
| GET /assets/{id} | people | the same, the latest alert's per-feature attribution, the open incidents (HIL-02) |
| GET /assets/{id}/series | people | scores and threshold, prognoses with bounds; `?from=&to=`, default the last 7 days of stream time |
| GET /incidents | people, agent | the queue with each incident's current brief, decision and work order; `?state=&asset_id=&open=1` |
| GET /incidents/{id} | people | the incident, its events, the current brief with every cited result expanded, the drafts, the trace summary, decisions, work orders and a banner for insufficient or budget_exhausted briefs (HIL-03, HIL-04) |
| GET /results/{id} | people | one stored tool result |
| GET /work-orders | people | work orders with status, attempts and last error (INT-04) |
| POST /briefs/{id}/decisions | reviewer | `{"action": "approve" \| "edit" \| "reject", "reason": "...", "work_order": {...}}` (HIL-05) |
| GET /decisions/{id} | people | the audit rebuild (HIL-08) |
| GET /traces/{id} | engineer, operator, evaluator | the agent's full trace (AGT-11) |
| POST /incidents/{id}/claim, /release, /briefs | agent | the agent's claim, hand-back and briefs |
| POST /agent/runs/{trace}/checkpoint, /trace | agent | the run's checkpoint and trace |
| GET /agent/runs/unfinished, /agent/incidents/{id}/outages | agent | runs to resume, outage tries |

"People" is reviewer, engineer, operator and evaluator. The study endpoints (T34) are in
`study.py`.

## Decisions

A decision is made on the current brief of an incident awaiting review, and needs a
reason:

- **approve**: the draft work order as the agent wrote it;
- **edit**: the reviewer's work order (checked against brief_v1's work-order schema),
  with a unified diff against the draft;
- **reject**: no work order; the incident moves rejected -> closed.

The `decisions` row (insert-only) holds the brief, the reviewer, the action and reason,
the approved text and diff, the SHA-256 of the evidence the reviewer saw (the brief,
every result it cites and the incident), the versions (the brief's plus the API's) and
the agent's trace id. In the same transaction the incident moves and, for approve or
edit, `pdm_common.workorders.queue` writes the work order the connector will send.
No other route writes `work_orders` (HIL-06); `tests/test_api.py` posts to every other
route as every role and checks the table is unchanged.

`GET /decisions/{id}` rebuilds the decision from the store: the brief, the evidence
snapshot built again and whether it still hashes to the recorded value, the trace,
the incident's events and whether they replay to the stored state, and the work order.

## Briefs from the agent

`POST /incidents/{id}/briefs` with `{"brief", "trace_id", "arm", "move"}` runs
`pdm_common.diagnosis.accept_brief`, the same function the agent's direct sink uses:
the brief is validated against the results its run stored, stored either way, and a
valid one moves the incident to awaiting_review. 201 when accepted; 422 with the
problems otherwise (counted in `pdm_brief_rejections_total{stage="api"}`).

## Tests

`tests/test_api.py`: tokens and roles, the read views, each decision with its record
and work order, refused decisions, the HIL-06 negative test, the audit rebuild with a
record whose hash no longer matches, and an agent run whose records all go through
the API (`services/agent/sinks.py ApiSink`).
