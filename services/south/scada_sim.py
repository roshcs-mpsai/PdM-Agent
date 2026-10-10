"""Simulated SCADA test bed -- the telemetry CSV as Sparkplug B and OPC UA (ING-14, PR-17).

Plays the plant for the southbound adapters. It reads the replayer's CSV
(index, timestamp, one column per profile signal) and serves the rows as:

* Sparkplug B 3.0 over MQTT, as the edge node and device named in the
  profile's ``south.sparkplug`` section: NBIRTH (bdSeq, Node Control/Rebirth),
  DBIRTH (names, aliases, datatypes and the first row's values), one DDATA per
  row (by alias, the row's timestamp on every metric), and DDEATH and NDEATH
  at the end. The NDEATH is also registered as the MQTT will. Births and data
  go out at QoS 0, as Sparkplug B specifies (--qos 1 for lossless fast
  replays); the will at QoS 1.
* optionally (--opcua) an OPC UA server with one variable per signal at the
  node ids of ``south.opcua.tags``, written with each row's values and the
  row's timestamp as source timestamp. No variable is writable by clients and
  the server has no methods.

Analog signals are Doubles, so the adapters give back the replayer's values
bit for bit; digital signals are Booleans, and a digital value other than 0
or 1 stops the simulation. Rows are paced like the replayer (--speed,
--max-wait). The timestamps in the CSV are read in the profile's
source_timezone.

The message building is pure (EdgeNodeSim, sparkplug_messages,
opcua_updates): tests/test_south.py runs CSV -> these messages -> the
adapters -> telemetry_json_v1 and compares the result with the replayer's
messages, without a broker. main() imports paho-mqtt (and asyncua with
--opcua) when it runs; neither is installed where this was written, so main()
has only been run with --dry-run.

    python services/south/scada_sim.py --profile profiles/metropt3_apu/asset_profile.yaml \\
        --data "data/MetroPT3(AirCompressor).csv" --speed 100 --from 2020-07-14T12:00:00 --to 2020-07-15T20:00:00
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import logging
import os
import sys
from collections import Counter
from datetime import datetime
from typing import Callable, Iterable, Iterator, Optional

from pdm_common import broker
from pdm_common.profile import ProfileError, digital_names, load_profile, signal_names
from pdm_common.timeutil import to_utc

if __package__:
    from .sparkplug import (BD_SEQ, BOOLEAN, DOUBLE, INT64, REBIRTH, VALUE_FIELD, Metric, Payload, SouthConfigError,
                            encode_payload, make_topic, mqtt_client, ms_of, signal_map, south_section)
else:                                         # run as a script: python services/south/scada_sim.py
    from sparkplug import (BD_SEQ, BOOLEAN, DOUBLE, INT64, REBIRTH, VALUE_FIELD, Metric, Payload, SouthConfigError,
                           encode_payload, make_topic, mqtt_client, ms_of, signal_map, south_section)

log = logging.getLogger("south.scada_sim")


class CsvError(ValueError):
    """The telemetry CSV does not match the profile."""


def read_rows(profile: dict, path, *, from_ts: Optional[datetime] = None, to_ts: Optional[datetime] = None,
              limit: Optional[int] = None, counts: Optional[Counter] = None) -> Iterator[tuple[datetime, dict]]:
    """(timestamp, {signal: value}) per row, parsed as the replayer parses them.
    Unparseable rows are skipped and counted in ``counts``."""
    signals = signal_names(profile)
    counts = counts if counts is not None else Counter()
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in ["timestamp", *signals] if c not in (reader.fieldnames or [])]
        if missing:
            raise CsvError(f"{path}: missing expected column(s): {', '.join(missing)}")
        served = 0
        for lineno, row in enumerate(reader, start=2):
            counts["read"] += 1
            try:
                ts = datetime.fromisoformat(row["timestamp"])
                values = {s: float(row[s]) for s in signals}
            except (ValueError, TypeError):
                counts["skipped"] += 1
                log.warning("skipping unparseable row at line %d", lineno)
                continue
            if (from_ts and ts < from_ts) or (to_ts and ts > to_ts):
                counts["filtered"] += 1
                continue
            yield ts, values
            served += 1
            if limit and served >= limit:
                return


def device_value(name: str, value: float, digital: bool):
    """What the simulated device sends: a float, or a Boolean for a digital signal."""
    if not digital:
        return float(value)
    if value not in (0, 1):
        raise ValueError(f"digital signal {name} reads {value!r}; the simulated device sends Booleans (0 or 1)")
    return bool(value)


class EdgeNodeSim:
    """The simulated edge node and its device, named by the profile's south.sparkplug.

    Every method returns (topic, payload bytes). Sequence numbers run 0-255
    from the NBIRTH, as Sparkplug B requires; aliases are 1..n in profile
    signal order.
    """

    def __init__(self, profile: dict, *, bd_seq: int = 0):
        cfg = south_section(profile, "sparkplug")
        metric_of = {s: m for m, s in signal_map(profile, cfg.get("metrics"), "south.sparkplug.metrics").items()}
        self.group, self.edge_node, self.device = cfg["group"], cfg["edge_node"], cfg["device"]
        self.tz = profile.get("source_timezone", "UTC")
        digital = set(digital_names(profile))
        self.metrics = [(s, metric_of[s], BOOLEAN if s in digital else DOUBLE, alias)
                        for alias, s in enumerate(signal_names(profile), start=1)]
        self.bd_seq = bd_seq % 256
        self._seq = 0

    def topic(self, msg_type: str) -> str:
        return make_topic(self.group, msg_type, self.edge_node, self.device if msg_type[0] == "D" else None)

    def _ms(self, ts) -> int:
        return ms_of(to_utc(ts, tz=self.tz))

    def _next_seq(self) -> int:
        seq, self._seq = self._seq, (self._seq + 1) % 256
        return seq

    def node_birth(self, ts) -> tuple[str, bytes]:
        self._seq = 0
        ms = self._ms(ts)
        metrics = [Metric(name=BD_SEQ, timestamp=ms, datatype=INT64, value=self.bd_seq),
                   Metric(name=REBIRTH, timestamp=ms, datatype=BOOLEAN, value=False)]
        return self.topic("NBIRTH"), encode_payload(Payload(timestamp=ms, seq=self._next_seq(), metrics=metrics))

    def device_birth(self, ts, values: dict) -> tuple[str, bytes]:
        ms = self._ms(ts)
        metrics = [Metric(name=name, alias=alias, timestamp=ms, datatype=dtype,
                          value=device_value(s, values[s], dtype == BOOLEAN))
                   for s, name, dtype, alias in self.metrics]
        return self.topic("DBIRTH"), encode_payload(Payload(timestamp=ms, seq=self._next_seq(), metrics=metrics))

    def device_data(self, ts, values: dict) -> tuple[str, bytes]:
        """One row: every metric by alias, without name or datatype, as DATA messages carry them."""
        ms = self._ms(ts)
        metrics = [Metric(alias=alias, timestamp=ms, value=device_value(s, values[s], dtype == BOOLEAN),
                          value_field=VALUE_FIELD[dtype])
                   for s, _, dtype, alias in self.metrics]
        return self.topic("DDATA"), encode_payload(Payload(timestamp=ms, seq=self._next_seq(), metrics=metrics))

    def device_death(self, ts) -> tuple[str, bytes]:
        return self.topic("DDEATH"), encode_payload(Payload(timestamp=self._ms(ts), seq=self._next_seq()))

    def node_death(self) -> tuple[str, bytes]:
        """Also the MQTT will, registered before connecting: bdSeq only, no timestamp or seq."""
        return self.topic("NDEATH"), encode_payload(Payload(metrics=[Metric(name=BD_SEQ, datatype=INT64,
                                                                            value=self.bd_seq)]))


def sparkplug_messages(profile: dict, rows: Iterable[tuple[datetime, dict]], *, bd_seq: int = 0,
                       death: bool = False) -> list[tuple[str, bytes]]:
    """A whole session as (topic, payload) pairs: NBIRTH, DBIRTH with the first
    row's values, one DDATA per row and, with ``death``, DDEATH and NDEATH."""
    sim = EdgeNodeSim(profile, bd_seq=bd_seq)
    out: list[tuple[str, bytes]] = []
    last = None
    for ts, values in rows:
        if last is None:
            out += [sim.node_birth(ts), sim.device_birth(ts, values)]
        out.append(sim.device_data(ts, values))
        last = ts
    if death and last is not None:
        out += [sim.device_death(last), sim.node_death()]
    return out


