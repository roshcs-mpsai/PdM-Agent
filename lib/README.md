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
| `mqtt` | `run_service()`: subscribe, handle, publish at QoS 1, flush on shutdown; `read_retained()` | — |
| `status` | NE 107 states, `r0_status()`, dashboard `marker()` colours | INC-07 stub |

Two rules:

- Nothing in here names an asset-specific signal; `tests/test_profiles.py`
  fails CI if one appears (CFG-03).
- Time conventions for `events.yaml` live in its header and are implemented
  once, in `events.py`. Read spans through `event_span()` / `half_open()`
  rather than re-deciding whether an end is inclusive.
