# The diagnostic checks C1 to C11 and how to read them together

PdM-Agent team note, 2026-10-10, CC BY 4.0.

When the gate opens an incident, every check in the asset's registry runs on the data as of the opening and returns pass, fail or inconclusive, with the measured value in words and as a number, the threshold, the rule and what a failure points to. The statistical thresholds are percentiles of healthy operation only, never read off a failure. The values below are the profile's when this note was written; each check result carries the threshold in force.

## Data quality: the alarm may have a benign cause in the data

- C1 motor-off command: load commanded with the motor under 1 A at any point of the opening window. A control or data problem, not an asset fault.
- C2 restart: data resumed after a gap of 30 minutes or more, less than 55 minutes before the alarm. A start-up transient.
- C3 bridged points: any interpolated grid point in the 40 minutes behind the gate. The values there are partly reconstructed.

## Fault signature: the data show a developing fault

- C4 continuous load: hours since the motor last rested above 0.1889 h. Failure in progress.
- C5 load share: the share of the past 6 hours with the motor loaded above 0.2341; inconclusive with under an hour of data. A leak precursor.
- C6 hot oil: oil temperature in the opening window above 70.85 degC. Thermal stress under continuous load.
- C7 low-pressure switch: active for more than 28 minutes in the past 24 hours. Low-pressure episodes.
- C8 leak-down: panel pressure lost per hour across a gap that ended in the past 6 hours above 4.053 bar per hour; inconclusive when there is no such gap. An air leak while the compressor stood still.
- C11 refill rate: only for a restart alarm (C2 fails). Panel pressure rising slower than 0.1773 bar per minute while the motor is loaded below 9.5 bar, measured over at most the first 24 minutes after data resume and needing 5 minutes of such data. A slow refill: air lost while charging. It was designed after reading answers on the incidents it was tried on, so its track record is in-sample.

## Instrument: a sensor, not the compressor, may be at fault

- C9 panel against reservoir pressure: more than 0.1 bar apart for 10 minutes behind the gate. It never fires on the 2020 data.
- C10 digital polarity audit: inconclusive until the suspect digital channels are settled; none of them is used by the checks.

## Reading the checks together

- A failed data-quality check and no failed fault signature: the benign explanation comes first. The exception worth flagging is C1: a compressor that does not start when load is commanded is itself worth a look.
- A restart (C2) with a slow refill (C11): air is being lost while the panel charges. With a normal refill, the restart explains the alarm.
- Continuous load (C4) with a high load share (C5) and hot oil (C6): the air leak signature. Pressure lost across a gap (C8) strengthens it; pressure that holds does not weaken it, because the compressor may be keeping up.
- C9 failing: treat it as an instrument problem first, whatever else fails.
- Inconclusive is not pass. It means the data the check needs were missing (no gap to measure, too little history), and the brief should say which.

## What a dismissal needs

No rule removes an alarm: the check results travel with the incident as evidence and the planner decides. The agent may recommend dismissing an alarm only by citing a data-quality check that failed in that incident's results; a recommendation to dismiss without one is shown as an escalation.

Sources in this repository: checks/README.md, checks/apu_metropt3/__init__.py, checks/apu_metropt3/triage.py, profiles/metropt3_apu/asset_profile.yaml (consistency_checks), notebooks/detector_selection.ipynb Sections 8.2, 8.7 and 9.6.
