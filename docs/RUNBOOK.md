# Runbook

How to run the PdM-Agent stack on one host: first-time setup, starting and stopping, Journey A
step by step, where to look when something fails, rotating secrets, backups, and what this build
has and has not been tested on (build plan T35, FRD section 12).

| File | What it is |
|---|---|
| `compose.yaml` | the stack: every service, its network, health check and restart policy |
| `mosquitto/compose.secure.yaml` | on top of `compose.yaml`: the secure broker and one broker account per service |
| `docker/Dockerfile`, `.dockerignore` | the one image every Python service runs, with the dataset and secrets kept out |
| `tools/verify_local.sh` | one command that checks a laptop's stack end to end |
| `tests/test_compose.py` | keeps the compose files, the image files and CI consistent, without Docker |

## The stack

| Service | What it does | Networks | On the host |
|---|---|---|---|
| mosquitto | MQTT broker | core, south | 127.0.0.1:1883 (secure broker: 8883, TLS) |
| postgres | the store, TimescaleDB on PostgreSQL 16 | core | nothing |
| mlflow | the model registry | core, ops | 127.0.0.1:5000 |
| replayer | one-shot replay of the dataset (profile `replay`) | core | nothing |
| ingest, detector, gate, prognostics, drift | the stream | core | nothing; /metrics on 9101 to 9110 inside |
| mcp_server | the agent's six tools over MCP, port 8765 | core | nothing |
| agent | diagnoses each incident, writes a brief | core, egress | nothing |
| api | the HTTP API, every request with a token | core, north | 127.0.0.1:8000 |
| dashboard | Streamlit pages over the API | north | 127.0.0.1:8501 |
| opcua_north | read-only OPC UA server for SCADA | core, north | 127.0.0.1:4840 |
| connector | sends approved work orders | core, north | nothing |
| south | Sparkplug B adapter, plant to telemetry | south, core | nothing |
| scada-sim | the simulated plant (profile `scada`) | south | nothing |
| workorder-sim | the simulated maintenance system | north | 127.0.0.1:8088 |
| prometheus, grafana | metrics, dashboards, alerts | core, ops | 127.0.0.1:9090, 127.0.0.1:3000 |
| otel-collector, jaeger | traces | core, ops | 127.0.0.1:4317 and 4318 (OTLP), 127.0.0.1:16686 (UI) |

The network rules (FRD section 12, NFR-06):

- **core** is internal: nothing on it has a route off the host, and Docker publishes no port of a
  container that sits on internal networks only.
- **egress** holds the agent alone. It is the one network with a route out, to the language-model API.
- **south** holds the broker, the south adapter and the simulated plant. No other service joins it.
- **north** holds the operator- and SCADA-facing endpoints. The api, the dashboard (through the api)
  and the work-order endpoint take tokens; opcua_north runs the anonymous demo endpoint (see known
  limits), which is why its port stays on 127.0.0.1.
- **ops** is not in the FRD table. It exists so that MLflow, Prometheus, Grafana, Jaeger and the
  collector can publish their ports on 127.0.0.1 (build guide R3.3).
- south, north and ops have IP masquerading off, so a container on them cannot open a connection
  off the host either. Published ports still work. On a Linux host that holds; Docker Desktop's VM
  networking may forward such traffic anyway, so check with `tools/verify_local.sh --check-networks`.

`PDM_NORTH_BIND=0.0.0.0` in `.env` opens the api, the dashboard and the work-order simulator to the
LAN. Everything else stays on 127.0.0.1.

## First-time setup

You need Docker Engine with Compose 2.24.4 or later (`docker compose version`), a Python 3.10 or
3.11 virtual environment with `pip install -r requirements.txt` and `pip install -e lib` (for the
tests and the tools), openssl, the dataset, and several GB of disk for the images.

### 1. `.env`

```bash
cp .env.example .env
chmod 600 .env
python -c "import secrets; print(secrets.token_urlsafe(32))"     # one fresh value per token
```

