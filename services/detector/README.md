# Detector

Scores every window with the frozen detector: PCA reconstruction error at the
SPOT threshold, the bundle eval/config.yaml pins (the Week-7 freeze, T30).

```bash
python services/detector/detector.py --profile profiles/metropt3_apu/asset_profile.yaml \
    [--store "$PDM_STORE"] [--run-id <the frozen run, to refuse anything else>]
```

- **Model.** `eval/frozen/<asset>/detector/bundle.json`: the 21 input features,
  the scaler (healthy median for missing values, mean and standard deviation),
  the PCA subspace and the threshold. Scoring needs numpy only
  (`pdm_common.frozen.DetectorModel`), so the service scores exactly as the
  harness did: tests/test_detector.py streams Apr 1-21, 2020 through ingest and
  this service and gets the evaluated scores back.
- **Refusals (DET-08).** It will not start if the bundle, the profile's
  detector sections, events.yaml or features.yaml differ from the freeze, or if
  `--run-id` names another run. It refuses a window whose `features_sha256` is
  not the frozen one, and counts it under `refused_by`.
- **Missing values** take the healthy median, as in evaluation; the score lists
  them under `imputed`.
- **Run ID.** The MLflow run ID once the bundle is registered
  (tools/register_bundle.py), else `bundle-<first 12 hex of its SHA-256>`.

The R0 z-score service is gone: it scored one-minute means, which ingest no
longer publishes. The z-score detector stays in the harness as the baseline
(eval/detectors.py, DET-02).

## Payload schema -- `score_v1`

On the profile's `topics.scores`, one per window, not retained (DET-06), and
one row in the store's `scores` table.

| Field | Meaning |
|---|---|
| `schema` | `score_v1` |
| `asset`, `profile_sha256`, `features_sha256` | asset id and the config hashes in force (CFG-07) |
| `detector`, `detector_run_id` | `PCA` and the frozen run |
| `window_start`, `window_end` | the window's bounds, from window_v1 |
| `score` | sum of the per-feature squared reconstruction errors (standardised) |
| `errors` | `{feature: squared error}` for the 21 inputs (DET-07) |
| `top` | the five largest terms as `[feature, share of the score]` |
| `imputed` | features that were missing and took the healthy median |
| `threshold`, `alert` | the frozen threshold and `score > threshold` |

Alerts become incidents only through the persistence gate (services/gate).
