# Node-RED test bed (optional, not run)

`flows.json` makes Node-RED the simulated plant for the Sparkplug B adapter.
It reads a slice of the telemetry CSV and publishes each row as a Sparkplug B
DDATA from edge node `scada_sim`, device `apu`, group `pdm_testbed`: the names
in the proposed `south.sparkplug` section (services/south/README.md). A second
inject node sets one value by hand, which is the build guide's check for
R2b.7: a value set in Node-RED reaches ingest.

`services/south/scada_sim.py` does the same job without Node-RED, and it is
what the tests use. This flow is for the demo and for checking the adapter
against a Sparkplug implementation other than our own.

**Not run.** The flow has not been imported into Node-RED. What was checked:
the JSON parses, every wire points at a node in the file, and the two
function-node bodies were run under Node.js with stand-ins for `msg`, `env`,
`context` and `node`. The Sparkplug side relies on
`node-red-contrib-mqtt-sparkplug-plus` (births, deaths, sequence numbers,
protobuf). The property names of its device node and of its
`mqtt-sparkplug-broker` config node, and its handling of `msg.definition` and
of per-metric `timestamp`, are written from its documentation; check them in
the editor after the import.

## The flow

```
[replay CSV slice] -> [read /data/replay.csv] -> [csv: one message per row] -> [row to Sparkplug metrics]
    -> [delay: 10 rows/s] -> [mqtt sparkplug device "apu"] -> [debug: commands received]
[set one value] -> [one column by hand] -> [mqtt sparkplug device "apu"]
```

- Each column other than the index and `timestamp` becomes a metric named
  `APU/<column>`, a Double, with the row's timestamp (the CSV is UTC). Set the
  environment variable `PDM_METRIC_PREFIX` in Node-RED to use another prefix;
  it must match the metric names in the profile.
- The first row also sends the metric definitions, which makes the device
  node publish its DBIRTH.
- The delay node paces the rows at 10 per second, about 100 times real time
  for 10 s rows. Change it there.
- The debug node shows any command a host sends to the device. PdM-Agent
  sends none.

## Running it

1. Start Node-RED 3 or 4, for example
   `docker run -d --name pdm-nodered -p 127.0.0.1:1880:1880 -v pdm_nodered:/data nodered/node-red:4.0.9`.
2. Install `node-red-contrib-mqtt-sparkplug-plus` (Menu, Manage palette,
   Install).
3. Put a CSV slice where the flow reads it. The whole file is 1.5 million rows;
   a few hours is plenty:

   ```bash
   CSV="data/MetroPT3(AirCompressor).csv"
   (head -1 "$CSV"; grep "^[0-9]*,2020-07-15 0[0-5]:" "$CSV") > /tmp/replay.csv
   docker cp /tmp/replay.csv pdm-nodered:/data/replay.csv
   ```

4. Import `flows.json` (Menu, Import). Open the config node "local broker":
   the broker is `localhost` when Node-RED runs on the host and the broker's
   compose service name (`mosquitto`) or `host.docker.internal` when it runs
   in a container. Group `pdm_testbed` and edge node `scada_sim` must match
   `south.sparkplug`, and the device node's name `apu` must match its
   `device`. Deploy.
5. Start ingest and the adapter (services/south/README.md), then click
   "replay CSV slice". `mosquitto_sub -t 'pdm/#' -v` shows the
   telemetry_json_v1 records.
6. The hand check: once the replay has finished, edit "set one value", for
   example `{"column": "<one of the CSV columns>", "value": 9.5}`, and click
   it. One record with that value, and every other signal at its last value,
   arrives on the telemetry topic. Its timestamp is the time of the click.

Run Node-RED or `scada_sim.py` as the plant, not both: they use the same edge
node name.
