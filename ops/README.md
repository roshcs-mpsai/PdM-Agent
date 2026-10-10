# Observability

Metrics, logs and traces for the PdM-Agent stack (T24, T26; OPS-01 to OPS-03,
build guide R3.5). Service health and model health are watched separately: two
Grafana dashboards, two alert groups.

```
every service --/metrics on :91xx--> Prometheus (scrapes every 5 s, alerts.yml) --> Grafana: service health, model health
agent, mcp_server --OTLP/HTTP :4318--> otel-collector --OTLP gRPC--> Jaeger (UI :16686)
every service --one JSON object per line on stderr--> docker compose logs <service>
```

The agent_traces table stays the audit record of every agent run; spans are for
live debugging and may be lost (batching, a collector restart).

## Files

| Path | What it is |
|---|---|
| `lib/pdm_common/metrics.py` | Prometheus client: `Registry`, `Counter`, `Gauge`, `Histogram`, text format 0.0.4, `serve()` for /metrics and /health, the metric catalogue, `service_metrics(name)`, `PORTS` |
| `lib/pdm_common/logs.py` | `setup(service)`: JSON log lines; `context(**fields)`: fields on every line inside a block |
| `lib/pdm_common/tracing.py` | `configure(service)`, `incident_trace()`, `tool_call()`, `span()`, `inject()`/`extract()` over the OpenTelemetry API |
| `prometheus/prometheus.yml` | one scrape job per compose service, every 5 s |
| `prometheus/alerts.yml` | service down, p95 scoring latency over 2 s, quarantine rate rising, brief rejections |
| `grafana/provisioning/` | Prometheus (uid `pdm-prometheus`) and Jaeger data sources; the dashboard provider |
| `grafana/dashboards/service_health.json` | up, ready, restarts, messages in/out per topic, handler p95, queue depth, handler errors, quarantine by code, ingest reconciliation |
| `grafana/dashboards/model_health.json` | p95 scoring latency (and the share within 2 s), windows scored/dropped/refused, alert rate, incidents, gate decisions, NE 107, drift, agent runs, tool calls, brief rejections, agent run time |
| `otel/collector.yml` | OTLP in on 4317 (gRPC) and 4318 (HTTP), batch, OTLP out to `jaeger:4317` |

## Ports

/metrics and /health per service:

| ingest | detector | gate | prognostics | mcp_server | agent | api | connector | opcua_north | drift | south |
|---|---|---|---|---|---|---|---|---|---|---|
| 9101 | 9102 | 9103 | 9104 | 9105 | 9106 | 9107 | 9108 | 9109 | 9110 | 9111 |

Platform: Prometheus 9090, Grafana 3000, otel-collector 4317 (gRPC), 4318 (HTTP)
and 13133 (health), Jaeger UI 16686.

`pdm_common.metrics.PORTS` holds the service ports; `PDM_METRICS_PORT` overrides
one (`off` switches the endpoint off). The api serves its metrics on 9107, on the
core network, not on its public port 8000.

## Metrics

Every name lives in the catalogue in `pdm_common/metrics.py` (`STANDARD`,
`SERVICE`); the dashboards and alert rules use nothing else, and
tests/test_metrics.py fails if they do. Add a metric there first.

