"""Southbound Sparkplug B adapter -- plant metrics to telemetry_json_v1 (ING-14, ING-15).

Subscribes to one Sparkplug B 3.0 group (``spBv1.0/<group>/#``), follows the
birth and death certificates of the edge node and device named in the
profile's ``south.sparkplug`` section, maps the device's metrics to profile
signals and publishes telemetry_json_v1 on the profile's telemetry topic,
where ingest takes over (ING-06 onward).

* Births: NBIRTH starts a new session of the edge node (aliases, bdSeq, the
  0-255 sequence numbers). DBIRTH declares the device's metrics with names,
  aliases and datatypes and carries their current values, which become the
  last value of each signal.
* DATA: a DDATA names its metrics by alias or name and updates the last value
  of their signals. Sparkplug reports by exception, so every record carries
  every signal's last value. A record is published for a DDATA that updated
  at least one mapped metric, and only once every profile signal has a value.
  ``ts`` is the latest metric timestamp in the message (the payload timestamp
  if its metrics have none), in UTC; ``seq`` counts records from 0.
* Deaths: a DDEATH, or an NDEATH whose bdSeq matches the NBIRTH, means the
  device has gone: its values are forgotten and nothing is published until
  the next DBIRTH (no data, for NE 107). DATA that arrives before any birth
  is ignored and counted; the adapter cannot ask for a rebirth, because that
  is an NCMD.
* Other edge nodes and devices, commands sent by other hosts, unknown
  metrics, historical values, nulls and malformed payloads are counted, never
  fatal. A null or non-numeric value clears its signal, so records stop until
  the signal has a usable value again. ``report()`` returns the counts.

Read-only (ING-15): the adapter publishes nothing but telemetry. Every publish
in ``main()`` goes through ``GuardedPublisher``, which calls ``guard_publish``
first: a topic in the Sparkplug namespace (NCMD, DCMD, STATE or anything else
under spBv1.0/) or any topic other than the telemetry topic raises
CommandRefused before the MQTT client sees it. The broker account
(PDM_SOUTH_MQTT_USER) should be allowed to read spBv1.0/<group>/# and to write
the telemetry topic only; services/south/README.md has the ACL.

The protobuf codec is written by hand for the part of the Sparkplug B
``Payload`` that telemetry needs, so nothing here depends on a protobuf
runtime. Unknown fields (metadata, properties, datasets, templates, uuid,
body) are skipped. opcua_client.py and scada_sim.py reuse the codec, the
profile mapping checks and the publish guard from this module.

    python services/south/sparkplug.py --profile profiles/metropt3_apu/asset_profile.yaml
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
import signal
import struct
import sys
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional

from pdm_common.hashing import canonical_json
from pdm_common.mqtt import Out
from pdm_common.profile import ProfileError, load_profile, signal_names
from pdm_common.timeutil import iso_utc

log = logging.getLogger("south.sparkplug")

EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
NAMESPACE = "spBv1.0"
NODE_TYPES = ("NBIRTH", "NDEATH", "NDATA", "NCMD")
DEVICE_TYPES = ("DBIRTH", "DDEATH", "DDATA", "DCMD")
COMMAND_TYPES = ("NCMD", "DCMD")
MSG_TYPES = NODE_TYPES + DEVICE_TYPES + ("STATE",)
BD_SEQ = "bdSeq"                         # NBIRTH and NDEATH carry the birth-death sequence
REBIRTH = "Node Control/Rebirth"         # every NBIRTH declares it; only a host's NCMD sets it

# Sparkplug B datatypes (sparkplug_b.proto, DataType)
INT8, INT16, INT32, INT64 = 1, 2, 3, 4
UINT8, UINT16, UINT32, UINT64 = 5, 6, 7, 8
FLOAT, DOUBLE, BOOLEAN, STRING, DATETIME, TEXT = 9, 10, 11, 12, 13, 14
UUID, DATASET, BYTES, FILE, TEMPLATE = 15, 16, 17, 18, 19
DATATYPE_NAMES = {
    0: "Unknown", INT8: "Int8", INT16: "Int16", INT32: "Int32", INT64: "Int64", UINT8: "UInt8",
    UINT16: "UInt16", UINT32: "UInt32", UINT64: "UInt64", FLOAT: "Float", DOUBLE: "Double",
    BOOLEAN: "Boolean", STRING: "String", DATETIME: "DateTime", TEXT: "Text", UUID: "UUID",
    DATASET: "DataSet", BYTES: "Bytes", FILE: "File", TEMPLATE: "Template",
}
NUMERIC_TYPES = frozenset({INT8, INT16, INT32, INT64, UINT8, UINT16, UINT32, UINT64, FLOAT, DOUBLE, BOOLEAN})
_SIGNED_BITS = {INT8: 8, INT16: 16, INT32: 32, INT64: 64}
_INT_RANGES = {**{t: (-(1 << (b - 1)), 1 << (b - 1)) for t, b in _SIGNED_BITS.items()},
               UINT8: (0, 1 << 8), UINT16: (0, 1 << 16), UINT32: (0, 1 << 32), UINT64: (0, 1 << 64)}

# Payload.Metric value fields (the "value" oneof) and the datatypes they carry
INT_VALUE, LONG_VALUE, FLOAT_VALUE, DOUBLE_VALUE = 10, 11, 12, 13
BOOLEAN_VALUE, STRING_VALUE, BYTES_VALUE = 14, 15, 16
VALUE_FIELD = {
    INT8: INT_VALUE, INT16: INT_VALUE, INT32: INT_VALUE, UINT8: INT_VALUE, UINT16: INT_VALUE, UINT32: INT_VALUE,
    INT64: LONG_VALUE, UINT64: LONG_VALUE, DATETIME: LONG_VALUE,
    FLOAT: FLOAT_VALUE, DOUBLE: DOUBLE_VALUE, BOOLEAN: BOOLEAN_VALUE,
    STRING: STRING_VALUE, TEXT: STRING_VALUE, UUID: STRING_VALUE, BYTES: BYTES_VALUE, FILE: BYTES_VALUE,
}

# protobuf wire types; Sparkplug B uses no groups (wire types 3 and 4)
VARINT, I64, LEN, I32 = 0, 1, 2, 5
U32, U64 = (1 << 32) - 1, (1 << 64) - 1
_MAX_FIELD = (1 << 29) - 1


# --- codec ---------------------------------------------------------------------------------

class DecodeError(ValueError):
    """Bytes that are not a Sparkplug B payload."""


class EncodeError(ValueError):
    """A payload that cannot be encoded."""


def typed(value, datatype: Optional[int]):
    """A decoded integer as its datatype means it: the signed types are two's
    complement in the unsigned protobuf fields (Int8 to Int32 in uint32, Int64
    in uint64). Idempotent, and anything that is not an int passes through."""
    bits = _SIGNED_BITS.get(datatype)
    if bits is None or isinstance(value, bool) or not isinstance(value, int):
        return value
    value &= (1 << bits) - 1
    return value - (1 << bits) if value >> (bits - 1) else value


@dataclass
class Metric:
    """One Sparkplug B metric, the fields telemetry needs.

    ``value`` is the Python value: int, float, bool, str or bytes. A decoded
    value of a signed integer type is signed when the metric carries its
    datatype; without one (DATA messages) it is the raw unsigned field, which
    ``typed`` reads with the datatype from the birth. ``value_field`` is the
    protobuf field that carries the value (10 to 16). It follows from
    ``datatype`` when not given; a DATA metric normally omits its datatype, so
    it gives ``value_field`` instead.
    """
    name: Optional[str] = None
    alias: Optional[int] = None
    timestamp: Optional[int] = None          # ms since the epoch, UTC
    datatype: Optional[int] = None
    is_historical: bool = False
    is_transient: bool = False
    is_null: bool = False
    value: object = None
    value_field: Optional[int] = None

    def __post_init__(self):
        if self.value is not None and self.value_field is None:
            self.value_field = VALUE_FIELD.get(self.datatype)


@dataclass
class Payload:
    """The Sparkplug B payload: timestamp (ms since the epoch), metrics, seq (0-255)."""
    timestamp: Optional[int] = None
    metrics: list = field(default_factory=list)
    seq: Optional[int] = None


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _key(number: int, wire: int) -> bytes:
    return _varint(number << 3 | wire)


def _uint(value, bits: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < (1 << bits):
        raise EncodeError(f"{what} must be an integer in [0, 2**{bits}), got {value!r}")
    return value


def _encode_value(number: int, value) -> bytes:
    if number in (INT_VALUE, LONG_VALUE):
        bits = 32 if number == INT_VALUE else 64
        if isinstance(value, bool) or not isinstance(value, int) or not -(1 << (bits - 1)) <= value < (1 << bits):
            raise EncodeError(f"{value!r} does not fit a {bits}-bit integer field")
        return _key(number, VARINT) + _varint(value & ((1 << bits) - 1))
    if number in (FLOAT_VALUE, DOUBLE_VALUE):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EncodeError(f"{value!r} is not a number")
        try:
            packed = struct.pack("<f" if number == FLOAT_VALUE else "<d", float(value))
        except (OverflowError, struct.error) as exc:
            raise EncodeError(f"{value!r} does not fit a 32-bit float") from exc
        return _key(number, I32 if number == FLOAT_VALUE else I64) + packed
    if number == BOOLEAN_VALUE:
        if not isinstance(value, bool):
            raise EncodeError(f"a Boolean metric needs True or False, got {value!r}")
        return _key(number, VARINT) + (b"\x01" if value else b"\x00")
    if number == STRING_VALUE:
        if not isinstance(value, str):
            raise EncodeError(f"a string metric needs str, got {type(value).__name__}")
        data = value.encode("utf-8")
        return _key(number, LEN) + _varint(len(data)) + data
    if number == BYTES_VALUE:
        if not isinstance(value, (bytes, bytearray)):
            raise EncodeError(f"a bytes metric needs bytes, got {type(value).__name__}")
        return _key(number, LEN) + _varint(len(value)) + bytes(value)
    raise EncodeError(f"no value field {number!r} in this codec (datatype missing or not supported)")


def _length_delimited(number: int, data: bytes) -> bytes:
    return _key(number, LEN) + _varint(len(data)) + data


def encode_metric(m: Metric) -> bytes:
    """One Payload.Metric, fields in number order (as protobuf serializers write them)."""
    out = bytearray()
    if m.name is not None:
        out += _length_delimited(1, m.name.encode("utf-8"))
    if m.alias is not None:
        out += _key(2, VARINT) + _varint(_uint(m.alias, 64, "alias"))
    if m.timestamp is not None:
        out += _key(3, VARINT) + _varint(_uint(m.timestamp, 64, "metric timestamp"))
    if m.datatype is not None:
        out += _key(4, VARINT) + _varint(_uint(m.datatype, 32, "datatype"))
    for number, flag in ((5, m.is_historical), (6, m.is_transient), (7, m.is_null)):
        if flag:
            out += _key(number, VARINT) + b"\x01"
    if m.value is not None and not m.is_null:
        span = _INT_RANGES.get(m.datatype)
        if span and isinstance(m.value, int) and not isinstance(m.value, bool) and not span[0] <= m.value < span[1]:
            raise EncodeError(f"{m.value} is out of range for {DATATYPE_NAMES[m.datatype]}")
        out += _encode_value(m.value_field, m.value)
    return bytes(out)


def encode_payload(p: Payload) -> bytes:
    """A Sparkplug B payload as protobuf bytes."""
    out = bytearray()
    if p.timestamp is not None:
        out += _key(1, VARINT) + _varint(_uint(p.timestamp, 64, "payload timestamp"))
    for m in p.metrics:
        out += _length_delimited(2, encode_metric(m))
    if p.seq is not None:
        out += _key(3, VARINT) + _varint(_uint(p.seq, 64, "seq"))
    return bytes(out)


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    for _ in range(10):
        if pos >= len(buf):
            raise DecodeError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            if result > U64:
                raise DecodeError("varint does not fit 64 bits")
            return result, pos
        shift += 7
    raise DecodeError("varint longer than 10 bytes")


def _fields(buf: bytes) -> Iterator[tuple[int, int, object]]:
    """(field number, wire type, value) per field: an int for a varint, bytes otherwise."""
    pos, end = 0, len(buf)
    while pos < end:
        key, pos = _read_varint(buf, pos)
        number, wire = key >> 3, key & 7
        if not 0 < number <= _MAX_FIELD:
            raise DecodeError(f"invalid field number {number}")
        if wire == VARINT:
            value, pos = _read_varint(buf, pos)
        elif wire in (I64, I32):
            size = 8 if wire == I64 else 4
            if pos + size > end:
                raise DecodeError(f"truncated {size * 8}-bit field {number}")
            value, pos = buf[pos:pos + size], pos + size
        elif wire == LEN:
            length, pos = _read_varint(buf, pos)
            if length > end - pos:
                raise DecodeError(f"truncated length-delimited field {number}")
            value, pos = buf[pos:pos + length], pos + length
        else:
            raise DecodeError(f"wire type {wire} in field {number}: groups are not used by Sparkplug B")
        yield number, wire, value


def _want(number: int, wire: int, expected: int, value):
    if wire != expected:
        raise DecodeError(f"field {number} has wire type {wire}, expected {expected}")
    return value


def _text(data: bytes, number: int) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DecodeError(f"field {number} is not UTF-8") from exc


def decode_metric(buf: bytes) -> Metric:
    m = Metric()
    for number, wire, value in _fields(buf):
        if number == 1:
            m.name = _text(_want(number, wire, LEN, value), number)
        elif number in (2, 3):
            setattr(m, "alias" if number == 2 else "timestamp", _want(number, wire, VARINT, value))
        elif number == 4:
            m.datatype = _want(number, wire, VARINT, value) & U32
        elif number in (5, 6, 7):
            setattr(m, ("is_historical", "is_transient", "is_null")[number - 5],
                    bool(_want(number, wire, VARINT, value)))
        elif number == INT_VALUE:
            m.value, m.value_field = _want(number, wire, VARINT, value) & U32, number
        elif number == LONG_VALUE:
            m.value, m.value_field = _want(number, wire, VARINT, value), number
        elif number == FLOAT_VALUE:
            m.value, m.value_field = struct.unpack("<f", _want(number, wire, I32, value))[0], number
        elif number == DOUBLE_VALUE:
            m.value, m.value_field = struct.unpack("<d", _want(number, wire, I64, value))[0], number
        elif number == BOOLEAN_VALUE:
            m.value, m.value_field = bool(_want(number, wire, VARINT, value)), number
        elif number == STRING_VALUE:
            m.value, m.value_field = _text(_want(number, wire, LEN, value), number), number
        elif number == BYTES_VALUE:
            m.value, m.value_field = bytes(_want(number, wire, LEN, value)), number
        # 8 metadata, 9 properties, 17 dataset, 18 template, 19 extension, unknown: skipped
    if m.datatype is not None:
        m.value = typed(m.value, m.datatype)
    return m


def decode_payload(data) -> Payload:
    """Protobuf bytes -> Payload. Raises DecodeError on anything malformed."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise DecodeError(f"a Sparkplug B payload is bytes, not {type(data).__name__}")
    p = Payload()
    for number, wire, value in _fields(bytes(data)):
        if number == 1:
            p.timestamp = _want(number, wire, VARINT, value)
        elif number == 2:
            p.metrics.append(decode_metric(_want(number, wire, LEN, value)))
        elif number == 3:
            p.seq = _want(number, wire, VARINT, value)
        # 4 uuid, 5 body, extensions: skipped
    return p