def opcua_updates(profile: dict, rows: Iterable[tuple[datetime, dict]]) -> list[tuple[str, object, dt.datetime]]:
    """(node id, value, source timestamp) for every signal of every row, in the
    order the simulated server writes them."""
    tags = signal_map(profile, south_section(profile, "opcua").get("tags"), "south.opcua.tags")
    node_of = {s: n for n, s in tags.items()}
    tz = profile.get("source_timezone", "UTC")
    digital = set(digital_names(profile))
    updates = []
    for ts, values in rows:
        moment = to_utc(ts, tz=tz)
        for s in signal_names(profile):
            updates.append((node_of[s], device_value(s, values[s], s in digital), moment))
    return updates


def step_wait(prev: Optional[datetime], ts: datetime, speed: float, max_wait: float) -> float:
    """As the replayer paces: recorded gaps at 1/speed, never more than max_wait wall seconds."""
    if prev is None:
        return 0.0
    delta = (ts - prev).total_seconds()
    return 0.0 if delta <= 0 else min(delta / speed, max_wait)


class OpcUaServerSim:
    """One variable per profile signal at the node ids of south.opcua.tags.
    Needs asyncua (imported in start()); not run here."""

    def __init__(self, profile: dict, endpoint: Optional[str] = None):
        cfg = south_section(profile, "opcua")
        self.endpoint = endpoint or cfg["endpoint"]
        self.tags = signal_map(profile, cfg.get("tags"), "south.opcua.tags")
        self.digital = set(digital_names(profile))
        self.tz = profile.get("source_timezone", "UTC")
        self.server = None
        self.variables: dict = {}             # signal -> (node, variant type)
        self._ua = None

    async def start(self) -> None:
        from asyncua import Server, ua

        self._ua = ua
        server = Server()
        await server.init()
        server.set_endpoint(self.endpoint)
        server.set_server_name("PdM-Agent simulated SCADA")
        ids = {node_id: ua.NodeId.from_string(node_id) for node_id in self.tags}
        if any(nid.NamespaceIndex == 0 for nid in ids.values()):
            raise SouthConfigError("south.opcua.tags: the simulated server cannot create nodes in namespace 0")
        need = max(nid.NamespaceIndex for nid in ids.values())
        while len(await server.get_namespace_array()) <= need:
            await server.register_namespace(f"urn:pdm-agent:scada-sim:{len(await server.get_namespace_array())}")
        folder = await server.nodes.objects.add_folder(need, "ScadaSim")
        for node_id, name in self.tags.items():
            vtype = ua.VariantType.Boolean if name in self.digital else ua.VariantType.Double
            node = await folder.add_variable(ids[node_id], ua.QualifiedName(name, ids[node_id].NamespaceIndex),
                                             False if name in self.digital else 0.0, varianttype=vtype)
            self.variables[name] = (node, vtype)        # never set_writable(): clients cannot write
        await server.start()
        self.server = server
        log.info("OPC UA server on %s: %d read-only variables", self.endpoint, len(self.variables))

    async def write_row(self, ts, values: dict) -> None:
        ua = self._ua
        moment = to_utc(ts, tz=self.tz)
        now = dt.datetime.now(dt.timezone.utc)             # server time: wall clock, as a server stamps it
        for name, (node, vtype) in self.variables.items():
            value = device_value(name, values[name], name in self.digital)
            await node.write_value(ua.DataValue(Value=ua.Variant(value, vtype), SourceTimestamp=moment,
                                                ServerTimestamp=now))

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.stop()
            self.server = None


