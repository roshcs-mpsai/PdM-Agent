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
profiles/metropt3_apu/   asset_profile.yaml + events.yaml — every asset-specific
                         fact lives here; shared code contains no MetroPT names
services/                one directory per component (replayer, ingest, detector,
                         prognostics, gate, mcp_server, agent, dashboard,
                         opcua_north, connector)
skills/                  shared agent skills + one folder per asset
checks/                  deterministic diagnostic checks (pure functions)
eval/                    leave-one-event-out harness, audits, study scripts
notebooks/               EDA (metropt3_eda.ipynb — runs end-to-end in Colab)
tests/                   unit, schema, negative and injection tests (CI-gated)
```

## Quickstart

```bash
pip install -r requirements.txt
pytest -q                          # profile/config invariants
python eval/evaluate.py            # harness Version Zero (prints fold plan)
docker compose up -d               # Mosquitto broker (walking skeleton)
jupyter lab notebooks/metropt3_eda.ipynb   # downloads the dataset on first run
```

## Working agreements

- `main` is protected; changes land by PR with green CI.
- Asset-specific names appear only under `profiles/`, `skills/<asset>/` and
  `checks/<asset>/` — anything else is a portability shortfall and gets reported.
- Event windows, healthy periods and label policy change only in
  `profiles/metropt3_apu/events.yaml`, never in code.
- The detector frozen at Week 7 (by MLflow run ID) is not touched afterwards.
