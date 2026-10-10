# PdM-Agent

Predictive maintenance with probabilistic time-to-event prognostics, a tool-enabled
diagnostic agent, and standards-based industrial integration. AI 894 capstone,
Penn State — Project 06, Group 6.

**Team:** Ashok Reddy Buthukuri · Gayatri Suresh Chaudhari · Rosh Chathoth Sreedharan
**Instructor:** Youakim Badr, Ph.D.

PdM-Agent watches streaming telemetry with a detector trained on healthy data,
estimates the probability of failure within 24/72/168 h, and — when an anomaly
persists — calls a diagnostic agent that drafts an evidence-cited incident brief
and work order for human approval. It never writes to equipment. Full requirements
live in the PRD (v0.2); the build sequence is in the construction plan.

## Dataset

MetroPT-3: the Air Production Unit compressor of a Metro do Porto train.
Feb–Aug 2020, 1,516,948 records at 0.1 Hz (10 s, jittered), 7 analog + 8 digital
channels, four documented air-leak failures.

- **Canonical source:** [UCI ML Repository, dataset 791](https://archive.ics.uci.edu/dataset/791/metropt+3+dataset) (CC BY 4.0) — this is what the EDA notebook downloads and what record counts are checked against.
- Reference: Veloso et al., *The MetroPT dataset for predictive maintenance*, Scientific Data 9, 764 (2022).
- A Kaggle mirror exists; we standardize on the UCI file.

The later 2022 campaign (MetroPT-2) is reserved untouched for the transfer test
and is not loaded before the Week 7 model freeze.

## Layout

```
profiles/metropt3_apu/   asset_profile.yaml, events.yaml, features.yaml: every asset-specific
                         fact lives here; shared code contains no MetroPT names
lib/pdm_common/          the shared library every service imports (lib/README.md)
services/                one folder per component, each with its README:
  replayer               deterministic replay of the dataset to MQTT
  ingest                 validation, quarantine, 10-minute windows and their features
  detector               the frozen detector's score per window
  prognostics            P(failure within 24/72/168 h) with intervals, every stream hour
  gate                   persistence gate, incidents, NE 107 status
  drift                  per-signal drift checks, candidate requests
  mcp_server             the agent's six read-only tools over MCP
  agent                  the bounded diagnostic loop that writes the brief
  api                    the HTTP API: reads, reviewer decisions, the agent's writes, the study
  dashboard              Streamlit pages over the API
  connector              approved work orders to the maintenance system's REST endpoint
  opcua_north            read-only OPC UA server for SCADA
  south                  Sparkplug B and OPC UA adapters, the simulated SCADA, the CIP reader
corpus/                  the knowledge corpus behind search_knowledge, with its source register
skills/                  shared agent skills and one folder per asset
checks/                  deterministic diagnostic checks, one folder per asset
schemas/                 JSON Schema for every message, tool result and brief
eval/                    leave-one-event-out harness, the freeze, the in-process pipeline, promotion
db/                      the PostgreSQL schema, generated from lib/pdm_common/store.py
mosquitto/               broker configs, the ACL, the secure-broker compose override
ops/                     Prometheus, Grafana, OpenTelemetry collector configuration
docker/                  the image every Python service runs
docs/                    RUNBOOK.md (running the stack), SECURITY.md (accounts, TLS, tokens, secrets)
tools/                   window builds and calibration, the freeze and the registry, broker accounts and
                         certificates, the secret scan, the work-order simulator, verify_local.sh
notebooks/               EDA (metropt3_eda.ipynb runs end to end in Colab)
tests/                   unit, contract, negative, injection and e2e tests (CI-gated)
```

## Quickstart: the full stack

docs/RUNBOOK.md has the details: first-time setup, Journey A step by step, where to look when
something fails, rotating secrets and backups.

```bash
cp .env.example .env                    # then fill it in (docs/RUNBOOK.md, first-time setup)
docker compose up -d --build --wait     # every service, healthy
docker compose run --rm replayer        # the F4 lead-up at 1000x, about three minutes
# the dashboard: http://localhost:8501, sign in with your API token
tools/verify_local.sh                   # the tests, the stack, a replay, an incident and its brief
```

Without an Anthropic API key, `PDM_AGENT_ARGS="--policy rules"` in `.env` runs the agent on the
triage skill's rules as code (eval/policy.py) instead of a model, and
`PDM_AGENT_ARGS="--scripted services/agent/scripts/dry_run.json"` on a fixed script. The secure broker (accounts, ACLs, TLS) is
`mosquitto/compose.secure.yaml` on top of `compose.yaml` (docs/RUNBOOK.md, docs/SECURITY.md).

## Quickstart: development

```bash
pip install -r requirements.txt
pip install -e lib                 # pdm_common, the shared library
pytest -q                          # tests that need the dataset or a broker skip themselves
python eval/evaluate.py            # harness Version Zero (prints fold plan)
jupyter lab notebooks/metropt3_eda.ipynb   # downloads the dataset on first run
```

## Run the stream on the host (replayer -> ingest -> detector)

A replayed record travels replayer -> ingest (10-minute windows and their
features) -> frozen detector (scores), with a log line per window at every
hop. One terminal per service, from the repository root, against the broker
alone (not beside the full stack, which would consume the same topics):

```bash
P=profiles/metropt3_apu/asset_profile.yaml
docker compose up -d mosquitto                               # broker on localhost:1883
python services/ingest/ingest.py --profile $P
python services/detector/detector.py --profile $P            # loads only the bundles eval/config.yaml pins
python services/replayer/replayer.py --profile $P \
  --data "data/MetroPT3(AirCompressor).csv" --speed 1000 --from 2020-07-14T00:00:00 --to 2020-07-15T20:00:00
```

Ctrl-C on ingest prints its reconciliation (in = accepted + quarantined,
windows = complete + dropped). The span above is the F4 lead-up (reported at
14:30 on Jul 15); in the evaluated scores the detector alerts continuously from
about 00:30 that day. The seven-day features need seven days of stream, so for
scores identical to the evaluation start the replay a week earlier. The same
chain runs as a test: `PDM_TEST_BROKER=localhost:1883 pytest -q -m e2e`.

## Start MLflow (the model registry)

```bash
docker compose up -d mlflow                      # http://localhost:5000
export MLFLOW_TRACKING_URI=http://localhost:5000
python tools/register_bundle.py data/processed_v4/detector_selection --dry-run   # what would be logged
python tools/register_bundle.py eval/frozen/metropt3_apu/detector \
    --name pdm-detector-metropt3_apu --alias production                          # prints the run ID
```

If the image will not start, `mlflow server --backend-store-uri sqlite:///mlruns.db` from the
virtual environment serves the same API on port 5000.

## The database

```bash
docker compose up -d postgres     # TimescaleDB 2.17 on PostgreSQL 16, schema from db/init
docker compose exec postgres psql -U pdm -d pdm
```

In the stack the services reach the store as `postgres:5432` on the internal core network, and
no port is published. To run host tools against it (`tools/db_benchmark.py --store "$PDM_STORE"`,
`PDM_TEST_PG=<url of a scratch database> pytest tests/test_store.py`), publish it on 127.0.0.1
with a `compose.override.yaml` (docs/RUNBOOK.md, development against the stack) and use
`PDM_STORE="postgresql://pdm:<password>@localhost:5432/pdm"`.

The schema is declared once, in `lib/pdm_common/store.py`; `db/init/001_schema.sql` is generated from
it (`python -m pdm_common.store --ddl postgres`) and a test fails if the two disagree. Without
`PDM_STORE`, everything uses SQLite at `runs/pdm.db`, which is what the tests and the single-process
pipeline run on. The init scripts run only when the volume is new; after a schema change, apply the
regenerated file with `docker compose exec -T postgres psql -U pdm -d pdm < db/init/001_schema.sql`
(new tables and indexes go in place; a changed column needs a fresh volume).

## Status

What is built, and how far it has been tried. The build sandbox has no Docker daemon, no broker,
no PostgreSQL and no model key, and lacks paho-mqtt, asyncua, streamlit, MLflow, LangGraph and the
OpenTelemetry SDK, so every service has run there in process only: on SQLite, on the in-memory bus
of eval/pipeline.py or against stand-ins. The last column is what still waits for a laptop; the
service READMEs say more. Tests marked (data) run only where `data/` holds the dataset.

| Part | Tests | Not run yet |
|---|---|---|
| replayer | test_replayer.py: determinism, faults, manifest | against a broker (tests/e2e) |
| ingest | test_ingest.py, test_stream.py; tools/stream_check.py gets back all 239,172 evaluated windows (data) | with a broker and PostgreSQL |
| detector | test_detector.py: Apr 1–21, 2020 through ingest gives the evaluated scores (data) | with a broker |
| prognostics | test_prognostics_service.py: three weeks of 2020 give the evaluated rows (data) | with a broker |
| gate | test_gate_service.py: the 34 incidents of the batch gate (data); test_pipeline.py: the F4 lead-up (data) | with a broker |
| drift | test_drift.py: handlers in process, injected shifts (data) | with a broker; Evidently itself |
| mcp_server | test_mcp_tools.py: every tool, refusals, as-of reads, MCP over streamable HTTP | in the stack |
| agent | test_agent.py: scripted models through the real runner, tool server and store | a live model; the LangGraph form |
| api | test_api.py: tokens, roles, decisions, the audit rebuild, on SQLite | PostgreSQL |
| dashboard | test_dashboard.py and test_status_view.py (services/dashboard/README.md) | served by Streamlit |
| connector | test_connector.py: against the simulator, retries, tamper checks | PostgreSQL; a real maintenance system |
| opcua_north | test_opcua_north.py: the read-only model; the asyncua binding against a stand-in | asyncua; a SCADA client |
| south | test_south.py, test_cip.py, test_workorder_sim.py, without a broker | paho-mqtt, asyncua, Node-RED, a CIP device |
| store | test_store.py on SQLite; `PDM_TEST_PG` runs it on PostgreSQL | PostgreSQL |
| broker security | test_broker_security.py (ACL evaluator, password hashes), test_secrets.py | a broker loading the secure config |
| observability | test_metrics.py, test_metrics_wiring.py, test_logs.py | Prometheus, Grafana, the collector, Jaeger; span export |
| corpus, skills, checks, schemas | test_corpus.py, test_skills.py, test_checks.py, test_schemas.py | |
| harness, freeze, promotion | test_harness.py, test_frozen.py, test_promote.py, test_register_bundle.py; CI runs eval/evaluate.py; docs/evidence/G1_G2.md from the frozen evaluation | an MLflow round trip |
| scripted incidents, journeys | test_scripted_incidents.py; test_journeys.py: Journeys A, B and C in one process, A and C on MetroPT-3 (data); the 20-incident library on the replayed campaign with the rules policy (docs/evidence/G3_scripted_incidents_rules.md) | the pinned model's run of the library and the manual groundedness review |
| deployment | test_compose.py; `docker compose config` on both compose files | building the image and running the stack: tools/verify_local.sh |

## Working agreements

- `main` is protected; changes land by PR with green CI.
- Asset-specific names appear only under `profiles/`, `skills/<asset>/` and
  `checks/<asset>/` — anything else is a portability shortfall and gets reported.
- Event windows, healthy periods and label policy change only in
  `profiles/metropt3_apu/events.yaml`, never in code.
- The detector frozen at Week 7 (by MLflow run ID) is not touched afterwards.
