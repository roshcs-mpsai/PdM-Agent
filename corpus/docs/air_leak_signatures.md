# Air leaks: the failure mode and how it shows in this data

PdM-Agent team note, 2026-10-10, CC BY 4.0.

All four failures that forced a repair in the 2020 MetroPT-3 campaign were air leaks; the dataset documentation describes them as air leaks under high stress. The maintenance expert's own log (Davari et al., 2021, Table II) lists 21 failures in the months it covers, from 20 minutes to 20 hours long, and the four repairs are among them.

## Why an air leak changes the compressor's behaviour

A leak lets air escape all the time, whether or not the train is using any. The compressor has to replace that air, so it loads more often and for longer. A small leak shows as more frequent charges and as pressure falling faster while the compressor is idle. A large leak keeps the compressor loaded without a rest: pressure then either holds, because the compressor keeps up, or falls, because it cannot. Running loaded for hours also heats the oil.

## The air leak signature in this data

In all four repair-forcing leaks normal cycling stops and the unit runs continuously under load: TP2 is pinned near 8 to 9 bar, motor current is flat at about 5.6 A, oil temperature climbs to 75 to 90 degC, and H1 stays near zero. Detecting a leak while it is happening is therefore easy. In the months the expert's log covers, a run of 30 minutes or more with the motor loaded in every minute (TP2 above 6 bar and motor current above 4.5 A) was a reported failure in all but one case, and such runs cover 18 of the 21 logged failures. Pressure on its own does not separate a leak from heavy demand: the log reports failures in which panel pressure held as well as ones in which it fell.

## The checks that read the leak signature

The checks registry turns the signature into deterministic tests whose thresholds come from healthy operation. The values here are the profile's when this note was written; each check result carries the threshold in force.

- C4 continuous load: hours since the motor last rested above 0.1889 h, about 11 minutes. In normal operation the motor stops every few minutes.
- C5 load share: the share of the past 6 hours with the motor loaded above 0.2341.
- C6 hot oil: oil temperature in the window where the alarm opens above 70.85 degC.
- C7 low-pressure episodes: the low-pressure switch active for more than 28 minutes in the past 24 hours.
- C8 leak-down: panel pressure lost across a recording gap that ended in the past 6 hours faster than 4.053 bar per hour.
- C11 slow refill: after a restart, panel pressure rising slower than 0.1773 bar per minute while the motor is loaded below 9.5 bar.

## Before the air leak is reported

Lead time differs from leak to leak. Before one of the four the compressor stopped resting about 14 hours ahead of the report: the hourly share of time loaded climbed from about 0.35 overnight to 0.86 in the hour the report came and 1.00 after it, and the oil went on to 89.05 degC, the campaign maximum. The other three show little beforehand. The feature that moved earliest is the idle loss, the panel pressure lost per hour while the motor is not loaded: from three days to one day before two of the leaks it was higher, and the refill rate lower, than on healthy days. That is the leak-down a slow leak should produce, but it rests on a single pair of events, and idle loss also drifts with the season. Reports record when a leak was noticed, not when it began.

## What looks like an air leak and is not

Three situations in the data reproduce parts of the signature. The refill after recording resumes from a stop: the panel is often near empty and the motor runs without a pause for several minutes. Load commanded while the motor stays off: pressure falls with no compressor running. A logger freeze that holds a loaded snapshot for hours. The data-quality checks C1 to C3 are there to rule these out before an alarm is read as a leak; the note on data-quality pitfalls describes each.

## Leaks of other kinds

The 2020 failures are air leaks under high stress. An oil leak, or an air leak on the consumers' side of the system, may look different. The 2022 campaign of the same dataset family includes both, and nothing in this note has been checked against them.

Sources in this repository: profiles/metropt3_apu/events.yaml (failures, expert_reported, verification note), profiles/metropt3_apu/asset_profile.yaml (consistency_checks), checks/README.md, notebooks/metropt3_eda_v4.ipynb Sections 5, 5.1, 5.2 and 8.2.
