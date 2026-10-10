# The air production unit and what each channel measures

PdM-Agent team note, 2026-10-10, CC BY 4.0.

The asset is the air production unit (APU) of a Metro do Porto train, as recorded in the MetroPT-3 dataset (UCI dataset 791, CC BY 4.0). The unit makes the compressed air that the train's pneumatic equipment, such as the brakes, runs on. The 2020 recording holds 1,516,948 records, one every 10 seconds (0.1 Hz; the real steps are 9 to 10 s), with 7 analog and 8 digital channels. All times are UTC.

## The air path

Air is drawn in and compressed by an oil-lubricated compressor driven by an electric motor. It then passes a cyclonic separator, which swirls the air to throw out liquid droplets, and an air dryer with two towers. In a twin-tower dryer of the usual design one tower dries the air while the other is purged and regenerated, and the two swap over. The dry air goes on to the pneumatic panel and to the downstream reservoirs, which store it for the train's consumers. The repository does not record whether the compressor is a screw or a piston machine, so these notes keep to what the channels show.

## How the compressor is controlled

The compressor works in load and unload cycles. It starts loading when pressure falls below about 8.2 bar (the MPG signal) and charges until the panel is full again; in healthy operation a 10-minute window typically sees panel pressure between about 8.2 and 9.9 bar. Normal operation is short load bursts: the motor is loaded only about 16 % of the time, and in 99 % of healthy windows it has stopped within the last 11 minutes. The motor current shows the state: about 0 A with the motor off, about 4 A running offloaded and about 6 to 7 A loaded. The system counts the motor as loaded above 4.5 A and as stopped below 1 A.

## Analog channels

- TP2 (bar): pressure at the compressor. It sits near 0 bar while the compressor is not loading and rises to 8 to 9 bar under load.
- TP3 (bar): pressure at the pneumatic panel. The checks use it as the reference pressure for leaks and refills.
- H1 (bar): pressure drop at the cyclonic separator. It reads high while the compressor is not loading and falls to near zero under load, the mirror image of TP2 (correlation -0.96).
- DV_pressure (bar): pressure drop at the towers' discharge. Its behaviour changes partway through the campaign: it goes flat and stays flat, so the channel has two different normals and is not a detector input.
- Reservoirs (bar): downstream reservoir pressure. It tracks TP3 almost exactly (correlation 1.00), so it is not a detector input either; it is kept for the plausibility check against TP3.
- Oil_temperature (degC): compressor oil temperature.
- Motor_current (A): the current in one phase of the motor.

## Digital channels

Each is 0 or 1. COMP: air intake closed, compressor off or offloaded. DV_eletric: outlet valve, compressor under load. Towers: which dryer tower is drying. MPG: load request below 8.2 bar, mirrors COMP. LPS: low-pressure switch, active below 7 bar. Pressure_switch: pilot-valve pressure detected. Oil_level: oil below the expected level. Caudal_impulses: air-flow pulses from a flow meter. The spellings DV_eletric and Caudal_impulses are the dataset's own. Three of these channels have an unconfirmed polarity; the note on digital channels says which.

## What the detector reads

The frozen detector reads 21 features of each 10-minute window: summary statistics of TP2, TP3, H1, Oil_temperature and Motor_current, and engineered channels built on motor current, panel pressure and oil temperature (the share of the last 6 hours with the motor loaded, the hours since the motor last rested, panel pressure against the past week, and the oil temperature rise over an hour). The asset profile gives every analog channel a plausibility range taken from the observed minimum and maximum, rounded outwards: TP2 and H1 -0.1 to 11 bar, TP3 and Reservoirs 0.5 to 11 bar, DV_pressure -0.1 to 10.5 bar, oil temperature 10 to 95 degC and motor current 0 to 10 A.

Sources in this repository: profiles/metropt3_apu/asset_profile.yaml (signals, detector), profiles/metropt3_apu/features.yaml, eval/frozen/metropt3_apu/detector/bundle.json (healthy medians), notebooks/metropt3_eda_v4.ipynb (dataset notes, Sections 3 and 4).
