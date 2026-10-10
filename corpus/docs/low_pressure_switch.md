# The low-pressure switch (LPS)

PdM-Agent team note, 2026-10-10, CC BY 4.0.

## What the switch signals

LPS is a digital channel from a pressure switch that becomes active when pressure falls below 7 bar. Its polarity is verified against the analog data: 84 % of the samples with LPS = 1 have reservoir pressure below 7 bar, and 96 % of the samples with reservoir pressure below 7 bar have LPS = 1. It is active in only 0.3 % of samples. In healthy operation a 10-minute window typically sees panel pressure between about 8.2 and 9.9 bar, so the switch stays quiet.

## Minutes, not hours

Low-pressure activity in this data is measured in minutes per day. Counting active samples times 10 seconds, only two days of the campaign reach an hour, both of them reported failures, and every day with 20 minutes or more is a reported failure, a restart refilling the panel after a recording gap, or load commanded while the motor stayed off. An earlier analysis reported several heavy low-pressure days by multiplying the share of recorded rows by 24, which turns ten minutes on a day with one hour of data into four hours. Count active time as samples times the sampling step, never as a share of the rows that happen to exist.

## The check C7

C7 counts the minutes with LPS = 1 in the past 24 hours (the feature lps_minutes_24h) and fails above 28 minutes, the 99th percentile of healthy operation. A failure means low-pressure episodes: the compressor is not keeping the system above 7 bar.

## Reading low-pressure activity

Three explanations are not a leak and one is:

- a restart: after the train has stood, the panel can be near empty when recording resumes, and the switch is active for the first minutes of the refill (check C2, and the refill rate, C11);
- load commanded with the motor off: the valves call for load, the motor does not start and pressure falls (check C1);
- demand the compressor does not keep up with for a while;
- an air leak large enough to pull pressure down while the compressor runs, which usually comes with continuous load (C4) and hot oil (C6).

Low pressure right after data resume, or with the motor off, is a data-quality or control case first. Low pressure with sustained load points to a failure.

## Not to be confused with

Pressure_switch is a different channel (pilot-valve pressure detected, polarity unconfirmed, 0 is its rare state), and MPG is the load request below 8.2 bar. In the light-duty first month of the recording, LPS is silent throughout.

Sources in this repository: profiles/metropt3_apu/asset_profile.yaml (LPS, C7), profiles/metropt3_apu/events.yaml (v0.3 header, regimes), eval/frozen/metropt3_apu/detector/bundle.json (healthy medians), notebooks/metropt3_eda_v4.ipynb Sections 4 and 4.1.