def ms_of(moment: dt.datetime) -> int:
    """Aware datetime -> Sparkplug timestamp (ms since the epoch, UTC)."""
    return (moment - EPOCH) // dt.timedelta(milliseconds=1)


def moment_of(ms: int) -> dt.datetime:
    """Sparkplug timestamp -> aware UTC datetime (exact, no float on the way)."""
    return EPOCH + dt.timedelta(milliseconds=ms)


def metric_number(metric: Metric, datatype: Optional[int] = None) -> Optional[float]:
    """The metric's value as a telemetry number: integers and floats as they are,
    Booleans as 1.0 / 0.0. None for a null, a missing value, a non-numeric
    datatype or a value that is not finite."""
    dtype = datatype if datatype is not None else metric.datatype
    if metric.is_null or metric.value is None or (dtype is not None and dtype not in NUMERIC_TYPES):
        return None
    value = typed(metric.value, dtype)
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


# --- topics ---------------------------------------------------------------------------------

class TopicError(ValueError):
    """Not a Sparkplug B topic."""


@dataclass(frozen=True)
class SparkplugTopic:
    msg_type: str
    group: Optional[str] = None
    edge_node: Optional[str] = None
    device: Optional[str] = None
    host_id: Optional[str] = None          # STATE only

    @property
    def is_command(self) -> bool:
        return self.msg_type in COMMAND_TYPES


