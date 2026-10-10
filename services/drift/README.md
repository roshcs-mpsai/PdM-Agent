# Drift

Per-signal drift checks against the healthy reference every 6 hours of stream
time (T25; OPS-04), and the first step of gated retraining: a drift alert
becomes a candidate request, never a production model (OPS-06, OPS-07, PR-23).

```bash
python tools/drift_reference.py --processed data/processed_v4        # the reference; committed, rebuild after data or regime changes
python services/drift/drift.py --profile profiles/metropt3_apu/asset_profile.yaml [--store "$PDM_STORE"]
```

Reads `topics.telemetry` (telemetry_json_v1); writes `pdm/_sys/drift`
(drift_v1, **retained**), one `drift_alerts` row per signal and check, and a
candidate request in `runs/<asset>/candidates/requests/` when an alert starts.
The gate already reads `pdm/_sys/drift`: while `alert` is true its NE 107
status is out of specification.

## The check

- **Statistic.** The first Wasserstein distance between the window's values
  and the healthy reference, divided by the reference's standard deviation
  (floored at 0.001): `scipy.stats.wasserstein_distance(values, reference) /
  max(std, 0.001)`, an alert when it reaches the threshold. This is the number
  Evidently's `wasserstein` stattest ("Wasserstein distance (normed)")
  reports. Evidently is not installed here and cannot be (no PyPI access), so
  the PRD's Evidently is replaced by the same distance computed directly
  (`drift_distance` in drift.py). The reference is held as 200 quantiles at
  (k + 0.5) / 200, each standing for half a percent of the healthy values; on
  24 days sampled across the three regimes, the distance against them was
  within 0.005 standard deviations of the distance against every healthy value.
- **Cadence.** Boundaries every `drift.every` (6 h) aligned to the epoch:
  00:00, 06:00, 12:00 and 18:00 UTC. The check for a boundary runs when the
  first record at or after it arrives and covers `[boundary - drift.window,
  boundary)` (24 h). After a recording gap, boundaries whose window still
  holds data are checked when data resumes, stamped with their boundary;
  boundaries whose window holds none are skipped (`checks_without_data`).
- **Regimes.** The reference is per regime (events.yaml, half-open). A check
  uses the regime of its window end (`pdm_common.zones.regime_of`) and only
  that regime's samples, so February's light duty and the summer after the
  Jun 8 repair are judged against their own healthy data, and a window that
  straddles a regime change is judged on the new regime's samples alone. The
  reference keeps the regimes it was built on; the service refuses an
  events.yaml whose regimes differ. A window end outside every calibrated
  regime gets no check (logged, counted as `checks_without_reference`). The
  channel that went flat after Jun 8 is not in `drift.signals`.
- **The stream is cut like the reference.** Healthy means the negative zone,
  so the reference holds no gap edges: the first 15 min after a recording gap
  of 30 min or more (events.yaml `gap_edges`), the refill after a restart. The
  check leaves the same records out (the rule is copied into the reference
  and must match events.yaml). Without this, restarts alone read as drift: on
  Jul 31, a healthy stretch, the 12:00 window held 269 gap-edge points with the
  panel pressure down to 2.6 bar and reached 0.287 against a threshold of
  0.280. Also left out: a reading outside its profile range (that value only,
  as ingest keeps it off the grid) and logger freezes, found with the profile's
  `freeze_rule` through `pdm_common.quality` as ingest finds them (the buffer
  holds 30 min before the window, so a freeze running into it is found).
  Records ingest would quarantine (wrong schema or asset, bad seq or
  timestamp, a missing or non-numeric signal) and records out of time order
  are ignored; `seq` 0 after other records is a new replay: a new stream with
  no alert.
- **Minimum data.** A signal needs `min_samples` = 4320 usable samples in the
  window (half of 24 h at 10 s). With fewer it gets no verdict: `distance`
  null, `verdict` `no_verdict` and a `note` saying how many there were.
- **Alert state.** A signal's alert holds until a check with enough data
  comes in under its threshold; a check without a verdict neither raises nor
  clears it. `alert` at the top is true while any signal's is.

## Reference and thresholds

`eval/frozen/metropt3_apu/drift_reference.json` (27 KB), from
`tools/drift_reference.py` on data/processed_v4. Healthy means grid points in
the negative zone with an observed value. For each signal and regime it holds
the 200 quantiles, the count and standard deviation, the threshold, and the
figures behind it; `sources` has the SHA-256 of grid_10s.csv.gz, the
manifest, the profile and events.yaml. `--check` rebuilds it and compares
(tests/test_drift.py runs that when the data is present).

