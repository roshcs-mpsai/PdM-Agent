# Sensor plausibility: channels that must agree, drift and stuck readings

PdM-Agent team note, 2026-10-10, CC BY 4.0.

## Process fault or instrument fault?

A leak, a blockage or a control fault changes the process, and every sensor that sees that part of the process moves with it. A failing sensor changes one channel and leaves its partners where they were. So the first plausibility question is whether sensors that should agree still do. When two of them disagree for minutes on end, suspect an instrument before the process.

## Panel and reservoir pressure

TP3 (pneumatic panel) and Reservoirs (downstream reservoir) measure what is effectively the same pressure. In the 2020 data they agree to within 0.010 bar at the 99.9th percentile and correlate at 1.00, and only three rows in seven months differ by more than 0.1 bar, never two in a row. The check C9 fails when the panel and reservoir pressure sensors disagree by more than 0.1 bar for 10 minutes in a row in the 40 minutes behind the alarm. Such a disagreement means one of the two pressure channels is wrong, not the process: the diagnosis is instrumentation, the NE 107 status is out of specification rather than maintenance required, and the recommended action is a sensor check, not an inspection for a leak. C9 never fires on the 2020 data; the tests trip it with an injected drift of 0.3 bar on TP3 over 2 hours. Single-row differences are noise, which is why the rule asks for 10 minutes.

## Compressor pressure and separator drop

TP2 and H1 mirror each other (correlation -0.96): H1 is high while the compressor is not loading and near zero under load. A sustained break in that relationship is worth a look as a possible instrument problem, although no check reads it yet.

## Drift

A drifting sensor reads a slowly growing offset. Against a partner channel, drift shows as a gap that widens over days; on its own, as a level that moves away from the healthy reference. The system compares the last 24 hours of TP2, TP3, H1, Oil_temperature and Motor_current with the healthy reference of the same operating regime every 6 hours (a Wasserstein distance per signal), and a drift alert sets the status to out of specification. A drift alert says the data no longer look like the data the detector was trained on; it does not say whether the sensor or the process changed.

## Stuck readings and logger freezes

A stuck sensor repeats one value while its partners move. If every analog channel repeats its value exactly at the same time, the sensors are not at fault: the logger has frozen. The 2020 data hold ten such freezes, about 170 hours in all, with the digital COMP channel still toggling while the analog values stand still. A freeze is masked before any feature is computed, and it is a function check on the data, not an asset alarm.

## Out-of-range readings

The asset profile gives every analog channel a plausibility range taken from the observed minimum and maximum, rounded outwards. Ingest flags any reading outside its range, and the flag sets the status to out of specification. An out-of-range value is a sensor or wiring problem until shown otherwise.

## A channel that changes its normal

DV_pressure changes behaviour partway through the campaign and stays flat afterwards. That is a change of regime, not a sensor fault, and it is why the channel is not a detector input: a model fitted across the change would learn the date rather than the fault.

## On site

General practice: confirm a suspect pressure transducer against a calibrated reference gauge at the same point, check its connector and wiring, and compare it with its partner channel before replacing anything.

Sources in this repository: profiles/metropt3_apu/asset_profile.yaml (signals, C9, drift), checks/apu_metropt3/__init__.py, tests/test_checks.py (injected drift), lib/pdm_common/status.py, services/ingest/ingest.py (out_of_range), notebooks/metropt3_eda_v4.ipynb Sections 2.2, 3, 3.1 and 3.2.
