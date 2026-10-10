# Oil temperature and thermal stress

PdM-Agent team note, 2026-10-10, CC BY 4.0.

## What the channel is

Oil_temperature is the compressor's oil temperature in degC, a detector input, with a plausibility range of 10 to 95 degC in the asset profile. The oil lubricates the compressor and takes up part of the heat of compression. While the compressor runs in short load bursts the oil has time to cool between charges; when it runs loaded without a rest, the heat builds up and the oil temperature climbs over hours.

## Healthy and failure values

The check C6 compares the mean oil temperature of the window in which an alarm opens with the 99th percentile of healthy windows, 70.85 degC. In the four repair-forcing leaks of the 2020 data the oil climbed to 75 to 90 degC, and in one of them it reached 89.05 degC, the highest value of the campaign, after about 16 hours without a rest. High oil temperature in this data follows continuous load. It supports a leak hypothesis when it comes with continuous load (C4) and a high load share (C5), and says little on its own.

## Rise per hour

The feature oil_rise_1h, the window's mean oil temperature minus the same an hour earlier, hardly separates failure windows from healthy ones in this data. The level of the oil temperature matters more than its hourly change.

## Cold oil after a start

Low oil temperature means the compressor has just started after standing. Two of the twelve false alarms on held-out healthy weeks in the detector selection were starts from cold: the detector found the cold oil unusual, and nothing was wrong. When an alarm opens less than 55 minutes after data resume and the oil is cold, the restart is the first explanation to check (C2).

## Seasonal drift

In the 2020 data, healthy windows in late spring were already warmer and busier than earlier ones. That may be the season; the cause is not settled. Compare the oil temperature with recent healthy operation of the same unit rather than with a fixed value from another season.

## Other causes of high oil temperature

These are general practice, not findings in this data. Apart from continuous load, oil runs hot when cooling is impaired (a dirty or blocked oil cooler, restricted cooling air, a high ambient temperature), when the oil level is low or the oil is degraded or of the wrong grade, or when a thermostatic valve in the oil circuit sticks. None of these can be told apart with the 2020 channels; they are what to check on the unit when the oil is hot without continuous load.

## The oil level channel

The Oil_level digital channel is not a reliable guide: its polarity is unconfirmed, it did not change at any of the four leaks, and it reads 0 for most of two stretches late in the campaign. Check the oil level on the unit as the maintenance instructions specify.

## Safety

The oil and the surfaces around it stay hot after the compressor stops, and in some compressor designs the oil circuit is under air pressure while the unit runs. Let the unit cool and vent it before opening anything on the oil side; see the safety note.

Sources in this repository: profiles/metropt3_apu/asset_profile.yaml (Oil_temperature, Oil_level, C2, C6), profiles/metropt3_apu/events.yaml (failure notes), notebooks/metropt3_eda_v4.ipynb Sections 5, 5.2 and 8.2, notebooks/detector_selection.ipynb Section 6.3.
