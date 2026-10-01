# Ingest (stub)

Validates telemetry, quarantines what fails, and publishes one-minute windows
for the R0 walking skeleton (R0.3; ING-06 stub, ING-08). R1a.6 replaces the
windows with 10-minute windows on the masked, bridged 10 s grid and adds
out-of-range flags (ING-07) and TimescaleDB.

```bash
python services/ingest/ingest.py --profile profiles/metropt3_apu/asset_profile.yaml
```

Topics come from the profile: it reads `topics.telemetry` and writes
`topics.windows`. Stop it with Ctrl-C: it stops handling messages (any still
arriving are counted in the log, not processed), publishes the open window,
then prints the reconciliation as one JSON line and exits non-zero if the
counts do not balance. Every accepted sample is in a published window; if
the broker has already gone, the last window is logged as not published.

## Validation

A pydantic model is built from the profile at start-up, so a second asset
needs no code change. A message is quarantined, never dropped, with the
first matching code:

| Code | Meaning |
|---|---|
| `bad_json` | not a JSON object |
| `wrong_schema` | `schema` is not the profile's `topics.payload_schema` |
| `unknown_asset` | `asset` is not the profile's asset id |
| `bad_timestamp` | `ts` is not an ISO time |
| `bad_seq` | `seq` is not an integer >= 0 |
| `missing_signal` | a profile signal is absent |
| `non_numeric` | a value is not a finite number (strings, booleans, NaN, inf) |
| `unknown_signal` | a signal the profile does not declare |
| `unexpected_field` | any other top-level field |
| `duplicate_ts` | same timestamp as the last accepted message |
| `stale_ts` | earlier than the last accepted message |
| `internal_error` | the validator itself failed; the message is kept so the counts still balance |

Timestamps without an offset are read in the profile's `source_timezone`
(ING-09). `seq` is tracked for every message whose `seq` is readable, accepted
or not, so a quarantined message is never also counted as lost; a jump in
`seq` is counted as `lost`. `seq` 0 marks a new replay: the previous replay's
open window is published and the ordering checks reset. A replay whose
first message never arrives is not detected; its rows land as `stale_ts`.

## Quarantine record

Appended to `runs/<asset_id>/quarantine.jsonl` (change with `--quarantine`):

```json
{"asset": "metropt3_apu", "error": "asset: Input should be 'metropt3_apu'", "error_code": "unknown_asset",
 "payload": "<raw message>", "received_at": "2026-09-30T19:49:59Z"}
```

`received_at` is wall-clock time and belongs to the log, not the data.

## Reconciliation

```json
{"accepted": 300, "asset": "metropt3_apu", "balanced": true, "in": 302, "lost": 0,
 "quarantined": 2, "quarantined_by_code": {"missing_signal": 1, "unknown_asset": 1}, "windows": 50}
```

`balanced` means in = accepted + quarantined. Each closed window also logs
one line with the running counts.

## Payload schema -- `window_v1` (stub)

One message per closed one-minute bucket, epoch-aligned in UTC. Canonical
JSON (sorted keys, no whitespace), stream time only.

| Field | Type | Meaning |
|---|---|---|
| `schema` | string | `window_v1` |
| `asset` | string | asset id from the profile |
| `profile_sha256` | string | SHA-256 of the profile file in force (CFG-07) |
| `start`, `end` | string | bucket bounds, ISO 8601 UTC with `Z`; `end` is exclusive |
| `n` | int | samples in the bucket |
| `features` | object | `{input: mean}` for each of the profile's `detector.inputs` |

The R0 z-score fit feeds its training rows through the same
`pdm_common.windows.MinuteWindower`, so it is fitted on exactly what it scores.