| Metric | Type | Labels | Exported by |
|---|---|---|---|
| `pdm_up` | gauge | service | every service: 1 when it can work, 0 when it runs but cannot (no broker) |
| `pdm_process_start_time_seconds` | gauge | | every service; a change is a restart |
| `pdm_messages_in_total`, `pdm_messages_out_total` | counter | topic | every service |
| `pdm_handler_errors_total` | counter | topic | every service: a handler raised, the message was skipped |
| `pdm_handler_seconds` | histogram | topic | every service: time in the handler per message |
| `pdm_queue_depth` | gauge | queue | services with a queue of their own |
| `pdm_records_in_total`, `_accepted_total`, `_lost_total`, `_out_of_range_total` | counter | asset | ingest |
| `pdm_records_quarantined_total` | counter | asset, code | ingest (the codes of services/ingest/README.md) |
| `pdm_windows_published_total`, `pdm_windows_dropped_total` | counter | asset | ingest |
| `pdm_windows_scored_total`, `pdm_alerts_total` | counter | asset | detector |
| `pdm_windows_refused_total` | counter | asset, reason | detector |
| `pdm_scoring_seconds` | histogram | asset | detector: window received to score stored and ready to publish |
| `pdm_gate_decisions_total` | counter | asset, decision | gate: open, attach, suppress, close |
| `pdm_incidents_opened_total` | counter | asset | gate |
| `pdm_ne107_status` | gauge | asset, status | gate: 1 for the current status, 0 for the other four |
| `pdm_ne107_status_changes_total` | counter | asset, to | gate |
| `pdm_prognoses_total` | counter | asset | prognostics |
| `pdm_drift_checks_total`, `pdm_drift_alerts_total` | counter | asset | drift |
| `pdm_tool_calls_total` | counter | tool, outcome | mcp_server: ok, rejected (allowlist or input schema), error; a name outside the allowlist counts as tool `unknown` |
| `pdm_tool_call_seconds` | histogram | tool | mcp_server |
| `pdm_agent_runs_total` | counter | arm, outcome | agent: sufficient, insufficient, budget_exhausted, fallback, error |
| `pdm_agent_run_seconds` | histogram, 1 to 300 s | arm | agent |
| `pdm_agent_runs_in_progress` | gauge | | agent |
| `pdm_brief_rejections_total` | counter | stage | agent (draft, repair), api (api) |
| `pdm_http_requests_total` | counter | route, method, code | api (route template, never the raw path) |
| `pdm_http_request_seconds` | histogram | route, method | api |
| `pdm_decisions_total` | counter | action | api: approve, edit, reject |
| `pdm_work_order_posts_total` | counter | outcome | connector: sent, duplicate, retry, failed |
| `pdm_work_orders_blocked_total` | counter | reason | connector: no_approval, tampered, conflict, rejected_by_endpoint (alert on any increase) |
| `pdm_work_orders_pending` | gauge | | connector: rows queued or waiting to retry |

Duration histograms use buckets from 1 ms to 10 s with an edge at 2 s, so "p95
under 2 s" is read off a bucket count rather than interpolated; the agent's run
time uses 1 to 300 s (its budget is 180 s). Label values are bounded sets;
free text (validator messages, tool arguments) goes to logs and spans.

A service exports its metrics like this:

```python
from pdm_common import metrics
M = metrics.service_metrics("detector")          # the standard set plus the detector's own
metrics.serve(metrics.REGISTRY, metrics.port_for("detector"))

with M.handling(topic):                          # in, time and errors for one message
    outs = handler(payload)
with M.scoring_seconds.labels(asset=asset).time():
    ...
M.windows_scored_total.labels(asset=asset).inc()
```

**Create label sets at start-up.** A counter series that first appears already at
1 shows no increase to `rate()` or `increase()`, so the first quarantined record
or brief rejection would never reach a panel or an alert. Each service calls
`.labels(...)` once at start-up for the values it knows: ingest every quarantine
code (`ERROR_CODES`) for its asset, the detector its refusal reasons, the gate
every decision and NE 107 status, the agent the stages draft and repair and its
outcomes, the MCP server each tool with ok, rejected and error.

**What scoring latency measures.** Time inside the detector for one window,
store write included. It does not include the broker hop, and it cannot be
measured end to end from the payloads: they carry stream time only. A window
also waits in ingest until later data cannot change it (about 30 s of stream
time, 3 s at 10x); that wait is by design and is not latency.

**Queue depth.** run_service handles each message on paho's network thread, so
a backlog waits in the broker, where the service cannot see it; a broker that
drops queued messages shows up as `pdm_records_lost_total` at ingest. The
queues that are visible are: ingest's stream buffer (`StreamWindows.buffered()`)
and its telemetry batch, the agent's incidents waiting for a run, the MCP
server's calls in flight, and the connector's queued work orders.