def parse_topic(topic: str) -> SparkplugTopic:
    """``spBv1.0/<group>/<type>/<edge_node>[/<device>]``, or ``spBv1.0/STATE/<host_id>``.

    Node message types (NBIRTH, NDEATH, NDATA, NCMD) take no device; device
    types (DBIRTH, DDEATH, DDATA, DCMD) need one. STATE is the 3.0 host
    application topic.
    """
    if not isinstance(topic, str):
        raise TopicError(f"a topic is a string, not {type(topic).__name__}")
    parts = topic.split("/")
    if parts[0] != NAMESPACE:
        raise TopicError(f"{topic!r} is not in the {NAMESPACE} namespace")
    if any(not p or "+" in p or "#" in p for p in parts):
        raise TopicError(f"{topic!r} has an empty level or a wildcard")
    if len(parts) == 3 and parts[1] == "STATE":
        return SparkplugTopic("STATE", host_id=parts[2])
    if len(parts) not in (4, 5):
        raise TopicError(f"{topic!r} is not {NAMESPACE}/<group>/<type>/<edge_node>[/<device>]")
    group, msg_type, edge_node = parts[1:4]
    device = parts[4] if len(parts) == 5 else None
    if msg_type in NODE_TYPES:
        if device is not None:
            raise TopicError(f"{topic!r}: {msg_type} is a node message and takes no device")
    elif msg_type in DEVICE_TYPES:
        if device is None:
            raise TopicError(f"{topic!r}: {msg_type} is a device message and needs a device")
    else:
        raise TopicError(f"{topic!r}: unknown message type {msg_type!r}")
    return SparkplugTopic(msg_type, group, edge_node, device)


