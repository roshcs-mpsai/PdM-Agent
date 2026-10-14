# Southbound adapters (ING-14, ING-15)

Plant tags in, `telemetry_json_v1` out. Two adapters subscribe to a plant
source, map its tags to the asset profile's signals through the profile's
`south` section, and publish on the profile's telemetry topic. From there
ingest takes over exactly as it does for the replayer. Neither adapter has a
write path to the plant. A simulated SCADA plays the plant for the tests and
the demo, and `tools/workorder_sim.py` plays the downstream work-order system
for the REST connector.

| File | What it is |
|---|---|
| `sparkplug.py` | Sparkplug B 3.0 adapter: hand-written protobuf codec, topic parser, birth and death tracking, the publish guard, its MQTT loop |
| `opcua_client.py` | OPC UA adapter: the interval mapper, read-only wrappers around the asyncua client, its main |
| `scada_sim.py` | Simulated SCADA: the replayer's CSV served as Sparkplug B and, optionally, from an OPC UA server |
| `nodered/` | Optional Node-RED flow for the same test bed (not run) |
| `cip.py`, `CIP.md` | The read-only CIP device reader (T23), a separate component documented in `CIP.md` |
| `tools/workorder_sim.py` | Simulated work-order REST endpoint |
| `tests/test_south.py`, `tests/test_workorder_sim.py` | The tests |

## The profile's `south` section (proposed)

Schema 0.3 already defines `south` (`pdm_common.schema.South`); the MetroPT-3
profile does not have one yet. This is the section the test bed is built for.
Until it lands in the profile, `tests/test_south.py` appends this block to the
profile and validates it; afterwards it checks the profile's own.

```yaml
south:                         # ING-14: southbound sources -> profile signals (services/south)
  opcua:
    endpoint: "opc.tcp://localhost:4841/scada-sim/"   # the simulated SCADA (4840 is opcua_north)
    publishing_interval: 10s                          # one record per interval of source time
    tags:                                             # OPC UA node id -> signal
      "ns=2;s=APU.TP2": TP2
      "ns=2;s=APU.TP3": TP3
      "ns=2;s=APU.H1": H1
      "ns=2;s=APU.DV_pressure": DV_pressure
      "ns=2;s=APU.Reservoirs": Reservoirs
      "ns=2;s=APU.Oil_temperature": Oil_temperature
      "ns=2;s=APU.Motor_current": Motor_current
      "ns=2;s=APU.COMP": COMP
      "ns=2;s=APU.DV_eletric": DV_eletric
      "ns=2;s=APU.Towers": Towers
      "ns=2;s=APU.MPG": MPG
      "ns=2;s=APU.LPS": LPS
      "ns=2;s=APU.Pressure_switch": Pressure_switch
      "ns=2;s=APU.Oil_level": Oil_level
      "ns=2;s=APU.Caudal_impulses": Caudal_impulses
  sparkplug:
    group: pdm_testbed
    edge_node: scada_sim
    device: apu
    metrics:                                          # Sparkplug metric name -> signal
      APU/TP2: TP2
      APU/TP3: TP3
      APU/H1: H1
      APU/DV_pressure: DV_pressure
      APU/Reservoirs: Reservoirs
      APU/Oil_temperature: Oil_temperature
      APU/Motor_current: Motor_current
      APU/COMP: COMP
      APU/DV_eletric: DV_eletric
      APU/Towers: Towers
      APU/MPG: MPG
      APU/LPS: LPS
      APU/Pressure_switch: Pressure_switch
      APU/Oil_level: Oil_level
      APU/Caudal_impulses: Caudal_impulses
```

Every signal must be mapped exactly once, or the adapter refuses to start and
names the signals: ingest wants every signal in every record. A real plant
gets its own tag names here; nothing in the code knows them.

## Sparkplug B adapter (`sparkplug.py`)

Subscribes to `spBv1.0/<group>/#` and follows the edge node and device named
in `south.sparkplug`.

- **Births.** NBIRTH starts a session (aliases, bdSeq, sequence numbers).
  DBIRTH declares the device's metrics with names, aliases and datatypes, and
  its values become each signal's last value: Sparkplug reports by exception,
  so DATA messages carry only what changed.
- **Records.** One record per DDATA that updated a mapped metric, and only
  once every signal has a value. A birth is never a record. `ts` is the latest
  metric timestamp in the message (the payload timestamp when the metrics have
  none), in UTC with a `Z`; `seq` counts records from 0 for each run of the
  adapter.
- **Deaths.** A DDEATH, or an NDEATH whose bdSeq matches the NBIRTH, forgets
  the device's values; nothing is published until the next DBIRTH. For NE 107
  that is "no data", which the gate turns into function check after
  `ne107.no_data_after`. An NDEATH with another bdSeq (a will from an earlier
  session, delivered late) is ignored.