## Logs

`logs.setup(service)` replaces `logging.basicConfig(...)` in a service's main;
loggers and `log.info("... %s", x)` calls stay as they are.

```json
{"ts": "2026-10-10T13:45:01.123Z", "level": "INFO", "service": "agent", "logger": "agent", "message": "brief stored",
 "incident_id": "INC-metropt3_apu-20200715T0030Z", "trace_id": "T-0012-1", "result_ids": ["R-0012-3"],
 "otel_trace_id": "4bf92f3577b34da6a3ce929d0e0e4736", "otel_span_id": "00f067aa0ba902b7"}
```

- `ts` is wall-clock UTC. Stream times stay in the message and in payloads.
- `incident_id`, `trace_id`, `result_id`, `result_ids` and `asset` come from
  `extra={...}` or from `with logs.context(incident_id=..., trace_id=...):`, which
  adds them to every line inside the block (the thread and its asyncio tasks).
- `otel_trace_id` and `otel_span_id` appear while a span with a valid context is
  current, so a log line leads straight to its trace in Jaeger.
- `exception` holds the traceback of `log.exception(...)`.
- Level: `PDM_LOG_LEVEL` (default INFO). Lines go to stderr; stdout stays free
  for the reports services print at shutdown.

`trace_id` in logs and spans is the agent run's key in agent_traces. The OpenTelemetry
trace id is a different, random id; the agent can store it with the run (it is
`tracing.current_ids()[0]` inside the incident trace) to give the dashboard a direct
Jaeger link.

## Traces

```python
from pdm_common import tracing
tracing.configure("agent")                       # once, at start-up
with tracing.incident_trace(incident_id, trace_id=run_id, arm="full"):
    with tracing.tool_call("get_telemetry", arguments, incident_id=incident_id) as span:
        result = ...                             # the MCP call
        span.set(result_ids=[result["result_id"]])
    with tracing.span("brief", brief_id=brief_id, result_ids=cited, evidence_state=state):
        ...
```

- `configure` exports over OTLP/HTTP to `$OTEL_EXPORTER_OTLP_ENDPOINT` (default
  `http://otel-collector:4318`; from a host process use `http://localhost:4318`).
  It needs `opentelemetry-sdk` and `opentelemetry-exporter-otlp-proto-http` in the
  agent's and MCP server's requirements (not added yet). Without them it logs once
  `tracing off ...` and every span is a no-op. `OTEL_SDK_DISABLED=true` turns it off.
- `incident_trace` always starts a new trace: one trace per agent run, with
  `incident_id` (and `trace_id`) on the root span and on every log line inside.
  A retried run on the same incident is a second trace with the same tag.
- `tool_call` records the tool, its arguments as canonical JSON and, once known,
  the result ids. Attribute values longer than 4096 characters are cut.