async def simulate(profile: dict, rows: Iterable[tuple[datetime, dict]], *,
                   publish: Optional[Callable[[str, bytes], None]] = None, server: Optional[OpcUaServerSim] = None,
                   sparkplug: bool = True, speed: float = 1.0, max_wait: float = 60.0, bd_seq: int = 0,
                   log_file=None, counts: Optional[Counter] = None) -> Counter:
    """Serve the rows, paced; return counts. ``publish(topic, payload)`` sends a
    Sparkplug message (None: log only); ``server`` is the OPC UA server or None."""
    counts = counts if counts is not None else Counter()
    sim = EdgeNodeSim(profile, bd_seq=bd_seq) if sparkplug else None

    def send(message: tuple[str, bytes]) -> None:
        topic, payload = message
        if publish is not None:
            publish(topic, payload)
        if log_file is not None:
            log_file.write(f"{topic} {payload.hex()}\n")
        counts["messages"] += 1

    prev = None
    for ts, values in rows:
        if prev is None and sim is not None:
            send(sim.node_birth(ts))
            send(sim.device_birth(ts, values))
        wait = step_wait(prev, ts, speed, max_wait)
        if wait > 0:
            await asyncio.sleep(wait)
        if sim is not None:
            send(sim.device_data(ts, values))
        if server is not None:
            await server.write_row(ts, values)
        counts["rows"] += 1
        prev = ts
    if sim is not None and prev is not None:
        send(sim.device_death(prev))
        send(sim.node_death())
    return counts


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile", required=True, help="asset profile YAML with a south section")
    p.add_argument("--data", required=True, help="telemetry CSV (the replayer's format)")
    p.add_argument("--broker", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--qos", type=int, default=0, choices=[0, 1],
                   help="QoS of births and data (Sparkplug B: 0; 1 for lossless fast replays)")
    p.add_argument("--speed", type=float, default=1.0, help="multiple of recorded time")
    p.add_argument("--from", dest="from_ts", type=datetime.fromisoformat, default=None, metavar="ISO")
    p.add_argument("--to", dest="to_ts", type=datetime.fromisoformat, default=None, metavar="ISO")
    p.add_argument("--limit", type=int, default=None, help="stop after N rows")
    p.add_argument("--max-wait", type=float, default=60.0, help="cap on wall seconds waited per step")
    p.add_argument("--bd-seq", type=int, default=0, help="birth-death sequence of this session (0-255)")
    p.add_argument("--no-sparkplug", action="store_true", help="serve OPC UA only")
    p.add_argument("--opcua", action="store_true", help="also serve the rows from an OPC UA server (asyncua)")
    p.add_argument("--opcua-endpoint", default=None, help="default: south.opcua.endpoint")
    p.add_argument("--dry-run", action="store_true", help="no broker and no server: build and log only")
    p.add_argument("--log", default=None, help="write '<topic> <payload hex>' lines here")
    args = p.parse_args(argv)
    if args.speed <= 0:
        p.error("--speed must be positive")
    if args.no_sparkplug and not args.opcua:
        p.error("--no-sparkplug needs --opcua")
    return args


