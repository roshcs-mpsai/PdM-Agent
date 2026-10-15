# db

`init/001_schema.sql` creates the PdM-Agent store in PostgreSQL. It is generated from the
schema declared in `lib/pdm_common/store.py`:

```bash
python -m pdm_common.store --ddl postgres > db/init/001_schema.sql
```

Do not edit the SQL by hand; `tests/test_store.py` fails when the file and the declaration differ.
The file is idempotent (every statement is `IF NOT EXISTS` or replaces itself), and the Postgres
container runs it once, when its volume is created.

## What it creates

| Tables | Kind | Write rule |
|---|---|---|
| telemetry, windows, scores, prognoses | hypertables when TimescaleDB is present | append-only by convention |
| quarantine, drift_alerts, device_events | tables | append-only by convention |
| incidents, work_orders | tables | rows change: incidents through `incident_events`, work orders by the connector |
| corpus_documents, corpus_passages | tables | loaded offline |
| incident_events, gate_log, status_log, tool_results, agent_traces, briefs, decisions, study_sessions, promotions | tables | **insert-only**: a trigger raises `<table> is insert-only` on UPDATE or DELETE |

Signals, features, per-feature errors, probabilities and agent messages are JSONB, so no table
names an asset channel (CFG-03). Without the TimescaleDB extension the same file creates plain
tables, which is what the tests use.

## Benchmark (T3 step 4)

`tools/db_benchmark.py` loads April 2020 (198,734 rows) into `telemetry` through
`Store.insert_many`, then joins 100 incidents to the 2 h of telemetry before each one,
averaging one signal out of the JSON. Criterion: both under a minute.

| Engine | Load | Join | One incident's evidence read (median) |
|---|---|---|---|
| SQLite 3 (file) | 3.7 s | 0.11 s | 6.7 ms |
| PostgreSQL 16, no TimescaleDB, pure-Python psycopg | 9.6 s | 0.05 s | 20 ms |

Both agree on the join (56,815 rows; 88 of the 100 incidents fall where telemetry exists).
These runs were made without the TimescaleDB extension; rerun against the compose image
(`docker compose up -d postgres`, then `python tools/db_benchmark.py --store "$PDM_STORE"`) to
record the TimescaleDB figure that closes the Week 5 decision.