- So that the MCP server's spans land in the agent's trace, the agent sends
  `tracing.inject({})` as request headers (or in the MCP request's `_meta`) and the
  server starts its span with `tracing.span(..., context=tracing.extract(headers),
  kind="server")`. Without that, the server's spans are separate traces that still
  carry `incident_id` if the server sets it.

### Finding an incident's trace

A trace id cannot be derived from the incident id with the OpenTelemetry API (the
SDK generates trace ids), so the incident id is a tag on the root span:

1. Jaeger UI, http://localhost:16686, Search: Service `agent`, Tags
   `incident_id=INC-metropt3_apu-20200715T0030Z`, Find Traces.
2. The lookback filter is wall-clock time of the run, not the incident's stream
   time: an incident from July 2020 replayed today is under "Last hour", not July 2020.
3. The same search works from Grafana: Explore, data source Jaeger, query type
   Search, the same service and tag.
4. From the logs: `docker compose logs agent | grep INC-metropt3_apu-20200715T0030Z`
   gives `otel_trace_id`, which Jaeger's "Lookup by Trace ID" opens directly.

Jaeger's HTTP API (the one its UI uses; not a stable interface):

```bash
curl -sG http://localhost:16686/api/traces --data-urlencode service=agent \
  --data-urlencode 'tags={"incident_id":"INC-metropt3_apu-20200715T0030Z"}' --data-urlencode limit=20
```

## Running it

The lead adds these to compose.yaml (images to be pinned by digest, NFR-05; the
tags below are suggestions and were not pulled here):

```yaml
  prometheus:
    image: prom/prometheus:v2.54.1
    volumes: ["./ops/prometheus:/etc/prometheus:ro"]
    ports: ["127.0.0.1:9090:9090"]
    restart: unless-stopped
  grafana:
    image: grafana/grafana:11.2.2
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_PASSWORD:?set GRAFANA_PASSWORD in .env}
    volumes:
      - ./ops/grafana/provisioning:/etc/grafana/provisioning:ro
      - ./ops/grafana/dashboards:/var/lib/grafana/dashboards:ro
    ports: ["127.0.0.1:3000:3000"]
    restart: unless-stopped
  otel-collector:
    image: otel/opentelemetry-collector-contrib:0.111.0
    command: ["--config=/etc/otelcol-contrib/collector.yml"]
    volumes: ["./ops/otel/collector.yml:/etc/otelcol-contrib/collector.yml:ro"]
    ports: ["127.0.0.1:4317:4317", "127.0.0.1:4318:4318"]
    restart: unless-stopped
  jaeger:
    image: jaegertracing/all-in-one:1.62.0
    environment: {COLLECTOR_OTLP_ENABLED: "true"}
    ports: ["127.0.0.1:16686:16686"]
    restart: unless-stopped
```

- The scrape targets are compose service names. While the services still run as
  host processes, either run Prometheus on the host or point the targets at
  `host.docker.internal:91xx` (on Linux with `extra_hosts:
  ["host.docker.internal:host-gateway"]` on the prometheus service).
- A job whose service does not exist yet (agent, api, connector, ...) reports
  up = 0 and fires `PdmServiceDown` after a minute; comment the job out of
  prometheus.yml until the service lands.
- A compose health check for a Python service (OPS-08) can hit its /health:
  `["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:9102/health', timeout=3)"]`.
  /health answers 503 when the service passes a `health` callable to `serve()` that
  returns False.
- Firing alerts show on Prometheus's /alerts page; there is no Alertmanager.

## The R3.5 checks

- **p95 scoring latency under 2 s during a 10x replay.** Model health dashboard,
  set the time range to the replay: the stat "p95 scoring latency over the range"
  is the figure for the G6 latency report, and "Windows scored within 2 s" gives the
  share directly from the 2.0 bucket. `PdmScoringLatencyHigh` fires if the 5-minute
  p95 stays over 2 s for 5 minutes.
- **An incident's trace found by its ID.** See "Finding an incident's trace".

## Not verified here

The build sandbox has no Docker and no network to the image registries, and only
the OpenTelemetry API (no SDK, no exporter). So:

- Prometheus, Grafana, the collector and Jaeger were not run. The configuration
  files were parsed as YAML and JSON and checked against the metric catalogue, but
  not by `promtool`, Grafana's provisioning or `otelcol validate`; the PromQL was
  never evaluated by Prometheus, and the dashboards were never rendered.
- `tracing.configure` with the real SDK and exporter was not run; the test uses
  stand-in modules. Spans have not reached a collector or Jaeger, so the trace search
  by tag is described, not demonstrated.
- The metric names are not wired into the services yet (the lead does that), so no
  service exports them today, and the 10x latency figure does not exist yet.
- What was run: tests/test_metrics.py (exposition read back by a parser, concurrent
  increments, the HTTP endpoint on a free port, the dashboards and rules against the
  catalogue) and tests/test_logs.py (JSON lines, context, exceptions, the no-op and
  API-only tracing paths, trace context through headers).
