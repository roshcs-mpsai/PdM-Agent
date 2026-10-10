# Digital channels: what each means and which to trust

PdM-Agent team note, 2026-10-10, CC BY 4.0.

The eight digital channels were read against the analog data, with the motor counted as loaded above 4.5 A and logger freezes masked. The asset profile records a polarity verdict for each: verified (the description matches the behaviour), plausible (consistent, not yet confirmed), or suspect_inverted (the duty cycle contradicts the description, so 0 is treated as the eventful state until transitions around failures confirm it either way).

## Verified channels

- COMP, air intake closed (compressor off or offloaded): active 84 % of the time, and at 0 while loading.
- DV_eletric, outlet valve, compressor under load: active 16 % of the time, and 98.7 % of its active samples have the motor loaded. It reads 1 with the motor off in 0.14 % of samples (load commanded, motor not running), which is why the load share is built on motor current instead.
- Towers, which dryer tower is drying: active 92 % of the time because the towers switch only while the compressor loads; it averages 0.50 under load and 1.00 otherwise.
- MPG, the load request when pressure falls below 8.2 bar: mirrors COMP.
- LPS, the low-pressure switch, active below 7 bar: active 0.3 % of the time; the note on the low-pressure switch covers it.

## Channels with unconfirmed polarity

- Pressure_switch, described as active when pilot-valve pressure is detected: active 99 % of the time. Its 0 state is the rare one, about 22 transitions a day, mostly under load. The zeros are a load effect, not a failure signal.
- Oil_level, described as active when the oil is below the expected level: active 90 % of the time. It did not change at any of the four leaks, and it reads 0 for most of two stretches late in the campaign. If its polarity is inverted, the one channel that should speak to an oil leak reads the wrong way, which matters for the 2022 campaign, where an oil leak occurs.
- Caudal_impulses, air-flow pulses from a flow meter: active 94 % of the time, with a median of 0 transitions a day and zeros in one month only (34 % of that month's samples). At 0.1 Hz it carries no usable information.

## What follows for diagnosis

The check C10, the digital polarity audit, stays inconclusive until the three suspect channels are settled, and none of them is used by any check or by the detector. A brief should not cite Pressure_switch, Oil_level or Caudal_impulses as evidence for or against a fault; if one of them looks relevant, say that its meaning is unverified.

## Settling a polarity on site

General practice: an inverted reading often comes from a switch wired with normally-closed rather than normally-open contacts, or from logic that reports the opposite state. Checking the wiring diagram, or watching the channel while the unit passes through the state it describes, settles it.

Sources in this repository: profiles/metropt3_apu/asset_profile.yaml (signals.digital, C10), checks/apu_metropt3/__init__.py, notebooks/metropt3_eda_v4.ipynb Sections 4 and 4.1.