- **Counted, never fatal:** other edge nodes and devices, NDATA (the mapping
  covers device metrics), commands sent by other hosts, STATE, unknown aliases
  and metrics, historical (store-and-forward) values, sequence gaps and
  malformed payloads. A null or non-numeric value clears its signal, so
  records stop until it has a value again. The counts print on exit.

Limits to know about:

- DATA that arrives before any birth is ignored. A Sparkplug host would ask
  for a rebirth, but that is an NCMD and this adapter sends none. Start the
  adapter before the edge node, or have the edge node rebirth (restart it).
- State survives a broker reconnect, but births and deaths sent while the
  adapter was disconnected are missed (persistent sessions come with R3
  hardening).
- With a source that reports by exception, records come only when a mapped
  metric changes, and ingest bridges gaps of 30 s at most. The source should
  publish its metrics at the sampling interval, as the test bed does.

It has its own MQTT loop instead of `pdm_common.mqtt.run_service`, for two
reasons: `run_service` dispatches on exact topics and passes handlers only the
payload, while the adapter needs the topic of every message under
`spBv1.0/<group>/#`; and `run_service` connects without credentials, while the
read-only broker account is part of ING-15. Otherwise it follows `run_service`
(subscribe on connect, one message at a time, a bad message never stops it).

```bash
P=profiles/metropt3_apu/asset_profile.yaml          # with the south section
export PDM_SOUTH_MQTT_USER=... PDM_SOUTH_MQTT_PASSWORD=...   # only once the broker has accounts (R3)
python services/south/sparkplug.py --profile $P
```

## OPC UA adapter (`opcua_client.py`)

Connects to `south.opcua.endpoint` as the user in `PDM_SOUTH_OPCUA_USER` /
`PDM_SOUTH_OPCUA_PASSWORD`, subscribes to every node in `south.opcua.tags` and
publishes one record per publishing interval of source time:

- Intervals are aligned to the epoch, like ingest's 10 s grid. A record
  carries every signal's last value (OPC UA notifies changes only, so an
  unchanged signal keeps its value); its `ts` is the latest source timestamp
  in the interval (the server timestamp when a value has no source time).
- An interval is closed when a notification arrives from two intervals later
  (`--lateness 1`: one interval of slack, because a publish response can
  deliver a batch tag by tag rather than in time order), after two quiet
  intervals of wall time, or at shutdown. A notification for an interval
  already closed is counted as late and dropped.
- A Bad status or a non-numeric value clears its signal, like a Sparkplug
  null.

The other option, a record whenever every signal has changed since the last
one, was not taken: it stalls on any signal that holds its value. The cost of
this one is that two samples in one interval give one record (the later one).
With 9-10 s steps and a 10 s interval that happens now and then, where ingest
would have averaged the two on its grid.

`ReadOnlyClient` is the only handle the adapter keeps after connecting. It
offers connect, disconnect, `get_node`, `read_values`, the namespace lookups
and `create_subscription`. Nodes come back as `ReadOnlyNode` (reads and
browsing; browsed nodes are wrapped too) and the subscription as
`ReadOnlySubscription` (`subscribe_data_change`, `unsubscribe`, `delete`).

```bash
python services/south/opcua_client.py --profile $P            # --endpoint, --queue, --sampling-ms, --lateness
```

## Read-only guarantees (ING-15)

| Path | In the wrapper (tested here) | In configuration (proposed, not enforced yet) |
|---|---|---|
| Sparkplug B | Every publish goes through `GuardedPublisher`, which calls `guard_publish` first: NCMD, DCMD, STATE, anything under `spBv1.0/` and any topic other than the telemetry topic raise `CommandRefused` before the MQTT client sees them. A profile whose telemetry topic lies in `spBv1.0/` is refused at start-up. | A broker account that can read `spBv1.0/<group>/#` and write the telemetry topic only (ACL below). |
| OPC UA | Writing a value or attribute, calling a method, adding or deleting nodes or references, and the raw `uaclient`, `nodes` and `session` handles raise `WriteRefused` before the asyncua object is touched. The wrappers are allowlists, so anything not listed is refused the same way. | A server user with Browse, Read and Subscribe rights only. The simulated server has no client-writable variable and no method. |

The negative tests, in `tests/test_south.py`:

- `test_publishing_a_command_raises_before_the_client`: a DCMD, an NCMD and a
  STATE publish raise and the fake MQTT client records nothing;
- `test_the_guard_refuses_commands_and_every_other_topic`;
- `test_the_adapter_only_ever_returns_telemetry`: commands from another host
  arrive, and every message the adapter returns is telemetry;
- `test_a_telemetry_topic_in_the_sparkplug_namespace_is_refused`;
- `test_the_read_only_client_refuses_writes_before_asyncua_sees_them`: each
  refused operation on the client, a node, a browsed node and the subscription
  raises, and the fake asyncua client records no call at all.

Broker ACL for when Mosquitto gets accounts (R3 hardening), one account per side:

```text
user pdm-south                  # the adapters: read the plant, write telemetry only
topic read spBv1.0/pdm_testbed/#
topic write pdm/metropt3_apu/telemetry

user scada-sim                  # the simulated plant
topic write spBv1.0/pdm_testbed/#
```

