---
name: apu_metropt3
description: The MetroPT-3 air production unit - its channels, the air-leak failure mode and the checks that read it, the data-quality and instrument look-alikes, how to read its prognosis, and (in inspection.md) the inspection tasks and safety notes for its work orders.
version: "2026-10-10"
asset: metropt3_apu
---

# Air production unit (metropt3_apu)

An oil-lubricated, motor-driven compressor makes a metro train's compressed air and sends it through a cyclonic separator and a two-tower dryer to the pneumatic panel and the reservoirs. It loads when pressure falls below about 8.2 bar and unloads when the panel is full, so a healthy motor runs in short bursts and rests every few minutes.

## Channels

| Channel | Measures | Note |
|---|---|---|
| TP2 (bar) | pressure at the compressor | near 0 unloaded, 8 to 9 loaded |
| TP3 (bar) | pneumatic panel pressure | reference for leak and refill checks |
| H1 (bar) | separator pressure drop | high unloaded, near 0 loaded |
| DV_pressure (bar) | dryer discharge pressure drop | goes flat partway through the recording; flat is not a fault |
| Reservoirs (bar) | reservoir pressure | tracks TP3; read only by C9 |
| Oil_temperature (degC) | compressor oil | climbs during long loaded runs |
| Motor_current (A) | one motor phase | about 0 off, 4 offloaded, 6 to 7 loaded |
| COMP, MPG, Towers, LPS | intake closed, load request, dryer tower, pressure below 7 bar | trusted |
| DV_eletric | load commanded | trusted; can read 1 with the motor off (C1) |
| Pressure_switch, Oil_level, Caudal_impulses | pilot pressure, low oil, air-flow pulses | polarity unverified (C10): never evidence either way |

## The failure mode: air leak

Air leaks are the failures seen on this unit. A leak makes the compressor load more often and for longer; a large one keeps it loaded without a rest (TP2 high, Motor_current flat, H1 near zero) and heats the oil. Panel pressure may hold or fall: pressure that holds does not argue against a leak.

Fault-signature checks. Their thresholds, carried in each result, are healthy 99th percentiles (C11: the 10th), so each also fails now and then on a healthy unit.

- C4 continuous load: the motor has gone longer without a rest than healthy. Failure in progress.
- C5 load share: loaded for more of the past 6 h than healthy. An earlier, weaker sign.
- C6 hot oil: oil hotter than healthy in the opening window. Follows continuous load; weak alone.
- C7 low-pressure switch: active for longer in the past 24 h than healthy.
- C8 leak-down: panel pressure lost faster across a recent recording gap than across healthy gaps: a leak while stopped, or venting on purpose (look in the maintenance history).
- C11 refill rate: after a restart only, a slower refill than healthy restarts: air lost while charging. Designed in-sample, so supporting evidence only.

C4, C5 and C6 failing together, with no data-quality failure, is the leak signature: process likely. C8 or C11 failing adds to it.

## Look-alikes in the data

- C2 restart: data resumed after a gap, less than 55 min before the alarm. The panel refills from low pressure, so C4 (and often C7) fails with no leak. Then read C11: a normal refill points to the restart (data_quality likely); a slow one to air lost while charging (process possible, likely only with other signs).
- C1 motor-off command: load was commanded but the motor stayed off and pressure fell. Not a leak. Keep a data_quality and a device hypothesis (the motor or its starter did not start), call device_health, and recommend a data_quality_check unless device data show a fault.
- C3 bridged points: some values behind the alarm were interpolated; say so.
- Logger freeze: every analog channel repeats its value exactly while COMP keeps toggling. A frozen loaded snapshot looks exactly like the leak signature. If get_telemetry shows every analog channel flat over one span, data_quality is likely.

## Instrument faults

TP3 and Reservoirs measure the same pressure. C9 fails when they stay more than 0.1 bar apart for 10 min behind the alarm: one pressure sensor or its wiring is wrong, not the process. Then put instrumentation first and recommend a sensor_check, whatever else fails; signs that read TP3 (C8, C11, TP3 features in the attribution) lose their weight. Motor_current and Oil_temperature do not depend on the pressure sensors: if C4 to C6 also fail, keep a process hypothesis as possible.

C10 is inconclusive until the unverified channels are settled: expected, not a finding.

## Rule verdicts

Results may carry triage rules A1 to A4 with would_dismiss, what a rule would have done. No rule removes an alarm (and A4 was designed in-sample): evidence only.

## The prognosis

incident_context carries P(failure within 24, 72 and 168 h) with a 90 % interval, a constant-hazard baseline and the model's skill. The frozen model does not beat the constant hazard (skill.meets_bar is false). While that holds, cite it as the brief requires, say in the summary that it adds little, and never let it change a likelihood or the priority.

## Other failure modes

An oil leak or a leak on the consumers' side may look different and is untested here: if the results fit neither the leak signature nor a look-alike, say so (insufficient) rather than force a leak.
