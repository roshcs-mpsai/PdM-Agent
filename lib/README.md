# pdm_common

The shared library every PdM-Agent service imports. It holds the code that
must behave identically in the batch harness and in the streaming services,
so what we evaluate is what we deploy.

```bash
pip install -e lib     # once per environment; CI does the same
```

| Module | What it gives you | Requirements |
|---|---|---|
| `profile` | `load_profile()` (validates against `schema`), `profile_hash()`, `signal_names()`, `events_file()` | CFG-01, CFG-07 |
| `schema` | `AssetProfile` (schema 0.2) and `validate_profile()`, which reports every problem as `file:line: key.path: message` | CFG-02, CFG-08 |
| `events` | `load_events()`, `events_hash()`, `event_span()`, `half_open()` | CFG-04 |
| `hashing` | `sha256_file()`, `canonical_json()` for payloads | CFG-07, NFR-05 |
| `timeutil` | `to_utc()`, `iso_utc()`, `parse_duration()` | ING-09 |
| `quality` | `freeze_runs()`, `frozen_rows()`, `to_grid()` (mask first, epoch-aligned UTC grid, bridge one or two interior points), `raw_gaps()`, `label_gaps()`, and `mask_grid_and_gaps()` with every parameter from the profile | ING-09, ING-10 |
| `zones` | `zone_scheme()`, `gap_edges()`, `zone_codes()`, `label_grid()`, `regime_of()`: one zone per grid point by the events.yaml precedence | CFG-05 |
| `windows` | `window_ends()`, `make_windows()`, `add_folds()`: 10-minute windows at a 1-minute stride, complete or dropped, with zone, regime, test blocks, look-back flags and the fit / calibrate / check role; `MinuteWindower` for the R0 z-score | ING-11 |
| `features` | `load_feature_spec()` and `window_features()`: the 42 features declared in the asset's features.yaml, computed by kind through channel roles | ING-12, DET-04 |
| `stream` | `StreamWindows`: the batch pipeline (freeze mask, grid, windows, features) on a rolling buffer, releasing each window once later data cannot change it; what ingest runs | ING-09 to ING-12 |
| `catalogue` | `add()`, `find()`: the data catalogue, one JSON line per processed dataset with its lineage | ING-13 |
| `mqtt` | `run_service()`: subscribe, handle, publish at QoS 1, flush on shutdown; `read_retained()` | — |
| `spot` | `fit_spot()`, `threshold()`, `calibrate_q()`: the SPOT threshold from the GPD tail of healthy scores, q chosen at the budget | DET-05 |
| `gate` | `gate_params()`, `gate_passes()`, `incidents()`, `gate()`, `lead_at_report()`: the persistence gate as pure functions; `StreamingGate`: the same, one window at a time | INC-01 to INC-04 |
| `incidents` | the incident lifecycle: `open_incident()`, `transition()` (attributed, compare-and-set), `claim()`, `replay_state()` | INC-05, INC-06 |
| `asof` | `telemetry_frame()`, `grid_asof()`, `window_asof()`, `evidence_asof()`: the store's data as of a moment, through the ingest pipeline | AGT-04 |
| `frozen` | `load_frozen()`, `DetectorModel`, `SurvivalModel`: the frozen bundles, verified against eval/config.yaml before anything scores or predicts with them | DET-08, NFR-05, PRG-02 |
| `store` | `Store`, `open_store()`: one schema for PostgreSQL/TimescaleDB and SQLite, insert-only audit tables enforced by triggers, rows as dicts with UTC ISO times; `python -m pdm_common.store --ddl postgres` writes db/init/001_schema.sql | FRD section 8, ESR-02, HIL-05 |
| `checks` | `Evidence` (what an alarm's checks may read: the grid before the opening, the opening window, the gaps), `CheckResult`, `load_registry()`: the shared side of the per-asset checks in checks/<asset>/ | AGT-05 |
| `status` | `ne107()`: the NE 107 status and its causes from score, gate, incident, diagnosis, prognosis, drift and data; `marker()` colours | INC-07, INT-03 |

Two rules:

- Nothing in here names an asset-specific signal; `tests/test_profiles.py`
  fails CI if one appears (CFG-03).
- Time conventions for `events.yaml` live in its header and are implemented
  once, in `events.py`. Read spans through `event_span()` / `half_open()`
  rather than re-deciding whether an end is inclusive.