## Simulated SCADA (`scada_sim.py`)

Reads the replayer's CSV and serves the rows as the edge node and device in
`south.sparkplug`: NBIRTH, DBIRTH (names, aliases, datatypes, the first row's
values), one DDATA per row by alias, then DDEATH and NDEATH (also the MQTT
will). Analog signals are Doubles, so the adapters give back the replayer's
values bit for bit; digital signals are Booleans, and a digital value other
than 0 or 1 stops it. Births and data go out at QoS 0 as Sparkplug B
specifies; use `--qos 1` for fast replays on a busy broker. With `--opcua` it
also runs an OPC UA server (asyncua) with one variable per signal at the
node ids in `south.opcua.tags`, written with each row and the row's timestamp
as source time.

```bash
docker compose up -d mosquitto
python services/ingest/ingest.py --profile $P
python services/south/sparkplug.py --profile $P
python services/south/scada_sim.py --profile $P --data "data/MetroPT3(AirCompressor).csv" \
    --speed 100 --from 2020-07-14T12:00:00 --to 2020-07-15T20:00:00

# OPC UA instead: the client samples every 10 s of wall time, so serve at 1x
python services/south/opcua_client.py --profile $P
python services/south/scada_sim.py --profile $P --data ... --opcua --no-sparkplug --speed 1

# no broker: build the messages and log them as "<topic> <payload hex>"
python services/south/scada_sim.py --profile $P --data ... --dry-run --limit 100 --log /tmp/sim.log --speed 1e9
```

Feed ingest from one source at a time (the replayer, or the simulated SCADA
through one adapter): ingest wants one stream in time order. Restart the
adapter between two replays of the same period; its `seq` carries on and
ingest would read the second pass as stale. For OPC UA faster than 1x, use
`--sampling-ms 0 --queue N --lateness N` on the client with N at least the
speed, so every change is queued and the batches fit the slack.

## Work-order simulator (`tools/workorder_sim.py`)

The contract the REST connector is built against (INT-04 to INT-06):

| Request | Answer |
|---|---|
| `POST /api/work-orders`, `Authorization: Bearer <token>`, `Idempotency-Key: <approval_id>`, JSON body | 201 `{"reference": "WO-SIM-0001", "received_at": "<UTC ISO>"}` the first time |
| the same key and the same body again | 200, the same reference and `received_at`, header `Idempotent-Replayed: true` |
| the same key, a different body | 409 |
| no token, or the wrong one | 401 |
| no Idempotency-Key, a missing or mistyped field, or a key other than the body's `approval_id` | 422 |
| the first N POSTs with `WORKORDER_SIM_FAIL_FIRST=N` | 503, nothing stored |
| `GET /api/work-orders` (token needed) | `{"orders": [...]}` in the order received |
| `GET /health` | 200 `{"status": "ok"}`, no token |

The body needs `approval_id`, `wo_id`, `incident_id`, `asset_id`, `title`,
`priority`, `tasks` and `safety_notes` (lists of strings), `text_sha256` (64
lowercase hex digits), `approved_by` and `approved_at` (ISO 8601 with an
offset or `Z`); other fields are kept as sent. The token comes from
`WORKORDER_SIM_TOKEN` (default `sim-token`); `WORKORDER_SIM_STORE` keeps the
orders in a JSON file across restarts.

```bash
python tools/workorder_sim.py --port 8088                  # --fail-first N, --store FILE
uvicorn --app-dir tools --factory workorder_sim:create_app --port 8088
curl -s localhost:8088/api/work-orders -H "Authorization: Bearer sim-token"
```

## Tests

```bash
python -m pytest -q tests/test_south.py tests/test_workorder_sim.py
```

No broker, no OPC UA stack and no dataset needed. The round trips run the
same synthetic CSV through the replayer and through the simulated SCADA plus
each adapter, compare the records value by value, and feed them to ingest,
which accepts all of them. The codec test against Google's protobuf runtime
runs only where `protobuf` is installed.

## Not verified here

- Nothing here has talked to a broker or Node-RED. The `main()` functions of
  the three modules (except the simulator's `--dry-run`) and the Node-RED flow
  have not been run.
- The OPC UA path has run against asyncua 2.1.0:
  `test_csv_through_a_live_opcua_server_matches_the_replayer` starts the
  simulator's OPC UA server, subscribes with the read-only client and its
  handler (`create_subscription`, `subscribe_data_change(queuesize=,
  sampling_interval=)`, the data-change fields, `write_value` with a
  `SourceTimestamp`), and gets the replayer's records. The login
  (`set_user`/`set_password`) has not run, and asyncua 1.x has not been tried.
- The codec matches Google's protobuf runtime on the Sparkplug B subset it
  implements; it has not been tried against Eclipse Tahu or a real edge node.
- The broker ACL and the OPC UA read-only user are proposals; nothing
  enforces them yet.
