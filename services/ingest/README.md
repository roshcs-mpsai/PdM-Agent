# Ingest

Validates telemetry, quarantines what fails, flags implausible values, builds
the 10-minute windows with their features, stores telemetry and windows, and
reconciles every replay (T3; ING-06 to ING-13).

```bash
python services/ingest/ingest.py --profile profiles/metropt3_apu/asset_profile.yaml \
    [--store "$PDM_STORE"] [--replay-manifest runs/metropt3_apu/replay_manifest.json]
```

Topics come from the profile: it reads `topics.telemetry` and writes
`topics.windows`. The store is `$PDM_STORE` (PostgreSQL/TimescaleDB in the
compose stack, SQLite at `runs/pdm.db` otherwise); `--no-store` publishes
windows only. Stop it with Ctrl-C: it stops handling messages, releases every
window the data allows, prints the reconciliation as one JSON line, appends it
to `runs/<asset_id>/reconciliation.jsonl`, writes the data-catalogue entry and
exits non-zero if the counts do not balance.

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
`seq` is counted as `lost`. `seq` 0 marks a new replay: everything the previous
replay left open is released, and the stream and the ordering checks start over. A replay whose
first message never arrives is not detected; its rows land as `stale_ts`.

## Out-of-range values (ING-07)

A value outside its signal's `range` in the profile (digital signals: anything
but 0 or 1) is not quarantined: an implausible reading is evidence of a sensor
fault (Journey C). The record is accepted and stored with `out_of_range:
{signal: value}`, and the value is left off the grid, so it never reaches a
score. One bad reading is bridged like a dropped sample; a run of them leaves
the windows incomplete. Each window reports how many flagged readings fell
inside it.

## Windows (ING-09 to ING-12)

`pdm_common.stream.StreamWindows` runs the batch pipeline on a rolling buffer:
freeze mask, 10 s grid with bridging, windows of `windowing.length` at a
`windowing.stride`, and the features declared in the profile's features file.
A window is released once later data cannot change it: about 30 s after it
ends (bridging reads two points ahead), longer while a run of identical rows
might still turn out to be a logger freeze, and only when data resumes if the
stream stops right after it. Complete windows are published and stored;
incomplete ones (a gap, a freeze, persistent bad values) are counted as
dropped. `tools/stream_check.py` replays the whole 2020 file through this
code and gets back all 239,172 windows of the evaluated table, every feature
within the table's six significant digits.

## Quarantine record

Appended to `runs/<asset_id>/quarantine.jsonl` (change with `--quarantine`)
and to the store's `quarantine` table:

```json
{"asset": "metropt3_apu", "error": "asset: Input should be 'metropt3_apu'", "error_code": "unknown_asset",
 "payload": "<raw message>", "received_at": "2026-09-30T19:49:59Z"}
```

`received_at` is wall-clock time and belongs to the log, not the data.

## Reconciliation (ING-08)

```json
{"accepted": 8640, "asset": "metropt3_apu", "balanced": true, "first_ts": "2020-04-17T00:00:00Z",
 "in": 8642, "last_ts": "2020-04-17T23:59:50Z", "lost": 0, "out_of_range_by_signal": {},
 "out_of_range_records": 0, "quarantined": 2, "quarantined_by_code": {"missing_signal": 1, "unknown_asset": 1},
 "replays": 1, "windows": 1431, "windows_complete": 1431, "windows_dropped": 0}
```

`balanced` means in = accepted + quarantined and windows = complete + dropped.
A jump in `seq` is counted as `lost`; a quarantined message is not lost.

## Data catalogue (ING-13)

At shutdown, the windows the run produced get one line in `data/catalogue.jsonl`
(`$PDM_CATALOGUE`): rows, columns, the replay manifest's SHA-256 and the lineage
(data file and replay log hashes from the manifest, profile, events and feature
declarations by hash). `tools/build_windows.py` records the batch tables the
same way, and the harness looks a processed folder up there before evaluating.

## Payload schema -- `window_v1`

One message per complete window, in time order. Canonical JSON (sorted keys,
no whitespace), stream time only.

| Field | Type | Meaning |
|---|---|---|
| `schema` | string | `window_v1` |
| `asset` | string | asset id from the profile |
| `profile_sha256` | string | SHA-256 of the profile file in force (CFG-07) |
| `features_sha256` | string | hash of what features.yaml computes (`pdm_common.frozen.features_sha256`); the detector refuses windows computed differently from its frozen inputs |
| `start`, `end` | string | window bounds, ISO 8601 UTC with `Z`; `end` is exclusive |
| `n_observed`, `n_bridged` | int | grid points observed and bridged (they sum to 60) |
| `out_of_range` | object | `{signal: readings flagged inside the window}`, usually empty |
| `features` | object | `{feature: value}` for every feature in features.yaml; `null` where undefined (e.g. no rest in the look-back) |
