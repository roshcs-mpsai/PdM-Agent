# Connector

Sends approved work orders to the maintenance system's REST endpoint (T22;
INT-04 to INT-06, HIL-06).

```bash
PDM_WORKORDER_TOKEN=<token> python services/connector/connector.py [--store "$PDM_STORE"] \
    [--endpoint http://localhost:8088/api/work-orders] [--interval 5] [--once]
```

Reads the store's `work_orders`, `decisions` and `incidents` tables. Writes
`work_orders` (status, attempts, error, reference) and moves the incident
approved -> sent through `incident_events`. Only the approval API creates
work_orders rows, with `pdm_common.workorders.queue` in the same transaction as
the decision; the connector only sends them. While it loops (not with
`--once`), metrics and /health are on port 9108 (`PDM_METRICS_PORT` changes it).

| Setting | Meaning |
|---|---|
| `PDM_WORKORDER_TOKEN` | the endpoint's bearer token; required (`PDM_TOKEN_CONNECTOR`, the name docs/SECURITY.md uses, also works) |
| `--endpoint` or `PDM_WORKORDER_URL` | default `http://localhost:8088/api/work-orders` (the simulator) |
| `--store` or `PDM_STORE` | default `sqlite:///runs/pdm.db` |
| `--interval` | seconds between runs, default 5 |
| `--once` | one run, print the report, exit 1 if the run failed |

Against the simulator (`tools/workorder_sim.py`, see services/south/README.md):

```bash
export WORKORDER_SIM_TOKEN=<token>
python tools/workorder_sim.py --port 8088 --fail-first 2 &
PDM_WORKORDER_TOKEN="$WORKORDER_SIM_TOKEN" python services/connector/connector.py --interval 2
```

## Rules the connector enforces

Every few seconds it takes the rows that are due (queued, or failed and
waiting to retry, with `next_attempt_at` reached), oldest first.

- **No approval, no order (HIL-06).** Before every POST: the decision the row
  names exists, its action is approve or edit, it is for the row's incident,
  and the incident is approved (or already sent, for a resend after a lost
  response). Otherwise the row is blocked with reason `no_approval`. A row
  written straight into the table, a rejected incident and an incident still
  awaiting review never reach the endpoint.
- **The approved text, by hash (INT-06).** The row's `text_sha256` must be the
  SHA-256 of the decision's approved text (title, asset, priority, tasks and
  safety notes, as canonical JSON; the draft status is not part of it). The
  body's title, priority, tasks and safety notes must hash to the same value.
  Every other body field must be what the decision gives (approval id, work
  order id, incident, asset, reviewer, time, brief), with nothing added, and
  the order must be for the incident's asset. Otherwise the row is blocked
  with reason `tampered`. Recomputing the hashes after an edit does not get
  past this: the reference is the decision row, which is insert-only.
- **Authenticated (INT-05).** Every POST carries `Authorization: Bearer
  <token>`. Without a token the connector does not start. The token is never
  written to the log, to `last_error` (an endpoint that echoes it back gets
  `[token]`) or to the report.
- **Exactly once (INT-04).** The `Idempotency-Key` is the decision id, and the
  body is the one stored at approval time, sent byte for byte the same on every
  attempt (canonical JSON). After a lost response the retry gets 200 and the
  same reference, and the endpoint still holds one order.
- **Retries.** 5xx, 408, 429, timeouts and refused connections are retried
  after 5, 10, 20 ... s, at most 300 s apart. After 8 attempts the row is
  blocked. A 409 (the endpoint holds a different body for this approval) and
  any other 4xx block at once and are never retried automatically.
- **One sender per row.** Every change to a row is a compare-and-set on its
  status and `next_attempt_at`. Before posting, the connector claims the row by
  moving `next_attempt_at` 60 s ahead, so a second connector finds nothing due.
  A row whose update changes nothing was changed by someone else and is left
  alone (`skipped`). If the connector dies mid-request, the row is due again
  when the claim runs out, and the idempotency key makes that retry safe.
- **Alerts.** Every blocked row logs an error naming the work order, the
  incident and the reason, and counts `pdm_work_orders_blocked_total{reason}`.

## Statuses

| Status | Meaning | Then |
|---|---|---|
| `queued` | written by the approval API | sent on the next run |
| `failed` | a send failed for a reason that may pass (5xx, 408, 429, timeout, no connection) | retried at `next_attempt_at` |
| `sent` | the endpoint accepted it: `reference` and `sent_at` set, the incident is `sent` | nothing |
| `blocked` | refused (`no_approval`, `tampered`, `conflict`, `rejected_by_endpoint`) or given up after 8 attempts; the reasons are in `last_error` | a person decides |

`attempts` counts the POSTs made. `last_error` holds the latest error and is
cleared when the order is sent. The rows with status `failed` or `blocked`
and their `last_error` are the failures the dashboard should show (INT-04).
To send a blocked row again after fixing the cause (a token, the endpoint),
set its status back to `queued` and its attempts to 0; the checks run again
first.

## Metrics

| Metric | Labels | Meaning |
|---|---|---|
| `pdm_work_order_posts_total` | outcome | one per POST: `sent`; `duplicate` (the endpoint already had it); `retry`; `failed` (blocked by the answer, or the last attempt) |
| `pdm_work_orders_blocked_total` | reason | `no_approval`, `tampered`, `conflict`, `rejected_by_endpoint` |
| `pdm_work_orders_pending` | | rows queued or failed |

`pdm_up{service="connector"}` is 0, and /health answers 503, while a run
fails (for example, the store cannot be read).

## Tests

```bash
python -m pytest -q tests/test_connector.py tests/test_workorders.py
```

`tests/test_connector.py` runs the connector against the simulator through
starlette's TestClient. Incidents move through the real lifecycle, decisions
are queued as the API queues them, and the clock is injected, so no test
waits. The tests show:

- an approved order is sent once, the incident moves to sent (actor
  `connector`), and the endpoint lists one order;
- with two injected 503s the order is retried after 5 s and then 10 s and sent
  on the third run, still one order; the wait doubles up to its cap;
- after a lost response (the order posted before, or the answer to the
  connector's own POST lost) the retry gets 200 and the same reference, and
  there is still one order; an incident already sent is not moved again;
- a changed order (title edited, row hash changed, text changed with both
  hashes recomputed, a field added, another asset) is blocked as tampered,
  with an error logged and nothing sent;
- no approval (no decision row, also with no due time; a reject; an incident
  awaiting review; another incident's decision) is blocked and nothing is sent;
- 409, a wrong token and every class of answer get their outcome, and the
  token stays out of the logs, the errors and the report;
- a stale copy of a row and a row a person blocked are skipped; a second
  connector running while a POST is in flight finds nothing due; after a crash
  mid-request the row is due again when the claim ends;
- `main()` refuses to start without a token (before it opens the store), runs
  once, and in the loop stops on SIGTERM after the row in hand.

`tests/test_workorders.py` covers the shared module: the hash ignores key
order and the draft status, the body passes the simulator's own validation,
`queue` refuses a reject and commits or rolls back with the caller's
transaction, and each case of `send_problems`.

## Not verified here

- The connector has run only in process, against the simulator, on SQLite.
  The PostgreSQL path (the same SQL through psycopg) and a real maintenance
  system have not been tried; psycopg is not installed where this was written.
- An `http://` endpoint on another host gets a warning at start-up (the token
  would cross the network unencrypted), not a refusal.
- compose.yaml runs it (with the work-order simulator as the endpoint), but the
  stack has not been started where this was written (docs/RUNBOOK.md, known limits).
