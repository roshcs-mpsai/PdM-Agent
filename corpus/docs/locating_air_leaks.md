# Finding an air leak on site

PdM-Agent team note, 2026-10-10, CC BY 4.0. General practice for compressed-air systems, written by the team from general engineering knowledge. It is not the train operator's or the manufacturer's procedure; theirs take precedence.

## Start from the data, then go to the train

The monitoring data can say that air is being lost and roughly how fast; they cannot say where. Before a technician goes looking, the brief should state what the data show (continuous load, pressure lost while idle, a slow refill, pressure lost across a stop) so the search starts with the right expectation of the leak's size.

## Safety first

Leaks are found with the system under pressure, but nothing is tightened, loosened or replaced under pressure. Every repair step starts with isolation, lock-out and venting, as the safety note describes. Keep hands and face away from escaping air.

## Methods, from quick to thorough

- Listening. With the compressor stopped and the train quiet, walk the air path from the compressor to the consumers and listen for hissing. Only larger leaks can be heard, and background noise hides most of them.
- Leak-detection fluid. Brush or spray soap solution or a commercial leak-detection fluid on suspect joints while the system is pressurised; a leak blows bubbles. Use a fluid that will not harm seals or paint, and wipe it off afterwards.
- Ultrasonic detector. Escaping air makes high-frequency sound that an ultrasonic detector picks up in a noisy place and from a distance, which makes it the usual tool for joints that are hard to reach and for surveying a whole system.
- Pressure-decay test. Stop the compressor, isolate the section under test from the consumers, and record its pressure over a fixed time. The rate of fall shows whether that section leaks and how badly, and repeating the test section by section narrows the search. Compare the result with the same test on a known good unit or an earlier test of the same unit rather than with an absolute number.

## Where air leaks usually are

Threaded and compression fittings, hose connections and couplings, valve bodies and stems (including the dryer's switching valves and the drain valves), seals and gaskets, pressure regulators, condensate drains that stick open, and cracked or chafed pipes and hoses. Joints that move or vibrate in service leak more often than fixed ones.

## The data as an unplanned decay test

A recording gap during which the compressor stood still works as a pressure-decay test nobody planned. The check C8 compares the panel pressure lost per hour across a gap that ended in the past 6 hours with the 99th percentile of healthy gaps, 4.053 bar per hour. The idle-loss feature does the same over the last 6 hours while the motor is not loaded. A fast loss across a gap can also mean the system was vented on purpose, for example for maintenance, so it needs the maintenance record before it is called a leak.

## After the repair

Tag every leak found, record where it was and how it was fixed, and repeat the decay test to confirm. In the data, a repaired leak should show normal cycling again: the motor resting every few minutes, the 6-hour load share back near its healthy median of about 0.10, and oil temperature back in its healthy range.

Sources in this repository: profiles/metropt3_apu/asset_profile.yaml (C8), profiles/metropt3_apu/features.yaml (idle_loss_6h, gap_leak_down), notebooks/metropt3_eda_v4.ipynb Section 8.3, notebooks/detector_selection.ipynb Section 6.3; general compressed-air practice.
