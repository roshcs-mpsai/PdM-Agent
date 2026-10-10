# Data-quality pitfalls in the MetroPT-3 recording

PdM-Agent team note, 2026-10-10, CC BY 4.0.

An alarm on this unit has a benign explanation in the data more often than an asset fault would suggest. These are the situations to rule out first; the data-quality checks C1 to C3 read three of them.

## Sampling and clock

One record every 10 seconds (0.1 Hz); the real steps are 9 or 10 s. The figure of 15,169,480 instances that circulates for this dataset is ten times the file: the count at the original 1 Hz, before the data were published at 0.1 Hz. The clock is UTC and records straight through the change to summer time, with no jump and no hole.

## Gaps and overnight stops

About 82 % of the campaign is recorded. There are 331 gaps longer than 50 seconds, about 909 hours in all. Gaps of 3 to 12 hours hold nearly half the missing time, and most of them are the train parked overnight: 57 gaps of 3 hours or more start between 19:00 and 03:00 UTC and end before 06:00, most of them at 04:20 to 05:05, when service starts. Fifteen gaps of 12 hours or more, 340 hours together, are real outages. Windows that straddle a gap are dropped, never filled in.

## Logger freezes

Ten times the logger kept writing rows while the analog inputs had stopped updating: all seven analog channels repeat their values exactly, the step is 12 seconds, and COMP keeps toggling. The ten freezes last about 170 hours together, 3.3 % of the rows, and one pair of them holds a loaded snapshot for 62 hours that looks exactly like the failure signature. Freezes are masked before resampling and features and excluded from training, scoring and evaluation. A run is only known to be a freeze once it has lasted 30 minutes, so a stream cannot mask the first half hour of one. An alarm with every analog channel flat is a freeze until shown otherwise: a function check on the data, not an asset alarm.

## Restarts after a stop

When recording resumes after a stop of 30 minutes or more, the panel is often near empty: in half of the 190 restarts pressure was below 7 bar at the first sample. The motor then runs without a pause until the panel is full, and the low-pressure switch is active for the first minutes. In restarts that begin below 8 bar, pressure is back at 8 bar within 10 minutes in nine of ten, but the motor's first rest comes later, after a median of 13 minutes (19 minutes at the 90th percentile). For those minutes a restart looks like a leak. The persistence gate needs 15 windows of 10 minutes, so the earliest an alarm can open after data resume is 24 minutes in, and the detector's restart alarms open then. C2 fails when data resumed less than 55 minutes before the alarm, and C11 then compares the refill rate with healthy restarts.

## Interpolated points

Holes of one or two grid points (30 seconds or less) are bridged, by linear interpolation for analog channels and by carrying the last value for digital ones, and the bridged points are flagged. Only 86 points of the whole grid are bridged. C3 fails on any bridged point in the 40 minutes behind the alarm: the values there are partly reconstructed.

## Load commanded with the motor off

In parts of 15 clock hours the valves command load (DV_eletric = 1) while the motor current stays under 1 A and pressure falls; in one spell the panel fell from 8.2 to 4.9 bar before the motor started. There are 11 such spells of 5 minutes or more, 322 minutes in all. They are in neither label source and they are not leaks: the compressor is asked to load and does not run. They were among the detector's most frequent false alarms, but a compressor that does not start when asked is still worth a look, so C1 reports them and no rule dismisses the alarm.

## Pressure lost across a gap

Across most gaps of 5 to 120 minutes the panel loses a fraction of a bar, but a few gaps lose 6 to 9 bar in under an hour. That can be a leak or the system being vented on purpose, and it needs the maintenance record to tell. C8 reads the loss rate across a recent gap.

## Counting pitfalls

Cycle counts alias at a 10-second step, so they are a lower bound, and the apparent cycling spikes in this data were all logger freezes. A daily amount of time, such as low-pressure minutes, must be counted as samples times 10 seconds, not as a share of the recorded rows scaled to 24 hours.

Sources in this repository: profiles/metropt3_apu/asset_profile.yaml (sampling, gap_policy, freeze_rule, C1 to C3), profiles/metropt3_apu/events.yaml (data_quality_masks, data_quality_events, gap_edges), notebooks/metropt3_eda_v4.ipynb Sections 2, 2.1, 2.2, 2.3, 4, 5.3, 7.1 and 8.3.
