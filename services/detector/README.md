# Detector

R0 ships one detector: a z-score baseline (R0.4, DET-02) that turns each
window into a score and a status. R1b adds the common detector interface,
PCA and one deep model behind it, and SPOT thresholds.

## z-score baseline

```bash
# 1. fit once: per-input mean and std of the one-minute means over a
#    healthy candidate from events.yaml; writes models/<asset_id>/zscore.json
python services/detector/zscore.py --profile profiles/metropt3_apu/asset_profile.yaml \
  --fit --data "data/MetroPT3(AirCompressor).csv" --candidate H4

# 2. score the stream: windows in, scores and a retained status out
python services/detector/zscore.py --profile profiles/metropt3_apu/asset_profile.yaml
```

- **Fit data.** The candidate's days (H4 is Mar 21-23, clean in the EDA
  check), minus its `exclude` spans and any freeze mask, uncertain or grey
  period. The rows go through `pdm_common.windows.MinuteWindower`, the class
  the ingest stub publishes with, so the fit sees exactly what it scores.
  On the full file, H4 gives 4,245 windows from 25,688 rows.
- **Score.** The largest |z| across `detector.inputs`; the per-input |z|
  travels with it for attribution (DET-07).
- **Threshold.** A placeholder: `--threshold` at fit time, default 4.0, and
  in-sample percentiles are kept in the model file for reference (H4: p99.9
  is 3.82). SPOT replaces it in R1b. There is no persistence gate yet, so
  every window above the threshold turns the status amber.
- **Run ID.** `zscore-<12 hex>`, a hash of the model file's content, stands in
  for the MLflow run ID until R1b. The model file is not committed (models/
  is gitignored); refit after any change to the inputs.

Each window logs one line:

```text
detector score 2020-07-15T14:30:00Z 4.27 / 4.00 (top Oil_temperature) alert=True -> out_of_specification
```

## Payload schema -- `score_v1`

On the profile's `topics.scores`, one per window, not retained (DET-06).

| Field | Meaning |
|---|---|
| `schema` | `score_v1` |
| `asset`, `profile_sha256`, `events_sha256` | asset id and the config hashes in force (CFG-07) |
| `detector`, `detector_run` | `zscore` and the model's run ID |
| `window_start`, `window_end` | the window's bounds, from window_v1 |
| `score` | aggregate score: the largest per-input error |
| `errors` | `{input: |z|}` for every detector input |
| `threshold`, `alert` | the threshold in force and `score > threshold` |

## Payload schema -- `status_v1`

On the profile's `topics.status`, **retained**, so a dashboard or SCADA
reading it later gets the latest state at once.

| Field | Meaning |
|---|---|
| `schema` | `status_v1` |
| `asset`, `profile_sha256`, `detector_run` | as above |
| `ts` | stream time of the latest window (its end) |
| `ne107` | NE 107 state: `good` or, while an alert is active, `out_of_specification` |
| `score`, `threshold`, `alert` | from the latest score |

The NE 107 vocabulary and the dashboard colours live in
`pdm_common.status`. The gate service takes over this topic in R2b with the
full FRD section 8 mapping.