def make_topic(group: str, msg_type: str, edge_node: str, device: Optional[str] = None) -> str:
    text = "/".join([NAMESPACE, group, msg_type, edge_node] + ([device] if device is not None else []))
    parse_topic(text)
    return text


# --- read-only publishing (ING-15) ---------------------------------------------------------

class CommandRefused(PermissionError):
    """A southbound adapter tried to publish something other than telemetry (ING-15)."""


def guard_publish(topic: str, allowed: Iterable[str]) -> None:
    """Raise CommandRefused unless ``topic`` is one of ``allowed`` and outside the
    Sparkplug namespace. Nothing a southbound adapter sends may reach the plant:
    no NCMD or DCMD, no STATE, no other topic."""
    if not isinstance(topic, str):
        raise CommandRefused(f"refused to publish on {topic!r}: not a topic")
    if topic == NAMESPACE or topic.startswith(NAMESPACE + "/"):
        try:
            kind = parse_topic(topic).msg_type
        except TopicError:
            kind = "a message"
        what = "a command to the plant" if kind in COMMAND_TYPES else f"{kind} into the Sparkplug namespace"
        raise CommandRefused(f"refused to publish {what} on {topic!r}: southbound adapters are read-only (ING-15)")
    allowed = frozenset(allowed)
    if topic not in allowed:
        raise CommandRefused(f"refused to publish on {topic!r}: a southbound adapter publishes only on "
                             f"{', '.join(sorted(allowed)) or 'nothing'} (ING-15)")


