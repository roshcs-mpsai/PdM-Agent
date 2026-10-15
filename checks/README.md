# checks

Deterministic diagnostic checks, one folder per asset (PR-10, AGT-05). The MCP
tool `run_check` dispatches here through the profile's
`references.checks_registry`; the gate attaches every check's result and the
triage rules' verdicts to each incident it opens.

A check is a pure function `Evidence -> CheckResult`
(`lib/pdm_common/checks.py`). The evidence of one alarm is the 10 s grid up to
the moment the incident opens, the opening window's features and the gaps in
the data before it, so nothing recorded later can reach a result. A result is
`pass`, `fail` or `inconclusive`, with the measured value in words and as a
number, the threshold, the rule and what a failure points to.

## apu_metropt3

| Id | Check | Kind | Fails when | Threshold |
|---|---|---|---|---|
| C1 | data_quality.motor_off_command | data quality | load commanded with the motor under 1 A in the opening window | rule |
| C2 | data_quality.restart | data quality | data resumed after a 30-min gap less than 55 min before the alarm | rule |
| C3 | data_quality.bridged_points | data quality | an interpolated point in the 40 min behind the gate | rule |
| C4 | continuous_load_signature | fault signature | hours since the motor last rested above healthy p99 | 0.1889 h |
| C5 | load_share_rise | fault signature | loaded share of the past 6 h above healthy p99 | 0.2341 |
| C6 | oil_temperature_high | fault signature | opening-window oil temperature above healthy p99 | 70.85 degC |
| C7 | lps_activity | fault signature | low-pressure switch minutes in 24 h above healthy p99 | 28 min |
| C8 | leak_down | fault signature | panel pressure lost per hour across a gap that ended in the past 6 h, above healthy gaps' p99 | 4.053 bar/h |
| C9 | tp3_reservoirs_divergence | instrument | TP3 and Reservoirs over 0.1 bar apart for 10 min behind the gate | 0.1 bar |
| C10 | digital_polarity_audit | instrument | inconclusive until the suspect digital channels are settled | rule |
| C11 | refill_rate | fault signature | after a restart, panel pressure refills slower than healthy restarts' p10 | 0.1773 bar/min |

The statistical thresholds come from healthy operation only:
`python tools/calibrate_checks.py --processed data/processed_v4 --check`
recomputes them and fails if the profile disagrees. C9 never fires on the
2020 data (Journey C injects a TP3 fault to exercise it).

`triage.py` keeps the four rule sets of the Oct 2-4 A/B test (A1 motor-off,
A2 data quality, A3 with a fault-signature veto, A4 with the refill rate).
Since Oct 3 no rule removes an alarm: their verdicts travel with the incident
as evidence, and the policy stays `none`.

`tests/test_checks.py` pins every check on synthetic grids and, with
data/processed_v4 present, reproduces the 41 evidence packets of the
detector-selection notebook: every result, every measured number and the
A1-A3 verdicts.

## Adding an asset

Create `checks/<asset>/__init__.py` with a `REGISTRY` dict (check id ->
function), declare each id in the asset profile's `consistency_checks`, and
point `references.checks_registry` at the folder. Signal names may appear in
these folders and nowhere else in shared code.
