"""Southbound OPC UA client -- subscribed tags to telemetry_json_v1 (ING-14, ING-15).

Subscribes to the nodes mapped in the profile's ``south.opcua.tags`` on the
profile's endpoint and publishes telemetry_json_v1 on the telemetry topic,
where ingest takes over.

``OpcUaMapper`` turns data-change notifications (node id, value, source
timestamp) into records: one per publishing interval of source time. The
intervals are aligned to the epoch, like ingest's 10 s grid. A record carries
every signal's last value, with ``ts`` the latest source timestamp in its
interval. OPC UA notifies changes only, so a signal that did not change keeps
its last value. An interval is closed when a notification arrives from
``lateness + 1`` intervals later (default lateness 1: one interval of slack,
because a publish response can deliver a batch tag by tag rather than in time
order), on ``tick()`` after a quiet spell, or on ``flush()``. A notification
for an interval already closed is counted as late and dropped. A null value or
a Bad status clears its signal: no records until it has a value again.

Chosen over the other option (a record each time every signal has changed
since the last record) because that one stalls on any signal that holds its
value, which OPC UA never notifies. The cost: two samples in one interval
give one record, the later one. With 9-10 s steps in a 10 s interval that
happens now and then; ingest would have averaged the two on its grid.

``ReadOnlyClient`` wraps the asyncua Client. It offers connect, disconnect,
get_node, read_values, the namespace lookups and create_subscription; nodes
come back as ``ReadOnlyNode`` (reads and browsing only) and the subscription
as ``ReadOnlySubscription`` (subscribe_data_change, unsubscribe, delete).
Anything else -- writing a value or an attribute, calling a method, adding or
deleting nodes or references, the raw ``uaclient``, ``nodes`` or ``session``
attributes -- raises WriteRefused before the asyncua object is touched
(ING-15). The wrapper is the client-side half; the server-side half is the
account in PDM_SOUTH_OPCUA_USER, which should have Browse, Read and Subscribe
rights only.

``main()`` imports asyncua and paho-mqtt when it runs. Neither is installed
where this was written, so main() has not been run; the mapper and the
wrappers are tested in tests/test_south.py.

    python services/south/opcua_client.py --profile profiles/metropt3_apu/asset_profile.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import math
import numbers
import os
import signal
import sys
import time
from collections import Counter
from typing import Callable, Optional

from pdm_common import broker
from pdm_common.mqtt import Out
from pdm_common.profile import ProfileError, load_profile, signal_names
from pdm_common.timeutil import parse_duration, to_utc

if __package__:
    from .sparkplug import (EPOCH, CommandRefused, GuardedPublisher, SouthConfigError, broker_credentials,
                            mqtt_client, signal_map, south_section, telemetry_record, telemetry_topic)
else:                                         # run as a script: python services/south/opcua_client.py
    from sparkplug import (EPOCH, CommandRefused, GuardedPublisher, SouthConfigError, broker_credentials,
                           mqtt_client, signal_map, south_section, telemetry_record, telemetry_topic)

log = logging.getLogger("south.opcua")


def _number(value) -> tuple[Optional[float], Optional[str]]:
    """(telemetry number, problem): Booleans as 1.0 / 0.0, other reals as they are."""
    if value is None:
        return None, "null"
    if isinstance(value, bool):
        return (1.0 if value else 0.0), None
    if isinstance(value, numbers.Real):
        number = float(value)
        if math.isfinite(number):
            return number, None
    return None, "non_numeric"


class OpcUaMapper:
    """The client minus OPC UA and MQTT: notifications in, telemetry_json_v1 out."""

    def __init__(self, profile: dict, *, lateness: int = 1, clock: Callable[[], float] = time.monotonic):
        cfg = south_section(profile, "opcua")
        if not isinstance(cfg.get("endpoint"), str) or not cfg["endpoint"]:
            raise SouthConfigError("south.opcua.endpoint must be an opc.tcp:// URL")
        try:
            self.interval = parse_duration(cfg.get("publishing_interval"))
        except ValueError as exc:
            raise SouthConfigError(f"south.opcua.publishing_interval: {exc}") from None
        if self.interval <= dt.timedelta(0):
            raise SouthConfigError("south.opcua.publishing_interval must be positive")
        if lateness < 0:
            raise ValueError("lateness is a number of intervals, 0 or more")
        self.profile = profile
        self.asset = profile["asset"]["id"]
        self.telemetry_topic = telemetry_topic(profile)
        self.endpoint = cfg["endpoint"]
        self.signal_of = signal_map(profile, cfg.get("tags"), "south.opcua.tags")
        self.node_ids = list(self.signal_of)
        self.signals = signal_names(profile)
        self.interval_ms = self.interval.total_seconds() * 1000.0
        self.lateness = lateness
        self.clock = clock
        self.latest: dict[str, float] = {}                # signal -> value carried between intervals
        self.pending: dict[int, list] = {}                # interval -> [(moment, signal, value or None)]
        self.closed_through: Optional[int] = None
        self.last_arrival: Optional[float] = None
        self.seq = 0
        self.counts = Counter({k: 0 for k in ("notifications", "records", "unknown_node", "no_timestamp", "late",
                                               "null", "non_numeric", "incomplete")})

    def notify(self, node_id, value, source_timestamp) -> list[Out]:
        """One data change. ``source_timestamp``: a datetime (naive means UTC, as
        OPC UA times are) or an ISO string. ``value`` None is a Bad or null value."""
        self.counts["notifications"] += 1
        sig = self.signal_of.get(str(node_id))
        if sig is None:
            self.counts["unknown_node"] += 1
            return []
        try:
            moment = to_utc(source_timestamp)
        except (TypeError, ValueError, OverflowError):
            self.counts["no_timestamp"] += 1
            return []
        k = (moment - EPOCH) // self.interval
        if self.closed_through is not None and k <= self.closed_through:
            self.counts["late"] += 1
            return []
        number, problem = _number(value)
        if problem:
            self.counts[problem] += 1
        self.pending.setdefault(k, []).append((moment, sig, number))
        self.last_arrival = self.clock()
        return self._close_through(max(self.pending) - self.lateness - 1)

    def tick(self) -> list[Out]:
        """Wall-clock check: after ``lateness + 1`` intervals without a notification,
        close what is pending, so a source that has gone quiet still delivers
        its last record."""
        if not self.pending or self.last_arrival is None:
            return []
        if self.clock() - self.last_arrival < (self.lateness + 1) * self.interval.total_seconds():
            return []
        return self._close_through(max(self.pending))

    def flush(self) -> list[Out]:
        """End of stream or shutdown: close everything pending."""
        return self._close_through(max(self.pending)) if self.pending else []

    def _close_through(self, last: int) -> list[Out]:
        outs = []
        for k in sorted(j for j in self.pending if j <= last):
            outs += self._close(k)
        if self.closed_through is None or last > self.closed_through:
            self.closed_through = last
        return outs

    def _close(self, k: int) -> list[Out]:
        updates = sorted(self.pending.pop(k), key=lambda u: u[0])   # stable: arrival order breaks ties
        for _, sig, number in updates:
            if number is None:
                self.latest.pop(sig, None)
            else:
                self.latest[sig] = number
        if len(self.latest) < len(self.signals):
            self.counts["incomplete"] += 1
            return []
        out = telemetry_record(self.profile, self.seq, updates[-1][0], {s: self.latest[s] for s in self.signals})
        self.seq += 1
        self.counts["records"] += 1
        return [out]

    def report(self) -> dict:
        return dict(sorted(self.counts.items()), asset=self.asset, pending_intervals=len(self.pending))


# --- read-only OPC UA (ING-15) -------------------------------------------------------------

class WriteRefused(PermissionError):
    """A southbound OPC UA operation that is not a read (ING-15)."""


def _refused(owner: str, name: str, allowed) -> WriteRefused:
    return WriteRefused(f"{owner}.{name} refused: southbound OPC UA access is read-only (ING-15); "
                        f"allowed: {', '.join(sorted(allowed))}")


def _unwrap(obj):
    return obj._node if isinstance(obj, ReadOnlyNode) else obj


def _wrap(obj):
    if isinstance(obj, (list, tuple)):
        return type(obj)(_wrap(x) for x in obj)
    if hasattr(obj, "nodeid") and not isinstance(obj, (ReadOnlyNode, str, bytes)):
        return ReadOnlyNode(obj)
    return obj


class ReadOnlyNode:
    """An asyncua Node that can be read and browsed, nothing else."""

    READS = frozenset({
        "read_value", "read_data_value", "read_attribute", "read_attributes", "read_browse_name",
        "read_display_name", "read_description", "read_node_class", "read_data_type",
        "read_data_type_as_variant_type", "read_value_rank", "read_array_dimensions", "read_type_definition",
        "get_references", "get_children_descriptions",
    })
    BROWSE = frozenset({"get_child", "get_children", "get_parent", "get_properties", "get_variables",
                        "get_methods", "get_referenced_nodes", "get_path"})   # these return nodes, wrapped

    def __init__(self, node):
        self._node = node

    @property
    def nodeid(self):
        return self._node.nodeid

    def __repr__(self) -> str:
        return f"ReadOnlyNode({self._node.nodeid!r})"

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self.READS:
            return getattr(self._node, name)
        if name in self.BROWSE:
            method = getattr(self._node, name)

            async def browse(*args, **kwargs):
                return _wrap(await method(*args, **kwargs))
            return browse
        raise _refused("ReadOnlyNode", name, self.READS | self.BROWSE)


class ReadOnlySubscription:
    """An asyncua Subscription limited to monitoring data changes."""

    ALLOWED = frozenset({"subscribe_data_change", "unsubscribe", "delete"})

    def __init__(self, subscription):
        self._sub = subscription

    async def subscribe_data_change(self, nodes, *args, **kwargs):
        nodes = [_unwrap(n) for n in nodes] if isinstance(nodes, (list, tuple)) else _unwrap(nodes)
        return await self._sub.subscribe_data_change(nodes, *args, **kwargs)

    async def unsubscribe(self, handle):
        return await self._sub.unsubscribe(handle)

    async def delete(self):
        return await self._sub.delete()

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        raise _refused("ReadOnlySubscription", name, self.ALLOWED)


class ReadOnlyClient:
    """An asyncua Client limited to connect, browse, read and subscribe."""

    ALLOWED = frozenset({"connect", "disconnect", "get_node", "read_values", "get_namespace_array",
                         "get_namespace_index", "create_subscription"})

    def __init__(self, client):
        self._client = client

    async def connect(self):
        return await self._client.connect()

    async def disconnect(self):
        return await self._client.disconnect()

    async def __aenter__(self) -> "ReadOnlyClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.disconnect()

    def get_node(self, nodeid) -> ReadOnlyNode:
        return ReadOnlyNode(self._client.get_node(_unwrap(nodeid)))

    async def read_values(self, nodes):
        return await self._client.read_values([_unwrap(n) for n in nodes])

    async def get_namespace_array(self):
        return await self._client.get_namespace_array()

    async def get_namespace_index(self, uri: str):
        return await self._client.get_namespace_index(uri)

    async def create_subscription(self, period, handler, **kwargs) -> ReadOnlySubscription:
        return ReadOnlySubscription(await self._client.create_subscription(period, handler, **kwargs))

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        raise _refused("ReadOnlyClient", name, self.ALLOWED)


class SubscriptionHandler:
    """asyncua's subscription handler: every data change goes to the mapper and
    what the mapper returns goes to ``send``."""

    def __init__(self, mapper: OpcUaMapper, node_ids: dict, send: Callable[[Out], None]):
        self.mapper = mapper
        self.node_ids = node_ids            # asyncua NodeId -> the profile's node id string
        self.send = send

    def datachange_notification(self, node, val, data) -> None:
        value = getattr(getattr(data, "monitored_item", None), "Value", None)   # ua.DataValue
        status = getattr(value, "StatusCode", None)
        good = status is None or status.is_good()
        stamp = getattr(value, "SourceTimestamp", None) or getattr(value, "ServerTimestamp", None)
        for out in self.mapper.notify(self.node_ids.get(node.nodeid), val if good else None, stamp):
            self.send(out)

    def status_change_notification(self, status) -> None:
        log.warning("subscription status changed: %s", status)


# --- main (needs asyncua and paho-mqtt) ------------------------------------------------------

async def run(mapper: OpcUaMapper, send: Callable[[Out], None], *, endpoint: Optional[str] = None,
              username: Optional[str] = None, password: Optional[str] = None, queue: int = 10,
              sampling_ms: Optional[float] = None) -> None:
    """Connect as the read-only user, subscribe to every mapped node and feed the
    mapper until SIGINT or SIGTERM; then flush. Not run here (no asyncua)."""
    from asyncua import Client

    raw = Client(url=endpoint or mapper.endpoint)
    if username:
        raw.set_user(username)
        raw.set_password(password or "")
    client = ReadOnlyClient(raw)
    del raw                                  # from here on, only the read-only wrapper
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass                             # Ctrl-C still ends asyncio.run with KeyboardInterrupt
    try:
        async with client:
            nodes = [client.get_node(node_id) for node_id in mapper.node_ids]
            handler = SubscriptionHandler(mapper, {n.nodeid: nid for n, nid in zip(nodes, mapper.node_ids)}, send)
            sub = await client.create_subscription(mapper.interval_ms, handler)
            await sub.subscribe_data_change(nodes, queuesize=queue, sampling_interval=(
                mapper.interval_ms if sampling_ms is None else sampling_ms))
            log.info("subscribed to %d nodes on %s every %.0f ms", len(nodes), endpoint or mapper.endpoint,
                     mapper.interval_ms)
            try:
                while not stop.is_set():
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                    for out in mapper.tick():
                        send(out)
            finally:
                await sub.delete()
    finally:
        for out in mapper.flush():
            send(out)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile", required=True, help="asset profile YAML with a south.opcua section")
    p.add_argument("--endpoint", default=None, help="OPC UA endpoint (default: south.opcua.endpoint)")
    p.add_argument("--broker", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--qos", type=int, default=1, choices=[0, 1, 2])
    p.add_argument("--queue", type=int, default=10, help="monitored-item queue size")
    p.add_argument("--sampling-ms", type=float, default=None,
                   help="server sampling interval (default: the publishing interval)")
    p.add_argument("--lateness", type=int, default=1,
                   help="intervals of slack for notifications that arrive out of time order")
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        mapper = OpcUaMapper(load_profile(args.profile), lateness=args.lateness)
    except (ProfileError, SouthConfigError) as exc:
        raise SystemExit(f"opcua_client: {exc}")
    username, password = broker_credentials()
    client = mqtt_client("pdm-south-opcua", username=username, password=password)
    try:
        broker.connect(client, "south", args.broker, args.port)
    except OSError as exc:
        raise SystemExit(f"opcua_client: cannot reach the broker at {args.broker}:{args.port} ({exc})")
    client.loop_start()
    publisher = GuardedPublisher(client, {mapper.telemetry_topic}, qos=args.qos)

    def send(out: Out) -> None:
        try:
            publisher.publish(out.topic, out.payload, retain=out.retain)
        except CommandRefused as exc:
            log.error("%s", exc)

    log.info("asset %s: %d tags on %s -> %s", mapper.asset, len(mapper.node_ids), args.endpoint or mapper.endpoint,
             mapper.telemetry_topic)
    try:
        asyncio.run(run(mapper, send, endpoint=args.endpoint, username=os.environ.get("PDM_SOUTH_OPCUA_USER"),
                        password=os.environ.get("PDM_SOUTH_OPCUA_PASSWORD"), queue=args.queue,
                        sampling_ms=args.sampling_ms))
    except KeyboardInterrupt:
        pass
    except (OSError, asyncio.TimeoutError) as exc:
        raise SystemExit(f"opcua_client: cannot use the OPC UA server: {exc!r}")
    finally:
        client.loop_stop()
        client.disconnect()
    print(json.dumps(mapper.report(), sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
