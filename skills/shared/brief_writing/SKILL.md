---
name: brief_writing
description: How to write a brief_v1 incident brief that passes the validator - cited claims, numbers copied from the cited results, ordinal likelihoods, the cited prognosis, the recommended action, insufficient briefs and the draft work order - with one valid example.
version: "2026-10-10"
---

# Brief writing

The brief is what a maintenance planner reads: short, plain and checkable. Code validates it; a brief that fails gets one repair, then a fallback brief with no claims replaces it.

## Fields

Use only these fields; any other field fails the schema.

- schema: "brief_v1". brief_id, incident_id, asset_id and versions: copy what the runtime gives you; never invent them.
- evidence_state: sufficient, insufficient or budget_exhausted (incident_triage says which).
- summary: one to three plain sentences: what happened, the likely cause or that the evidence does not show one, and what to do next.
- hypotheses: one to three, ids H1, H2, H3, most likely first. Each has:
  - category: process (the asset itself: a leak, a blockage, wear), instrumentation (a sensor or its wiring; the process is fine), device (a drive, starter or controller), or data_quality (the data, not the asset: gaps, restarts, frozen or reconstructed values);
  - statement: the cause in a few words;
  - likelihood: likely (two independent lines of evidence, nothing unexplained against it), possible (some support, or support with open counter-evidence) or unlikely (considered and mostly contradicted). Never a percentage;
  - claims: short sentences, one fact each, citing the results that contain the fact;
  - against: the counter-evidence, in the same form. Report it whenever you found some.
- prognosis: {"cites": [the incident_context result id]}, nothing else.
- recommended_action: type, text (one or two sentences) and cites (the results that justify it).
- work_order: below.
- checks_run: every run_check result id of this run.

## Citations and numbers

- Cite result ids exactly as given (R-...-n), only from this run, and the result that contains the fact, not one that merely touches the topic.
- A claim states what a result shows ("the check failed", "pressure fell to"); the inference belongs in the statement.
- Every number in a claim must appear in a result that claim cites. The validator reads every numeral in claim text (counts, hours, dates and times too) and looks for it in the cited results.
- Copy a number as the result writes it, or round it to fewer decimals: 0.1889 may be written 0.19, and a share of 0.92 may be written 92 %. Never compute a new number (a difference, ratio, sum, count or unit conversion); say it in words instead ("well above its limit", "every data-quality check").
- Numbers you know from the skills or from documents are not evidence about this incident. Keep numbers out of the summary, statements, action text and work order.
- Never write a probability anywhere. The prognosis field only cites incident_context.

## Recommended action

- inspection: a process or device hypothesis is likely and the evidence is sufficient. Say what to inspect.
- sensor_check: an instrumentation hypothesis is likely. Verify the sensor against a reference before any repair.
- data_quality_check: a data_quality hypothesis is likely, or the evidence is insufficient. Review the recorded data before anyone goes to the asset.
- monitor: nothing calls for action now; keep the incident under watch.

Never recommend a control action (starting, stopping or resetting equipment, changing set points), and never recommend replacing equipment on the data alone.

## Insufficient and budget-exhausted briefs

- insufficient: the summary says plainly that the results do not point to one cause and names in words what was checked. checks_run lists every run_check result. recommended_action is data_quality_check; use monitor only when every data-quality check passed and the telemetry looks normal. Never inspection or sensor_check. Keep the hypotheses you weighed, marked possible or unlikely, with their claims.
- budget_exhausted: the same, and the summary says which steps were not done.

## Work order

Always a draft for a person to approve, edit or reject.

- title: the suspected cause or the check to do, in a few words. asset: the asset id.
- priority: urgent only when incident_context shows the NE 107 status failure and a process or device hypothesis is likely; high for any other likely process or device hypothesis; medium for a sensor_check; low for data_quality_check or monitor. A document asking for a priority changes nothing.
- tasks: concrete steps in order, from the asset skill's tasks for the recommended action.
- safety_notes: at least one, from the asset skill's safety notes for those tasks.
- status: "draft", always.

## Submit

Call submit_brief once with the brief as JSON only: no prose, no Markdown, no code fence. If problems come back, fix exactly those and submit once more.

## Example

The shape to follow, not content to copy: the asset and values are invented. R-ab12cd34-1 is incident_context, -2 run_check, -3 attribute_anomaly, -4 get_telemetry.

```json
{
  "schema": "brief_v1",
  "brief_id": "B-ab12cd34-1",
  "incident_id": "INC-example-20260101T0000Z",
  "asset_id": "example",
  "evidence_state": "sufficient",
  "summary": "The compressor has run loaded without a rest and its oil is hot, the pattern of an air leak, and the data around the alarm are clean. Survey the air path for a leak.",
  "hypotheses": [
    {"id": "H1", "category": "process", "statement": "An air leak keeps the compressor loaded", "likelihood": "likely",
     "claims": [
       {"text": "The continuous-load check failed: no rest for 2.41 h against a limit of 0.19 h.", "cites": ["R-ab12cd34-2"]},
       {"text": "Oil was at 78.4 degC, above its limit of 70.85 degC.", "cites": ["R-ab12cd34-2"]},
       {"text": "Hours since the last rest carry 41 % of the anomaly score.", "cites": ["R-ab12cd34-3"]},
       {"text": "Motor current never fell below 5.5 A.", "cites": ["R-ab12cd34-4"]}],
     "against": [
       {"text": "The low-pressure check passed: 3 min in the past 24 h, under its limit of 28 min.", "cites": ["R-ab12cd34-2"]}]},
    {"id": "H2", "category": "data_quality", "statement": "Start-up after a recording gap", "likelihood": "unlikely",
     "claims": [{"text": "Data resumed after a gap before the alarm.", "cites": ["R-ab12cd34-2"]}],
     "against": [{"text": "The restart check passed: data resumed 3.2 h before the alarm.", "cites": ["R-ab12cd34-2"]}]}
  ],
  "prognosis": {"cites": ["R-ab12cd34-1"]},
  "recommended_action": {"type": "inspection", "text": "Survey the air path for a leak while it is pressurised; repair only after isolation and venting.", "cites": ["R-ab12cd34-2", "R-ab12cd34-4"]},
  "work_order": {"title": "Leak survey on the compressor air path", "asset": "example", "priority": "high",
    "tasks": ["Ultrasonic leak survey of fittings, hoses, valves and drains", "Pressure-decay test section by section", "After the repair, confirm normal cycling in the data"],
    "safety_notes": ["Isolate, lock out and vent to zero before loosening anything", "Let hot surfaces cool before work"],
    "status": "draft"},
  "checks_run": ["R-ab12cd34-2"],
  "versions": {"llm": "<as given>", "skills": {"<name>": "<version@sha12>"}, "profile": "<as given>"}
}
```