class GuardedPublisher:
    """The only way the southbound services publish: the topic is checked
    before the MQTT client sees it."""

    def __init__(self, client, allowed: Iterable[str], *, qos: int = 1):
        self._client = client
        self.allowed = frozenset(allowed)
        self.qos = qos

    def publish(self, topic: str, payload, retain: bool = False):
        guard_publish(topic, self.allowed)
        return self._client.publish(topic, payload, qos=self.qos, retain=retain)


# --- the profile's south section (shared with opcua_client.py and scada_sim.py) ------------

class SouthConfigError(ValueError):
    """The profile's south section cannot drive an adapter."""


def south_section(profile: dict, source: str) -> dict:
    section = (profile.get("south") or {}).get(source)
    if not isinstance(section, dict):
        raise SouthConfigError(f"the asset profile has no south.{source} section (ING-14); "
                               "services/south/README.md shows one")
    return section


def signal_map(profile: dict, mapping, where: str) -> dict[str, str]:
    """Source tag -> signal, checked against the profile: every signal mapped
    exactly once, because ingest wants every signal in every record."""
    signals = signal_names(profile)
    if not isinstance(mapping, dict) or not mapping:
        raise SouthConfigError(f"{where} must map source tags to the profile's signals")
    problems = []
    unknown = sorted({str(s) for s in mapping.values() if s not in signals})
    if unknown:
        problems.append(f"not signals of this profile: {', '.join(unknown)}")
    seen = Counter(s for s in mapping.values() if s in signals)
    twice = [s for s in signals if seen[s] > 1]
    if twice:
        problems.append(f"mapped more than once: {', '.join(twice)}")
    missing = [s for s in signals if not seen[s]]
    if missing:
        problems.append(f"not mapped: {', '.join(missing)}")
    if problems:
        raise SouthConfigError(f"{where}: " + "; ".join(problems))
    return {str(tag): s for tag, s in mapping.items()}


