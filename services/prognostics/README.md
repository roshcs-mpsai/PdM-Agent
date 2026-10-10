# Prognostics

P(failure within 24, 72 and 168 h), with a 90 % interval, every hour of stream
time (T13, T14; PRG-01 to PRG-06).

```bash
python services/prognostics/prognosis.py --profile profiles/metropt3_apu/asset_profile.yaml [--store "$PDM_STORE"]
```

Reads `topics.windows` and `topics.scores`; writes `topics.prognosis` (retained)
and the store's `prognoses` table.

- **Model.** The frozen survival bundle eval/config.yaml pins
  (`eval/frozen/<asset>/survival/bundle.json`): one logistic model per horizon,
  made monotone in the horizon, plus 200 refits on bootstrapped days whose 5th
  and 95th percentiles are the interval. `pdm_common.frozen.SurvivalModel` runs
  it with numpy; eval/prognostics.py uses the same class.
- **Inputs.** On every hour whose window (ending on the hour) is complete and
  scored: the maximum and mean detector score and the share of windows over the
  detector's threshold in the last 24 h, and the hour's window's 6-h loaded and
  stopped shares and oil-temperature rise. These are the rows of
  `eval/prognostics.py hourly_table`; tests/test_prognostics_service.py streams
  three weeks of 2020 through the service and gets those rows and their
  probabilities back.
- **Honesty about skill.** Every prognosis carries the model's
  leave-one-event-out Brier skill against a constant hazard and the PRD's bar
  (0.15). The frozen model is below it at every horizon (24 h -0.03, 72 h
  -0.08, 168 h -0.13), so `skill.meets_bar` is false and the dashboard shows
  the constant-hazard baseline beside the model.

## Payload schema -- `prognosis_v1`

On `topics.prognosis`, **retained**, one per stream hour.

| Field | Meaning |
|---|---|
| `schema`, `asset`, `ts` | `prognosis_v1`, asset id, the hour |
| `horizons` | `{"24": {"p", "lower", "upper"}, "72": ..., "168": ...}` |
| `interval` | the interval's coverage (0.9) |
| `inputs`, `windows_in_history`, `input_from`, `input_to` | the hourly row and the span it read |
| `attribution` | each input's contribution to the log-odds of P(72 h), largest first (PRG-06) |
| `constant_hazard` | the baseline's P(h) for each horizon |
| `skill` | `brier_skill_by_horizon`, `mean`, `bar` (0.15), `meets_bar` |
| `model`, `model_run_id`, `profile_sha256` | the frozen model and the versions in force |