**Threshold rule.** The service's own check, run over the healthy data: at
every 6-h boundary, the last 24 h of healthy points of the boundary's regime,
kept when there are at least 4320. The threshold is 1.25 times the largest
distance such a window reaches (`max_distance`), so no healthy window of 2020
alerts and the 25 % margin is for healthy days the reference did not contain.
The 99th percentile of non-overlapping daily blocks was the first candidate
and was dropped: a regime has 28 to 63 such blocks, so their 99th percentile
is about their maximum, and between 2 and 9 of the overlapping 6-hourly
healthy windows per signal and regime go past it (`windows_over_p99_daily`),
each a false alert. Both figures stay in the file.

Thresholds, in healthy standard deviations and (in brackets) in signal units,
which is the smallest pure shift that alerts once it fills the window:

| Regime (check windows) | TP2 | TP3 | H1 | Oil_temperature | Motor_current |
|---|---|---|---|---|---|
| light_duty (112) | 0.103 (0.23 bar) | 0.129 (0.07 bar) | 0.110 (0.24 bar) | 1.374 (4.56 °C) | 0.216 (0.42 A) |
| normal (232) | 0.393 (1.09 bar) | 0.435 (0.26 bar) | 0.530 (1.48 bar) | 1.637 (9.46 °C) | 0.731 (1.59 A) |
| dv_pressure_flat (253) | 0.275 (0.85 bar) | 0.280 (0.16 bar) | 0.263 (0.80 bar) | 1.156 (4.75 °C) | 0.576 (1.29 A) |

How to read them:

- TP2, H1 and Motor_current are mostly two-state (near zero, or loaded), so
  their distance mostly measures a change in the share of time loaded: moving
  a tenth of the time between 0 and about 9 bar is about 0.9 bar.
- The normal regime's thresholds are the widest: its largest healthy
  distances come from windows beside the uncertain run of Mar 6 and the
  expert-reported episodes of May 19, which are labelled healthy.
- Oil temperature is a loose check: its healthy distribution moves with the
  season inside a regime, so only a shift of 4.5 to 9.5 °C alerts.

## On the 2020 data

From tests/test_drift.py (data tests) and one replay of the whole campaign,
every observed grid point as recorded, all zones included:

- **Known shifts.** Feb 3-23 (light duty), Jun 29 - Jul 5 and Jul 27 - Aug 9
  (after the Jun 8 repair) raise no alert.
- **Injected shift.** +0.5 bar on the panel pressure (TP3) from 03:00 on the
  third day of a healthy stretch is flagged within 24 h, naming that signal
  alone: after 9 h in light duty (Feb 12), 15 h in normal (May 6) and 9 h after
  Jun 8 (Jul 1). Over shift starts at 00:00, 03:00 and 09:00 on five stretches
  the delay was 3 to 18 h.
- **Whole campaign** (Feb 1 - Aug 31, 840 checks, 54 s): 82 checks alert, in
  13 episodes, every one inside a labelled anomalous period: the four March
  uncertain runs (Mar 6, 11-12, 27, 28-29), F1, F2, F3 and F4, and the
  expert-reported failures #1 (Apr 12), #6 (May 13), #9-10 (May 19-20), #19
  (Jul 8) and #21 (Jul 17). The short expert entries (20 to 60 min) do not
  move a 24 h window enough. No healthy stretch alerts. These episodes wrote
  22 candidate requests (see below). All of this is in-sample.

## Payload schema -- `drift_v1`

On `pdm/_sys/drift`, **retained**, one per check (schemas/drift_v1.json).

| Field | Meaning |
|---|---|
| `schema`, `asset`, `ts` | `drift_v1`, asset id, the boundary (window end) |
| `window` | the window length from the profile (`24h`) |
| `alert` | true while any signal's alert is on |
| `signals[]` | per signal: `signal`, `regime`, `distance` (null without a verdict), `threshold`, `alert` (the held state), `n` (samples used), `verdict` (`alert`, `ok` or `no_verdict`), `note` (why no verdict) |
| `candidate` | the candidate request when a signal's alert started at this check, else null |
| `profile_sha256` | the profile in force |

`drift_alerts` gets one row per signal and check: `asset_id`, `at` (the
boundary), `regime`, `signal`, `distance` (null without a verdict),
`threshold`, `alert` (the held state).