def telemetry_topic(profile: dict) -> str:
    topic = profile["topics"]["telemetry"]
    if topic == NAMESPACE or topic.startswith(NAMESPACE + "/"):
        raise SouthConfigError(f"topics.telemetry {topic!r} is in the Sparkplug namespace; "
                               "a southbound adapter never publishes there")
    return topic


def telemetry_record(profile: dict, seq: int, moment: dt.datetime, values: dict) -> Out:
    """One telemetry_json_v1 message, as the replayer builds it but with ``ts`` in UTC."""
    doc = {"schema": profile["topics"]["payload_schema"], "asset": profile["asset"]["id"], "seq": seq,
           "ts": iso_utc(moment), "signals": values}
    return Out(profile["topics"]["telemetry"], canonical_json(doc))


# --- the adapter ---------------------------------------------------------------------------

class SparkplugAdapter:
    """The adapter minus MQTT: ``handle(topic, payload)`` -> telemetry messages."""

    def __init__(self, profile: dict):
        cfg = south_section(profile, "sparkplug")
        for key in ("group", "edge_node", "device"):
            if not isinstance(cfg.get(key), str) or not cfg[key]:
                raise SouthConfigError(f"south.sparkplug.{key} must be a non-empty string")
        try:
            make_topic(cfg["group"], "DDATA", cfg["edge_node"], cfg["device"])
        except TopicError as exc:
            raise SouthConfigError(f"south.sparkplug: {exc}") from None
        self.profile = profile
        self.asset = profile["asset"]["id"]
        self.telemetry_topic = telemetry_topic(profile)
        self.group, self.edge_node, self.device = cfg["group"], cfg["edge_node"], cfg["device"]
        self.subscription = f"{NAMESPACE}/{self.group}/#"
        self.signal_of = signal_map(profile, cfg.get("metrics"), "south.sparkplug.metrics")
        self.signals = signal_names(profile)
        self.node_online = False
        self.device_online = False
        self.bd_seq: Optional[int] = None
        self.aliases: dict[int, str] = {}        # one table per edge node: aliases are unique across it
        self.datatypes: dict[str, int] = {}      # metric name -> datatype declared at birth
        self.values: dict[str, float] = {}       # signal -> last value
        self.next_seq: Optional[int] = None      # the Sparkplug seq expected next
        self.seq = 0                             # telemetry_json_v1 seq
        self.counts = Counter({k: 0 for k in ("messages", "records", "malformed", "other_source", "nbirth",
                                               "dbirth", "ndeath", "ddeath", "before_birth", "unknown_metric",
                                               "null", "non_numeric", "incomplete", "seq_gaps")})

    def guard_publish(self, topic: str) -> None:
        guard_publish(topic, {self.telemetry_topic})

    def handle(self, topic: str, payload: bytes) -> list[Out]:
        self.counts["messages"] += 1
        try:
            t = parse_topic(topic)
        except TopicError as exc:
            self.counts["bad_topic"] += 1
            log.warning("ignored: %s", exc)
            return []
        if t.msg_type == "STATE" or t.group != self.group or t.edge_node != self.edge_node \
                or (t.device is not None and t.device != self.device):
            self.counts["other_source"] += 1
            return []
        if t.is_command:
            self.counts["commands_seen"] += 1    # sent by another host; this adapter never sends one
            return []
        try:
            p = decode_payload(payload)
        except DecodeError as exc:
            self.counts["malformed"] += 1
            log.warning("malformed %s payload on %s: %s", t.msg_type, topic, exc)
            return []
        if t.msg_type != "NDEATH":               # NDEATH is the MQTT will: no seq
            self._check_seq(t.msg_type, p)
        return getattr(self, "_" + t.msg_type.lower())(p)

    # -- session state ----------------------------------------------------------------------
    def _check_seq(self, kind: str, p: Payload) -> None:
        if p.seq is None:
            self.counts["missing_seq"] += 1
            return
        if kind != "NBIRTH" and self.next_seq is not None and p.seq != self.next_seq:
            self.counts["seq_gaps"] += 1
            log.warning("%s seq %d, expected %d: messages lost or reordered", kind, p.seq, self.next_seq)
        self.next_seq = (p.seq + 1) % 256

    def _learn(self, metrics: list) -> None:
        for m in metrics:
            if m.name is None:
                continue
            if m.alias is not None:
                self.aliases[m.alias] = m.name
            if m.datatype is not None:
                self.datatypes[m.name] = m.datatype

    def _forget(self) -> None:
        self.device_online = False
        self.values.clear()

    def _nbirth(self, p: Payload) -> list[Out]:
        self.counts["nbirth"] += 1
        self.aliases.clear()
        self.datatypes.clear()
        self._forget()                           # the devices follow with their own births
        self._learn(p.metrics)
        self.node_online = True
        bd = next((m for m in p.metrics if m.name == BD_SEQ), None)
        self.bd_seq = bd.value if bd is not None and isinstance(bd.value, int) else None
        log.info("NBIRTH %s/%s (bdSeq %s)", self.group, self.edge_node, self.bd_seq)
        return []

    def _ndeath(self, p: Payload) -> list[Out]:
        bd = next((m.value for m in p.metrics if m.name == BD_SEQ), None)
        if self.bd_seq is not None and isinstance(bd, int) and bd != self.bd_seq:
            self.counts["stale_ndeath"] += 1     # a will from an earlier session, delivered late
            log.info("NDEATH with bdSeq %s ignored: the live session has bdSeq %s", bd, self.bd_seq)
            return []
        self.counts["ndeath"] += 1
        self.node_online = False
        self.aliases.clear()
        self.datatypes.clear()
        self.next_seq = None
        self._forget()
        log.warning("NDEATH %s/%s: no records until the next birth", self.group, self.edge_node)
        return []

    def _ndata(self, p: Payload) -> list[Out]:
        self.counts["ndata"] += 1                # node metrics: the mapping covers the device's
        return []

    def _dbirth(self, p: Payload) -> list[Out]:
        self.counts["dbirth"] += 1
        self._learn(p.metrics)
        self.values.clear()
        self._apply(p.metrics)
        self.device_online = True
        missing = [s for s in self.signals if s not in self.values]
        log.info("DBIRTH %s: %d metrics%s", self.device, len(p.metrics),
                 f"; no value yet for {', '.join(missing)}" if missing else "")
        return []

    def _ddeath(self, p: Payload) -> list[Out]:
        self.counts["ddeath"] += 1
        self._forget()
        log.warning("DDEATH %s: no records until the next DBIRTH", self.device)
        return []

    # -- data -------------------------------------------------------------------------------
    def _apply(self, metrics: list) -> tuple[bool, list[int]]:
        """Update the last values from the mapped metrics; return whether any
        was applied and the timestamps they carry."""
        applied, stamps = False, []
        for m in metrics:
            name = m.name if m.name is not None else self.aliases.get(m.alias)
            if name is None:
                self.counts["unknown_alias"] += 1
                continue
            sig = self.signal_of.get(name)
            if sig is None:
                self.counts["unknown_metric"] += 1
                continue
            if m.is_historical:                  # store-and-forward backfill, not the current value
                self.counts["historical"] += 1
                continue
            number = metric_number(m, self.datatypes.get(name))
            if number is None:
                self.counts["null" if m.is_null else "non_numeric"] += 1
                self.values.pop(sig, None)
            else:
                self.values[sig] = number
            applied = True
            if m.timestamp is not None:
                stamps.append(m.timestamp)
        return applied, stamps

    def _ddata(self, p: Payload) -> list[Out]:
        if not self.device_online:
            self.counts["before_birth"] += 1
            return []
        applied, stamps = self._apply(p.metrics)
        if not applied:
            return []
        ms = max(stamps) if stamps else p.timestamp
        if ms is None:
            self.counts["no_timestamp"] += 1
            return []
        if len(self.values) < len(self.signals):
            self.counts["incomplete"] += 1
            return []
        try:
            moment = moment_of(ms)
        except OverflowError:
            self.counts["no_timestamp"] += 1
            return []
        out = telemetry_record(self.profile, self.seq, moment, {s: self.values[s] for s in self.signals})
        self.seq += 1
        self.counts["records"] += 1
        return [out]

    def report(self) -> dict:
        return dict(sorted(self.counts.items()), asset=self.asset, node_online=self.node_online,
                    device_online=self.device_online)