async def _serve(args, profile, rows, publish, log_file, served: Counter) -> Counter:
    server = None
    if args.opcua and not args.dry_run:
        server = OpcUaServerSim(profile, args.opcua_endpoint)
        await server.start()
    try:
        return await simulate(profile, rows, publish=publish, server=server, sparkplug=not args.no_sparkplug,
                              speed=args.speed, max_wait=args.max_wait, bd_seq=args.bd_seq, log_file=log_file,
                              counts=served)
    finally:
        if server is not None:
            await server.stop()


def main(argv=None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        profile = load_profile(args.profile)
        edge = EdgeNodeSim(profile, bd_seq=args.bd_seq) if not args.no_sparkplug else None
        if args.opcua:
            OpcUaServerSim(profile, args.opcua_endpoint)
    except (ProfileError, SouthConfigError) as exc:
        raise SystemExit(f"scada_sim: {exc}")
    counts: Counter = Counter()
    rows = read_rows(profile, args.data, from_ts=args.from_ts, to_ts=args.to_ts, limit=args.limit, counts=counts)
    client = publish = None
    if edge is not None and not args.dry_run:
        client = mqtt_client(f"scada-sim-{edge.edge_node}", username=os.environ.get("PDM_SCADA_SIM_MQTT_USER"),
                             password=os.environ.get("PDM_SCADA_SIM_MQTT_PASSWORD"), will=edge.node_death())
        try:
            broker.connect(client, "scada_sim", args.broker, args.port)
        except OSError as exc:
            raise SystemExit(f"scada_sim: cannot reach the broker at {args.broker}:{args.port} ({exc})")
        client.loop_start()

        def publish(topic: str, payload: bytes, qos: int = args.qos) -> None:
            info = client.publish(topic, payload, qos=qos)
            if qos:
                info.wait_for_publish(timeout=10)
    log_file = open(args.log, "w") if args.log else None
    served: Counter = Counter()
    try:
        asyncio.run(_serve(args, profile, rows, publish, log_file, served))
    except CsvError as exc:
        raise SystemExit(f"scada_sim: {exc}")
    except KeyboardInterrupt:
        if publish is not None and served["rows"]:
            publish(*edge.node_death(), qos=1)    # a clean disconnect does not send the will
    finally:
        if log_file is not None:
            log_file.close()
        if client is not None:
            client.disconnect()                   # queued after the last messages, so they go out first
            client.loop_stop()
    print(f"served {served['rows']} of {counts['read']} rows ({counts['filtered']} outside --from/--to, "
          f"{counts['skipped']} unparseable) as {served['messages']} Sparkplug messages"
          + (" [dry run]" if args.dry_run else f" to {args.broker}:{args.port}")
          + (f"; OPC UA on {args.opcua_endpoint or south_section(profile, 'opcua')['endpoint']}" if args.opcua else ""))


if __name__ == "__main__":
    sys.exit(main())
