# Inspection tasks and safety notes

General compressed-air practice, not the operator's or manufacturer's procedure, which take precedence. Take work-order tasks and safety notes from here.

## Suspected air leak (inspection)

1. With the system pressurised and the compressor stopped, survey the air path from compressor to reservoirs with an ultrasonic detector or leak-detection fluid: fittings, couplings, hoses, dryer valves, condensate drains, seals, the regulator, chafed pipes.
2. Pressure-decay test section by section (compressor stopped, section isolated, pressure recorded over a fixed time); compare with a known good unit or an earlier test.
3. Tag and record each leak; repair only after isolation and venting; repeat the decay test.
4. Confirm normal cycling in the data afterwards: the motor resting every few minutes, the oil back in its usual range.

Oil hot without continuous load: check the oil level on the unit (not the Oil_level channel), the oil cooler and the cooling-air path.

## Suspected sensor fault (sensor_check)

1. Compare the transmitter that moved alone (TP3 or Reservoirs) with a calibrated reference gauge at the same point.
2. Inspect its connector and wiring; compare it with its partner channel over the same minutes.
3. Recalibrate or replace it only if the reference confirms the fault.

## Data review (data_quality_check)

Review the data around the alarm with the data team (gaps, restarts, interpolated points, flat channels, load commanded with the motor off) before anyone goes to the train. Safety note: "No work on the equipment is requested by this order."

## Safety notes (copy those that apply)

- Stored energy: the compressor restarts on its own when pressure falls; reservoirs, dryer towers, separator and pipework stay pressurised after the motor stops.
- Isolate, lock out and tag the motor supply under the site's rules before any work.
- Isolate the section, vent it through its vent or drain valve (towers, separator and trapped volumes included) and confirm zero on a local gauge before loosening anything; a remote reading of zero is not proof.
- Never tighten, loosen or remove a fitting under pressure; during leak finding keep hands and face away from escaping air.
- The compressor, its discharge pipework and oil stay hot after running: let them cool, wear gloves, vent the oil circuit before opening it.
- Wear eye protection, and hearing protection near running equipment.
- Re-pressurise gradually after work, watching the opened joints.
