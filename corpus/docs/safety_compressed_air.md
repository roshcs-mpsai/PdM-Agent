# Safety before work on the compressed-air system

PdM-Agent team note, 2026-10-10, CC BY 4.0. General safety points for work on compressed-air equipment, for the safety notes of a draft work order. It is not a procedure: the train operator's safety rules and the manufacturer's instructions govern every task, and a competent person decides how they apply.

## Stored energy is the main hazard

A stopped compressor is not a safe compressor. This unit starts loading on its own when pressure falls below about 8.2 bar, and the reservoirs, dryer towers, separator and pipework still hold compressed air after the motor stops. Released suddenly, that air can throw fittings, hoses and debris, and a jet of compressed air can injure skin and eyes.

## Before work: isolate, lock out, vent

- Isolate the motor's electrical supply, then lock it out and tag it under the site's lock-out rules, so that the compressor cannot restart while anyone is working on it.
- Isolate the section to be worked on from the reservoirs and the rest of the system with its isolation valves.
- Vent the isolated section through its designated vent or drain valve, including the dryer towers, the separator and any volume trapped between closed valves.
- Wait for pressure to read zero on a local gauge, and confirm that no air escapes when a vent is opened, before loosening anything.
- Do not take the monitoring data as proof that the system is depressurised. The logger can freeze and a sensor can drift, so a remote reading of zero is not evidence that the system is empty.
- Never tighten, loosen or remove a fitting, hose or component under pressure, not even to stop a leak.

## Hot surfaces and oil

The compressor, its discharge pipework and its oil are hot after running; this unit's oil reached 89 degC during one of the 2020 leaks. Let the unit cool, wear suitable gloves, and treat the oil circuit as hot and, in some designs, pressurised until it has been vented.

## During work

- Wear eye protection, and hearing protection near running equipment or when using air tools.
- Never point compressed air at a person or use it to clean skin or clothing.
- Condensate drains and vent valves can discharge suddenly and loudly; keep clear of their outlets.
- Leak finding needs the system pressurised: do it before isolation, keep hands and face away from suspected leaks, and carry out repairs only after isolation and venting.

## Returning to service

Remove locks and tags only as the site procedure allows, check that tools and people are clear, close vents and drains, re-pressurise gradually while watching the joints that were opened, and confirm normal cycling in the data afterwards: the motor resting every few minutes and panel pressure back in its usual band.

## For the agent

Every draft work order carries safety notes; the brief schema requires at least one. The agent drafts and a person approves. PdM-Agent never writes to equipment.

Sources in this repository: schemas/brief_v1.json (work_order.safety_notes), profiles/metropt3_apu/asset_profile.yaml (MPG), profiles/metropt3_apu/events.yaml (failure notes); general compressed-air safety practice.
