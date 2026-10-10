# Replayer

Deterministic stream replay of an asset's telemetry CSV to MQTT (PR-01, R0).
One JSON message per recorded row, published to the asset profile's telemetry
topic at a configurable multiple of recorded time.

## Topic namespace

The topic comes from the asset profile, never from code:

```
pdm/<asset_id>/telemetry        # e.g. pdm/metropt3_apu/telemetry
```

## Payload schema — `telemetry_json_v1`

One message per CSV row. Canonical JSON (sorted keys, no whitespace), so a
message log is byte-comparable across runs.

| Field     | Type   | Meaning                                                        |
|-----------|--------|----------------------------------------------------------------|
| `schema`  | string | Payload schema id from the profile (`telemetry_json_v1`)       |
| `asset`   | string | Asset id from the profile                                      |
| `seq`     | int    | Message sequence number within this replay, starting at 0      |
| `ts`      | string | Source timestamp of the row, ISO 8601, exactly as recorded     |
| `signals` | object | `{signal_name: numeric value}` for every profile signal        |

`ts` is the recorded timestamp, not the wall clock — alignment to a shared
UTC time base is the ingest service's job (PR-03). Digital channels are
published as recorded (0.0/1.0); polarity interpretation also belongs
downstream (the profile marks three channels suspect-inverted).

## Determinism (PR-01 acceptance)

Rows are published in file order and payloads are built only from file
contents, so the same file and arguments produce the same message sequence.
There is no random element, hence no seed. To verify: run twice with
`--log` and diff the logs — `tests/test_replayer.py` does exactly this.
Pacing (`--speed`, `--max-wait`) affects only wall-clock timing, never
message content.

## Running it

Data first: the EDA notebook (`notebooks/metropt3_eda.ipynb`) downloads
MetroPT-3 from the UCI archive on first run and leaves the CSV under
`data/` (gitignored). Broker: `docker compose up -d` starts Mosquitto
on `localhost:1883`.

```bash
# real time
python services/replayer/replayer.py \
  --profile profiles/metropt3_apu/asset_profile.yaml \
  --data "data/MetroPT3(AirCompressor).csv"

# 10x, first failure window only (F1 ± guard band)
python services/replayer/replayer.py \
  --profile profiles/metropt3_apu/asset_profile.yaml \
  --data "data/MetroPT3(AirCompressor).csv" \
  --speed 10 --from 2020-04-17T00:00:00 --to 2020-04-19T12:00:00

# smoke test without a broker
python services/replayer/replayer.py \
  --profile profiles/metropt3_apu/asset_profile.yaml \
  --data "data/MetroPT3(AirCompressor).csv" \
  --dry-run --limit 100 --speed 1000000 --log /tmp/replay.log

# watch it from another terminal
mosquitto_sub -t 'pdm/#' -v
```

## Replay manifest (ING-05)

Every run writes `runs/<asset_id>/replay_manifest.json` (change with
`--manifest`): the data file's name and SHA-256, the profile and events
hashes, the arguments, the rows read, published, dropped and filtered, the
first and last timestamps, and the SHA-256 of the message log. The log hash is
computed whether or not `--log` is given, so two replays can be compared by
manifest alone; only `written_at` differs between identical replays. Ingest
takes the manifest (`--replay-manifest`) for the lineage of its catalogue entry.

## Injected faults (`--inject`, Journey C and the ingest tests)

A faults file applies deterministic faults at recorded timestamps; the file's
name, hash and how often each fault fired go into the manifest. Signal names
come from the profile, so faults files live with the asset
(`profiles/<asset_id>/faults/`).

```yaml
schema: faults_v1
faults:
  - {kind: offset, signal: TP3, from: "2020-06-20T10:00:00", to: "2020-06-20T22:00:00", value: 0.6}
  - {kind: drift,  signal: TP3, from: "...", to: "...", rate_per_h: 0.05}
  - {kind: stuck,  signal: TP3, from: "...", to: "..."}        # holds the first affected value
  - {kind: set,    signal: Oil_temperature, at: "...", value: 250}   # one reading (out of range)
  - {kind: drop,   from: "...", to: "..."}                      # rows not sent; seq still advances
  - {kind: corrupt, at: "...", mode: bad_json}   # or wrong_schema, wrong_asset, missing_signal,
                                                 # non_numeric (with signal), duplicate, stale
```

Recorded gaps (the dataset has 331, up to days long) are capped at
`--max-wait` wall seconds per step so a replay never stalls; a capped gap
changes pacing only, not content.

Containerization: this runs as a host process against the composed broker
for now; it moves into `compose.yaml` as a service when ingest lands and
the compose network is worth having.