| Variable | Read by | Notes |
|---|---|---|
| `PG_PASSWORD` | postgres, every service with the store | URL-safe characters only, since it goes into `PDM_STORE`. Used when the database volume is created. |
| `GRAFANA_PASSWORD` | grafana | the `admin` login |
| `PDM_TOKEN_AGENT` | agent, api, mcp_server | required |
| `PDM_TOKEN_CONNECTOR` | connector, workorder-sim | required; the simulator accepts this token |
| `PDM_TOKEN_REVIEWER_ROSH`, `_ASHOK`, `_GAYATRI` | api | each reviewer signs in with their own; every decision records the name |
| `PDM_TOKEN_REVIEWER`, `PDM_TOKEN_ENGINEER`, `PDM_TOKEN_OPERATOR`, `PDM_TOKEN_EVALUATOR` | api | optional role tokens |
| `PDM_STUDY_SALT` | api | 16 characters or more; the study endpoints (T34) answer 503 without it |
| `ANTHROPIC_API_KEY` | agent | the model; without it, set `PDM_AGENT_ARGS` |
| `PDM_AGENT_ARGS` | agent | `"--policy rules"` runs the triage rules as code (eval/policy.py), no model; `"--scripted services/agent/scripts/dry_run.json"` (quoted) runs a fixed script |
| `PDM_NORTH_BIND` | api, dashboard, workorder-sim | `0.0.0.0` opens them to the LAN |
| `PDM_LOG_LEVEL` | every Python service | `INFO` by default; `DEBUG` for more |
| `COMPOSE_FILE` | docker compose | the secure broker, step 2 |

Every token is at least 32 characters and different from the others, or the api will not start
(docs/SECURITY.md). The api sees only the token variables listed under `x-api-tokens` in
`compose.yaml`; a token for another person needs a line there. `docker compose` stops at once,
naming each variable, when a required one is missing.

### 2. Broker accounts and certificates (the secure broker)

Without this step the stack runs the R0 development broker: anonymous, 127.0.0.1:1883 only. The e2e
tests and services run on the host use that broker too. The demo host should run the secure one:

```bash
python tools/broker_users.py            # a password per account into .env, the hashes into mosquitto/config/passwd
tools/make_certs.sh                     # local CA and broker certificate in mosquitto/certs/
python tools/broker_users.py --check
echo 'COMPOSE_FILE=compose.yaml:mosquitto/compose.secure.yaml' >> .env
```

From then on every `docker compose` command in this folder uses both files: the broker loads
`mosquitto.secure.conf`, publishes 8883 (TLS) instead of 1883, and each service logs in with its
own account. Inside the stack the services still use 1883, with accounts and ACLs applied. A host
program connects with TLS: `PDM_MQTT_HOST=localhost PDM_MQTT_TLS_CA=mosquitto/certs/ca.crt` and its
account (docs/SECURITY.md). To go back, remove the `COMPOSE_FILE` line.

### 3. The dataset

`notebooks/metropt3_eda.ipynb` downloads MetroPT-3 from the UCI archive on its first run and leaves
`data/MetroPT3(AirCompressor).csv` (about 218 MB). The image never contains it: the replayer and the
simulated plant mount `data/` read-only and run as uid 10001, so the file must be readable by others
(`chmod 644`, the default).

### 4. Build and start

```bash
docker compose build                    # the first build installs the requirements: several minutes
docker compose up -d --wait             # waits until every health check passes
tools/verify_local.sh --no-replay       # health and scrapes; without --no-replay also the F4 replay and its brief
```

For NFR-05, pin what was built on the demo host: `docker compose images` lists the images; write
each pulled image's digest into `compose.yaml` (`image: name:tag@sha256:...`, from
`docker image inspect --format '{{index .RepoDigests 0}}' <image>`), build with the base image pinned
the same way (docker/Dockerfile), and keep the installed versions:
`mkdir -p runs && docker compose run --rm --no-deps -T --entrypoint pip ingest freeze > runs/pip-freeze.txt`.

## Starting and stopping

```bash
docker compose up -d --wait             # start, or apply a change to .env or compose.yaml
docker compose ps                       # state and health of each service
docker compose logs -f gate agent       # follow logs
docker compose restart agent            # one service
docker compose stop                     # stop; the containers stay
docker compose down                     # remove the containers and networks; the volumes stay
docker compose down -v                  # also delete the volumes: the store, the registry, runs
```

| What | Where |
|---|---|
| Dashboard | http://localhost:8501, sign in with your API token |
| API | http://localhost:8000, `Authorization: Bearer <token>` |
| Grafana | http://localhost:3000, `admin` and `GRAFANA_PASSWORD` |
| Prometheus | http://localhost:9090 (targets, alerts) |
| Jaeger | http://localhost:16686 |
| MLflow | http://localhost:5000 |
| OPC UA | opc.tcp://localhost:4840/pdm/ |
| Work-order simulator | http://localhost:8088/api/work-orders, with `PDM_TOKEN_CONNECTOR` |

The one-shot jobs run with `docker compose run --rm`: `replayer` (profile `replay`) and `scada-sim`
(profile `scada`). Feed ingest from one source at a time. Before a second scada-sim run over the
same period, `docker compose restart south` (its sequence numbers carry on otherwise, and ingest
would read the second pass as stale).

