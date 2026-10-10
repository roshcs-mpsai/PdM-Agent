# Gate

Persistence gate, incident lifecycle and NE 107 status (T12b; INC-01 to INC-07).

```bash
python services/gate/gate.py --profile profiles/metropt3_apu/asset_profile.yaml [--store "$PDM_STORE"]
```

Reads `topics.scores`, `topics.windows`, `topics.prognosis` and `pdm/_sys/drift`;
writes `topics.incidents` and `topics.status` (retained), and the store's
`incidents`, `incident_events`, `gate_log` and `status_log` tables.

- **Gate (INC-01, INC-03, INC-04).** `pdm_common.gate.StreamingGate` is the
  harness's `gate_passes` and `incidents` one window at a time: a window passes
  when at least 80 % of the windows ending in the last 30 min alert and at least
  15 of them exist; a pass opens an incident unless the episode is still open
  (its last pass less than 6 h ago), in which case it attaches. Over the 2020
  campaign at the frozen threshold it opens exactly the 34 incidents the batch
  gate opens (tests/test_gate_service.py).
- **Decisions (INC-02).** `gate_log` gets one row per open, attach (passes
  resuming inside an episode), suppress (an alert run that ended without a
  pass) and close (the episode quiet for the hold-off), each with the gate
  parameters and the 30-minute score trajectory behind it.
- **Evidence.** On opening, the asset's checks run on the data as of the
  opening (`pdm_common.asof`, `checks/<asset>/`), and their results and the
  triage rules' verdicts are stored on the incident (`triage`). Nothing is
  dismissed: the triage policy is `none` (Oct 3 decision).
- **Lifecycle (INC-05, INC-06).** `pdm_common.incidents`: open -> diagnosing ->
  awaiting_review -> approved -> sent -> closed, or awaiting_review -> rejected
  -> closed. Each move is attributed and timestamped in `incident_events`
  (insert-only) and made by compare-and-set, so a duplicate trigger gets one
  agent run. The gate's episode closing does not change the state.
- **NE 107 (INC-07, INT-03).** `pdm_common.status.ne107` with the profile's
  `ne107` thresholds; a status_v1 goes out with every score, and every change is
  logged with its causes. No window for `no_data_after` (wall time) turns it to
  function check.

## Payload schema -- `incident_v1`

On `topics.incidents`, not retained. `event` is `opened` (the agent's trigger)
or `episode_closed`.

| Field | Meaning |
|---|---|
| `schema`, `asset`, `event` | `incident_v1`, asset id, what happened |
| `incident_id` | `INC-<asset>-<opening, UTC minute>`: deterministic, so a replay finds its own incidents |
| `at`, `as_of` | when it happened (stream time); the opening, which every tool reads as of |
| `gate`, `trajectory` | the gate parameters and the scores of the last 30 min |
| `cause`, `checks` | on `opened`: why it opened and each check's id, name, kind and result |
| `detector_run_id`, `profile_sha256` | the versions in force |

## Payload schema -- `status_v1`

On `topics.status`, **retained**, one per score.

| Field | Meaning |
|---|---|
| `schema`, `asset`, `ts` | `status_v1`, asset id, the latest window's end |
| `ne107`, `causes` | `good`, `maintenance_required`, `out_of_specification`, `function_check` or `failure`, and every condition behind it |
| `score`, `threshold`, `alert`, `detector_run_id` | from the latest score |
| `incident_id`, `incident_state`, `episode_open` | the incident not yet closed, if any |
| `prognosis` | the latest prognosis_v1 horizons, if any |
