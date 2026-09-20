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

Recorded gaps (the dataset has 331, up to days long) are capped at
`--max-wait` wall seconds per step so a replay never stalls; a capped gap
changes pacing only, not content.

Containerization: this runs as a host process against the composed broker
for now; it moves into `compose.yaml` as a service when ingest lands and
the compose network is worth having.