## From a drift alert to a candidate (OPS-06)

When at least one signal goes from no alert to alert, that check writes one
request, `CAND-<asset>-<boundary>.json` (`candidate_request_v1`): the trigger
(time, regime, the signals with their distances and thresholds), the profile
and reference hashes and the command to run. Its name comes from stream time,
so a replay rewrites the same request instead of adding one. A signal that
goes into alert while another is already alerting starts its own request,
which is why the 13 episodes of 2020 wrote 22.

```bash
python eval/retrain_candidate.py --request runs/metropt3_apu/candidates/requests/CAND-<...>.json \
    --processed data/processed_v4 [--dry-run]
python eval/retrain_candidate.py --trigger labelled_event --event F4 --processed <rebuilt folder>
```

`eval/retrain_candidate.py` fits the candidate the way the harness and the
freeze do (harness `load`, `assign_roles`, `scale`; the PCA of
eval/detectors.py on the fit windows; the empirical threshold and SPOT with
the freeze rule; `evaluate.detector_bundle`), and writes
`runs/<asset>/candidates/<UTC time>/` with `bundle.json` (detector_bundle_v1
plus a `candidate` block), `candidate.json` (trigger, data, the frozen
detector's figures beside the candidate's) and, without a reachable MLflow or
with `--dry-run`, `register_dry_run.json` (tools/register_bundle.py's dry
run). With MLflow it registers a new version of `pdm-detector-<asset>` with no
alias. It refuses an output folder under eval/ and checks that
eval/config.yaml is unchanged afterwards. Nothing runs it on its own: the
request waits for someone, or a scheduled job, to run the command. On
data/processed_v4 the candidate is the frozen detector again (same fit
windows, same SPOT threshold 5.2624, same held-out figures), which is the
expected result until a processed folder with new data or labels exists.

## Promotion (OPS-07, PR-23)

```bash
python eval/promote.py --model pdm-detector-metropt3_apu --version 3 --alias production \
    --requested-by "Gayatri Suresh Chaudhari" --approved-by "Rosh Sreedharan" \
    --approval-ref "team minutes 2026-11-24, item 3" [--store "$PDM_STORE"]
```

Refused (exit 2, every reason printed, a `promotions` row `refused: <reasons>`)
while the evaluation lock in eval/config.yaml is on and today (UTC) is before
its `until` date (2026-11-22; the lock covers every alias), when the approver
is not in eval/approvers.yaml, when the approver is the requester (spellings
listed for the same person count as the same person), or when the approval
reference is empty. Otherwise `approved` is recorded first and the alias
moves if MLflow is installed and `MLFLOW_TRACKING_URI` answers (a failure
there adds a `failed: ...` row, exit 1); without MLflow the row reads
`approved: alias not moved (no MLflow here)` and the exact command is
printed. `promotions` is insert-only, so every attempt stays on record. The
drift service and the candidate tool never set an alias (a test checks it).

## Not verified here

- No broker: `main()` and `run_service` were not run; the stream
  replayer -> drift -> gate was not exercised end to end, nor the retained
  message. The handlers were driven in process.
- No MLflow: neither a candidate's registration nor an alias move ran against
  a registry; promote's MLflow path ran with a stand-in client only.
- No Evidently: the formula follows its `wasserstein` stattest as documented,
  but no number was compared with Evidently's output.
- SQLite only: `drift_alerts` and `promotions` were not written to
  PostgreSQL.
- The thresholds are in-sample: they come from the same 2020 healthy data the
  replays use. How often a healthy day outside 2020 trips them is unknown;
  light duty, with 28 healthy days, is the thinnest.
- The tests replay the 10 s grid (bin means of the raw rows), not the raw
  file; raw rows arrive every 9 to 10 s, so a window holds slightly more
  samples than the grid gives it. The grid has the freezes masked already, so
  the freeze rule is tested on synthetic data. Once, outside the tests, the
  raw file for Jun 18-28 went through services/replayer's `replay()` into the
  service: no alert with the freeze rule; without it, 14 alerting checks
  during the 62-h loaded-snapshot freeze of Jun 22-25 (TP2 up to 2.45 sd).
- `pdm/_sys/drift` is one topic for every asset: with several assets the
  retained message holds the latest asset's check only.
- A run of identical rows still shorter than 30 min at a window's end is not
  yet a freeze and is counted; ingest waits for such runs, the drift check
  does not.