# --- MQTT ------------------------------------------------------------------------------------

def mqtt_client(client_id: str, *, username: Optional[str] = None, password: Optional[str] = None,
                will: Optional[tuple[str, bytes]] = None):
    """A paho client (imported here: only the mains need a broker)."""
    import paho.mqtt.client as mqtt

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    if username:
        client.username_pw_set(username, password)
    if will is not None:
        client.will_set(will[0], will[1], qos=1, retain=False)
    return client


def broker_credentials() -> tuple[Optional[str], Optional[str]]:
    """The southbound broker account: PDM_SOUTH_MQTT_USER and PDM_SOUTH_MQTT_PASSWORD."""
    return os.environ.get("PDM_SOUTH_MQTT_USER") or None, os.environ.get("PDM_SOUTH_MQTT_PASSWORD") or None


def run(adapter: SparkplugAdapter, *, host: str = "localhost", port: int = 1883, qos: int = 1,
        username: Optional[str] = None, password: Optional[str] = None) -> None:
    """Subscribe to the group and publish what the adapter returns, until SIGINT or SIGTERM.

    Not pdm_common.mqtt.run_service, for two reasons: run_service dispatches on
    exact topics and passes handlers only the payload, while the adapter needs
    the topic of every message under spBv1.0/<group>/# (message type, edge
    node, device); and it connects without credentials, while the read-only
    broker account is part of ING-15. Otherwise it behaves the same: subscribe
    on connect so a reconnect resubscribes, one message at a time, and a bad
    message never stops the loop. Adapter state survives a reconnect; births
    and deaths sent while disconnected are missed (persistent sessions are R3
    hardening).
    """
    client = mqtt_client("pdm-south-sparkplug", username=username, password=password)
    publisher = GuardedPublisher(client, {adapter.telemetry_topic}, qos=qos)
    lock = threading.Lock()
    stop = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("broker refused the connection: %s", reason_code)
            return
        client.subscribe(adapter.subscription, qos=qos)
        log.info("connected to %s:%s; subscribed to %s", host, port, adapter.subscription)

    def on_message(client, userdata, msg):
        try:
            with lock:
                outs = adapter.handle(msg.topic, msg.payload)
        except Exception:                        # a bad message must not stop the adapter
            log.exception("adapter failed on a message from %s", msg.topic)
            return
        for out in outs:
            try:
                publisher.publish(out.topic, out.payload, retain=out.retain)
            except CommandRefused as exc:
                log.error("%s", exc)

    client.on_connect = on_connect
    client.on_message = on_message
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        client.connect(host, port)
    except OSError as exc:
        raise SystemExit(f"sparkplug: cannot reach the broker at {host}:{port} ({exc}); "
                         "is it running? docker compose up -d")
    client.loop_start()
    try:
        stop.wait()
    finally:
        client.loop_stop()
        client.disconnect()


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile", required=True, help="asset profile YAML with a south.sparkplug section")
    p.add_argument("--broker", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--qos", type=int, default=1, choices=[0, 1, 2])
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        adapter = SparkplugAdapter(load_profile(args.profile))
    except (ProfileError, SouthConfigError) as exc:
        raise SystemExit(f"sparkplug: {exc}")
    log.info("asset %s: %s (edge node %s, device %s) -> %s", adapter.asset, adapter.subscription,
             adapter.edge_node, adapter.device, adapter.telemetry_topic)
    username, password = broker_credentials()
    run(adapter, host=args.broker, port=args.port, qos=args.qos, username=username, password=password)
    print(json.dumps(adapter.report(), sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