## Journey A, step by step

Replay, incident, brief, approval in the dashboard, work order at the simulator, NE 107 in SCADA
over OPC UA. `tools/verify_local.sh --journey-a` runs the same steps, approving through the API.

0. **A clean stack.** `docker compose down -v && docker compose up -d --wait`. This deletes the
   store. Incident ids are deterministic, so a second replay of the same span finds its incident
   already there and opens no new one.
1. **Replay the F4 lead-up.**

   ```bash
   docker compose run --rm replayer --speed 1000 --from 2020-07-14T00:00:00 --to 2020-07-15T20:00:00
   ```

   About three minutes. These are the replayer service's default arguments, so
   `docker compose run --rm replayer` alone does the same; arguments given on the command line
   replace all three (the replayer's own default speed is 1, real time). `--speed 10` is the demo
   pace, about 4.4 hours for this span. In the evaluated scores the detector alerts continuously
   from about 00:30 on Jul 15. For every window's seven-day look-back, start a week earlier:
   `--speed 1000 --from 2020-07-07T00:00:00 --to 2020-07-16T06:00:00` is the span on which
   tests/test_pipeline.py gets incidents at Jul 14 02:33 and Jul 15 00:46.
2. **The incident.** `docker compose logs -f gate` shows it open. The dashboard's Assets page shows
   the asset at maintenance required, or failure (step 6), with the incident. From a shell:

   ```bash
   TOKEN="$(grep '^PDM_TOKEN_OPERATOR=' .env | cut -d= -f2-)"
   curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/incidents | python -m json.tool
   ```

3. **The brief.** The agent claims the incident within seconds (`docker compose logs -f agent`). With
   the model a run takes up to 180 s, its budget. The dry run writes an insufficient brief that says
   no model read the evidence. The Review page shows the brief, each cited result, and a banner on an
   insufficient or budget-exhausted brief.
4. **Approval in the dashboard.** Open http://localhost:8501, sign in with your reviewer token
   (`PDM_TOKEN_REVIEWER_<NAME>`), open the incident on the Review page, and approve, edit or reject it
   with a reason. The decision records your name; the Audit page rebuilds it. The API call is
   `POST /briefs/<brief_id>/decisions` with `{"action": "approve", "reason": "..."}`.
5. **The work order at the simulator.** The connector sends it within about five seconds
   (`docker compose logs connector workorder-sim`). `GET /work-orders` on the API shows it sent, with
   the simulator's reference, and the incident moves to sent. The simulator lists what it received:

   ```bash
   CONNECTOR="$(grep '^PDM_TOKEN_CONNECTOR=' .env | cut -d= -f2-)"
   curl -s -H "Authorization: Bearer $CONNECTOR" http://localhost:8088/api/work-orders | python -m json.tool
   ```

6. **NE 107 in SCADA over OPC UA.** Connect an OPC UA client (UaExpert, or Node-RED with
   node-red-contrib-opcua) to `opc.tcp://localhost:4840/pdm/` with security None and an anonymous
   session. Look up the namespace `urn:pdm-agent`, browse Objects, PdM, the asset, and read `NE107`,
   `NE107Code`, `IncidentId` and `IncidentState`. While the incident is open and data flows, NE107
   reads `maintenance_required`, and `failure` while the windows also show the continuous-load
   signature. In the full-campaign replay (eval/pipeline.py) that held from Jul 14 23:36 to Jul 15
   18:28 stream time, most of this span, so the value you read depends on when you read it. Five
   minutes of wall time after the last window (`ne107.no_data_after` in the profile) it reads
   `function_check`, the no-data state: read it soon after the replay, or keep one running.

## Where to look when something fails

1. `docker compose ps -a`: which service is unhealthy, restarting or exited, and its exit code.
2. `docker compose logs --tail 100 <service>`: one JSON object per line. Lines written while a
   service works on an incident carry its id: `docker compose logs | grep INC-<asset>-<time>`.
3. Prometheus, http://localhost:9090/targets: every job up, `south` included. /alerts shows the
   firing rules. Grafana has the service-health and model-health dashboards.
4. Jaeger, http://localhost:16686: service `agent` or `mcp_server`, tag `incident_id=<id>`
   (ops/README.md). The two export spans when the OpenTelemetry SDK is installed (requirements.txt
   lists it); without it each logs `tracing off` once and sends none.
5. The store:

   ```bash
   docker compose exec postgres psql -U pdm -d pdm
   ```

   ```sql
   SELECT incident_id, state, opened_at FROM incidents ORDER BY opened_at DESC LIMIT 5;
   SELECT incident_id, from_state, to_state, actor, at FROM incident_events ORDER BY id DESC LIMIT 10;
   SELECT brief_id, incident_id, evidence_state, valid, arm FROM briefs ORDER BY created_at DESC LIMIT 5;
   SELECT wo_id, incident_id, status, attempts, last_error, reference FROM work_orders;
   SELECT error_code, count(*) FROM quarantine GROUP BY error_code;
   ```

6. Files the services write are in the `runs` volume: `docker compose exec ingest ls -R runs`, or
   `docker compose cp ingest:/app/runs ./runs-from-stack`. Ingest's reconciliation is printed to its
   log when it stops.

| What you see | Likely cause | What to do |
|---|---|---|
| `required variable ... is missing a value` | `.env` lacks it | set it (first-time setup, step 1) |
| agent restarting; its log says to set the key or use `--scripted` or `--policy rules` | no `ANTHROPIC_API_KEY` | set the key, or `PDM_AGENT_ARGS` for a run without a model |
| api exits: a token too short, or the same token twice | the token rules | fix `.env`, `docker compose up -d` |
| detector or prognostics refuses to start: the freeze does not match | the profile, events, features or a bundle changed, often line endings from a Windows checkout | `git status`; check out again with LF line endings |
| a service logs `broker refused the connection: Not authorized` | secure broker, and its password is missing or `passwd` is stale | `python tools/broker_users.py --check`, then `docker compose restart mosquitto` |
| mosquitto will not start: a bind source does not exist | secure broker without `passwd` or `mosquitto/certs` | step 2 of first-time setup |
| replayer: no such file under `data/` | the dataset is missing | step 3 |
| no incident after the replay | too little look-back, or the detector refused windows | `docker compose logs detector gate`; replay from 2020-07-07 (Journey A, step 1) |
| work orders blocked | no approval, a changed text, or the endpoint refused | the connector's log names the reason; `GET /work-orders` has `last_error` (services/connector/README.md) |
| `south` unhealthy | the adapter holds no broker connection | `docker compose logs south` |
| port is already allocated | another program on that port | stop it, or change the host port in `compose.yaml` |
| tables missing in the store | `db/init` runs only on a new volume | `docker compose exec -T postgres psql -U pdm -d pdm < db/init/001_schema.sql` |

## Rotating secrets

docs/SECURITY.md, "Rotation", has the steps for each secret. In this stack, after a change to `.env`,
`docker compose up -d` recreates every container whose environment changed. A new broker password
also needs `docker compose restart mosquitto`, which reads `passwd` only when it starts. `PG_PASSWORD`
is changed inside the database first (docs/SECURITY.md). Reviewers sign in again with their new token.

## Backing up the store

Outside the repository, so that no dump is ever committed:

```bash
BACKUP=~/pdm-backup && mkdir -p "$BACKUP"
docker compose exec -T postgres pg_dump -U pdm -d pdm -Fc > "$BACKUP/pdm-$(date -u +%Y%m%dT%H%MZ).dump"
docker compose cp mlflow:/mlflow "$BACKUP/mlflow"         # the registry
docker compose cp ingest:/app/runs "$BACKUP/runs"         # quarantine, reconciliation, candidate requests
```

To restore into the running database: the tables telemetry, windows, scores and prognoses are
TimescaleDB hypertables, hence the two calls around `pg_restore`.

```bash
docker compose stop                     # nothing may write during the restore
docker compose start postgres
docker compose exec -T postgres psql -U pdm -d pdm -c "SELECT timescaledb_pre_restore();"
docker compose exec -T postgres pg_restore -U pdm -d pdm --clean --if-exists < "$BACKUP/pdm-<time>.dump"
docker compose exec -T postgres psql -U pdm -d pdm -c "SELECT timescaledb_post_restore();"
docker compose up -d --wait
```

Not backed up: Prometheus's data (its own volume, 15 days), Jaeger's traces (memory only; the
agent_traces table is the record), and the work-order simulator's orders. The restore has not been
tried yet.

## Development against the stack

- The services can still run on the host against the R0 broker (README.md, "Run the stream"):
  `docker compose up -d mosquitto` publishes it on 127.0.0.1:1883. Do not run host services beside
  the full stack: both would consume the same topics and write the same store.
- The e2e tests need the R0 broker alone: `docker compose up -d mosquitto`, then
  `PDM_TEST_BROKER=localhost:1883 pytest -q -m e2e`.
- postgres publishes no port. To reach it from the host, add a `compose.override.yaml` (and, with
  the secure broker, append it to `COMPOSE_FILE`):

  ```yaml
  services:
    postgres:
      networks: [core, ops]
      ports: ["127.0.0.1:5432:5432"]
  ```

  then `PDM_STORE="postgresql://pdm:<password>@localhost:5432/pdm"`, as in README.md.
- The secure OPC UA endpoint (Basic256Sha256 SignAndEncrypt and one username,
  services/opcua_north/README.md) needs an RSA certificate and key, which no repository tool makes
  yet. With them in a folder git ignores (add `opcua-certs/` to .gitignore first), a
  `compose.override.yaml` would switch it on:

  ```yaml
  services:
    opcua_north:
      command: [python, services/opcua_north/server.py, --profile, profiles/metropt3_apu/asset_profile.yaml,
                --cert, /certs/server_cert.der, --key, /certs/server_key.pem]
      environment:
        PDM_OPCUA_USER: ${PDM_OPCUA_USER:?set PDM_OPCUA_USER in .env}
        PDM_OPCUA_PASSWORD: ${PDM_OPCUA_PASSWORD:?set PDM_OPCUA_PASSWORD in .env}
      volumes:
        - {type: bind, source: ./opcua-certs, target: /certs, read_only: true}
  ```

## Known limits of this build

Where it was tested. The build sandbox has Python, the test suite, the docker CLI and Compose (no
daemon), and no access to PyPI or to image registries. There:

- `tests/test_compose.py` checked `compose.yaml`, `mosquitto/compose.secure.yaml`, the Dockerfile,
  `.dockerignore` and CI, ran `python <file> --help` for every service command, and ran
  `docker compose config -q` on both compose files (Compose accepted them, `!override` included).
- The API steps of `tools/verify_local.sh` (incident, brief, approval, the order at the simulator, the
  Prometheus targets) ran against the real api, work-order simulator and connector on SQLite, and
  against a stand-in for Prometheus's targets endpoint. Its OPC UA read and network checks did not run.
- The services' own tests ran in process: on SQLite, against fake paho clients or the in-memory bus
  of eval/pipeline.py, and against starlette's test client.

What needs a laptop, because it was never run in the sandbox:

| Piece | Not run there |
|---|---|
| Docker | the image was never built and the stack never started; every health check, `depends_on` and network rule is untested at run time, including masquerading off on south, north and ops, and the published ports |
| Third-party images | the health checks of prometheus and grafana (busybox `wget`) and jaeger (its `status` command) were written from their documentation; the collector's image has no shell, so it has no container health check, and verify_local.sh asks its :13133 instead |
| paho-mqtt | no service has talked to a broker in the sandbox; mosquitto.secure.conf, the ACL and `passwd` have not been loaded by Mosquitto |
| asyncua | ran only in tests (asyncua 2.1.0): opcua_north's server, and the simulated plant's server read by the south client; the services' `main()`, the secure endpoint and the SCADA read in verify_local.sh are untested |
| streamlit, plotly | the dashboard has not been served by Streamlit in the sandbox |
| MLflow | no bundle has been registered; the server image has not run |
| LangGraph | `services/agent/graph.py` is untested |
| An Anthropic API key | no live model run; the agent ran only scripted models |
| PostgreSQL, TimescaleDB | the tests ran on SQLite; `db/init/001_schema.sql` has not been applied by the image; backup and restore have not been tried |
| OpenTelemetry SDK | not installed in the sandbox; no span has reached the collector |

Gaps known now:

- opcua_north runs the anonymous demo endpoint: north is not authenticated end to end until the
  secure endpoint has a certificate (above).
- The images are pinned to tags, not digests (NFR-05): pin them on the demo host.
- north has no route off the host, so the connector reaches only an endpoint on this host (the
  simulator). A maintenance system elsewhere needs north with masquerading on (drop its
  `driver_opts` in `compose.yaml`), at the cost of a route out for every service on north.
- The `south` adapter and the replayer must not feed ingest at the same time; nothing enforces it.
- No service reports a model promotion. NE 107's function-check state has an input for one
  (lib/pdm_common/status.py), but eval/promote.py only moves an MLflow alias, and the detector and
  prognostics serve the bundle eval/config.yaml pins until they restart, so there is no promotion
  in progress to report.
- The agent checks its token budget between model calls, so a run can go over 50,000 tokens by
  the one call that crossed it (at most 8,000 output tokens plus that call's input). The wall-time
  budget also bounds each model call.
- A run that stops without finishing (a crash, or writes to the store that failed) keeps its
  incident in diagnosing until the agent's first tick after `outage.stale_after_s` (600 s): the
  agent then resumes it from its checkpoint, or hands the incident back to open.
