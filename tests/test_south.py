"""Southbound adapters and the simulated SCADA test bed (T31; ING-14, ING-15).

Nothing here needs a broker, an OPC UA stack or the dataset. The Sparkplug B
codec is checked on hand-built payloads and, when the protobuf runtime happens
to be installed, against it. The adapters are driven with in-memory messages.
The round trips CSV -> scada_sim -> adapter -> telemetry_json_v1 are held to
the replayer's own messages for the same file, and ingest accepts the result.
The read-only guarantees (ING-15) are negative tests at the wrappers, with fake
clients that record every call that reaches them. Signal names come from the
profile; nothing here names a MetroPT signal.
"""
import asyncio
import datetime as dt
import json
import re
import struct
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
sys.path.insert(0, str(ROOT / "services" / "replayer"))
sys.path.insert(0, str(ROOT / "services" / "ingest"))

import ingest as ingest_service  # noqa: E402
import replayer  # noqa: E402
from pdm_common.features import features_file, load_feature_spec  # noqa: E402
from pdm_common.hashing import canonical_json  # noqa: E402
from pdm_common.profile import analog_names, digital_names, load_profile, signal_names  # noqa: E402
from pdm_common.schema import validate_profile  # noqa: E402
from pdm_common.schemas import validate  # noqa: E402
from pdm_common.timeutil import to_utc  # noqa: E402
from south import opcua_client, scada_sim  # noqa: E402
from south import sparkplug as sp  # noqa: E402

PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
README = ROOT / "services" / "south" / "README.md"
GROUP, EDGE, DEVICE = "pdm_testbed", "scada_sim", "apu"
T0 = dt.datetime(2020, 3, 21)                                  # naive, like the CSV (source_timezone UTC)
T0_UTC = T0.replace(tzinfo=dt.timezone.utc)
MS0 = sp.ms_of(T0_UTC)


def ms(seconds: float = 0) -> int:
    return MS0 + int(seconds * 1000)


def at(seconds: float) -> dt.datetime:
    return T0_UTC + dt.timedelta(seconds=seconds)


def iso(seconds: int) -> str:
    return (T0 + dt.timedelta(seconds=seconds)).isoformat() + "Z"


def south_for(profile: dict) -> dict:
    signals = signal_names(profile)
    return {
        "opcua": {"endpoint": "opc.tcp://localhost:4841/scada-sim/", "publishing_interval": "10s",
                  "tags": {f"ns=2;s=APU.{s}": s for s in signals}},
        "sparkplug": {"group": GROUP, "edge_node": EDGE, "device": DEVICE,
                      "metrics": {f"APU/{s}": s for s in signals}},
    }


@pytest.fixture(scope="module")
def base():
    return load_profile(PROFILE_PATH)


@pytest.fixture(scope="module")
def profile(base):
    return dict(base, south=south_for(base))


@pytest.fixture(scope="module")
def spec(base):
    return load_feature_spec(features_file(base, PROFILE_PATH))


def row(profile: dict, k: int) -> dict:
    """Row k: analog values inside their ranges, different every row; digital 0 or 1."""
    values = {}
    for j, s in enumerate(profile["signals"]["analog"]):
        lo, hi = s["range"]
        values[s["name"]] = round(lo + (hi - lo) * (0.3 + 0.0123 * k + 0.001 * j), 4)
    for j, s in enumerate(profile["signals"]["digital"]):
        values[s["name"]] = float((k + j) % 2)
    return values


def write_csv(path: Path, profile: dict, rows: int = 30, step: int = 10) -> Path:
    """The replayer's CSV format: unnamed index, timestamp, every signal."""
    names = signal_names(profile)
    lines = ["," + ",".join(["timestamp", *names])]
    for k in range(rows):
        values = row(profile, k)
        ts = T0 + dt.timedelta(seconds=step * k)
        lines.append(f"{k},{ts.isoformat(sep=' ')}," + ",".join(f"{values[n]:.4f}" for n in names))
    path.write_text("\n".join(lines) + "\n")
    return path


def replayed(profile: dict, path: Path) -> list:
    """What the replayer publishes for the file: (topic, payload) per row."""
    out = []
    replayer.replay(profile, path, lambda topic, payload: out.append((topic, payload)), speed=1e9)
    return out


def assert_matches_replayer(outs, published, profile):
    """Same records as the replayer's: topic, schema, asset, seq and every value
    bit for bit; ``ts`` the same instant, written in UTC."""
    assert len(outs) == len(published) > 0
    for out, (topic, payload) in zip(outs, published):
        ours, theirs = json.loads(out.payload), json.loads(payload)
        assert validate("telemetry_json_v1", ours) == []
        assert out.topic == topic
        assert ours["ts"].endswith("Z")
        assert to_utc(ours["ts"]) == to_utc(theirs["ts"], profile["source_timezone"])
        assert {k: v for k, v in ours.items() if k != "ts"} == {k: v for k, v in theirs.items() if k != "ts"}


# --- Sparkplug B traffic, built by hand ------------------------------------------------------

def topic(kind: str, device: str = DEVICE) -> str:
    return sp.make_topic(GROUP, kind, EDGE, device if kind[0] == "D" else None)


def payload(metrics, *, seq=None, timestamp=None) -> bytes:
    return sp.encode_payload(sp.Payload(timestamp=timestamp, seq=seq, metrics=list(metrics)))


class Edge:
    """The configured edge node and device; every profile signal is a metric APU/<signal>."""

    def __init__(self, profile: dict, bd_seq: int = 0):
        self.signals = signal_names(profile)
        self.digital = set(digital_names(profile))
        self.name = {s: f"APU/{s}" for s in self.signals}
        self.alias = {s: i for i, s in enumerate(self.signals, start=1)}
        self.bd_seq = bd_seq
        self.seq = 0

    def next_seq(self) -> int:
        seq, self.seq = self.seq, (self.seq + 1) % 256
        return seq

    def dtype(self, s):
        return sp.BOOLEAN if s in self.digital else sp.DOUBLE

    def value(self, s, v):
        return bool(v) if s in self.digital else float(v)

    def nbirth(self, t=0):
        self.seq = 0
        metrics = [sp.Metric(name=sp.BD_SEQ, datatype=sp.INT64, value=self.bd_seq, timestamp=ms(t)),
                   sp.Metric(name=sp.REBIRTH, datatype=sp.BOOLEAN, value=False, timestamp=ms(t))]
        return topic("NBIRTH"), payload(metrics, seq=self.next_seq(), timestamp=ms(t))

    def dbirth(self, values, t=0):
        metrics = [sp.Metric(name=self.name[s], alias=self.alias[s], timestamp=ms(t), datatype=self.dtype(s),
                             value=self.value(s, v)) for s, v in values.items()]
        return topic("DBIRTH"), payload(metrics, seq=self.next_seq(), timestamp=ms(t))

    def ddata(self, values, t, *, by="alias"):
        metrics = [sp.Metric(alias=self.alias[s] if by == "alias" else None, name=self.name[s] if by == "name" else None,
                             timestamp=ms(t), value=self.value(s, v), value_field=sp.VALUE_FIELD[self.dtype(s)])
                   for s, v in values.items()]
        return topic("DDATA"), payload(metrics, seq=self.next_seq(), timestamp=ms(t))

    def raw_ddata(self, metrics, t):
        return topic("DDATA"), payload(metrics, seq=self.next_seq(), timestamp=ms(t))

    def ddeath(self, t):
        return topic("DDEATH"), payload([], seq=self.next_seq(), timestamp=ms(t))

    def ndeath(self, bd_seq=None):
        bd = self.bd_seq if bd_seq is None else bd_seq
        return topic("NDEATH"), payload([sp.Metric(name=sp.BD_SEQ, datatype=sp.INT64, value=bd)])


def feed(adapter, *messages) -> list:
    return [out for t, p in messages for out in adapter.handle(t, p)]


def docs(outs) -> list:
    return [json.loads(o.payload) for o in outs]


REBIRTH_CMD = sp.encode_payload(sp.Payload(timestamp=ms(0), metrics=[
    sp.Metric(name=sp.REBIRTH, datatype=sp.BOOLEAN, value=True)]))


# --- codec -------------------------------------------------------------------------------------

def test_varints_and_field_keys_follow_protobuf():
    # the protobuf documentation's example: field 1, varint 150 -> 08 96 01
    assert sp.encode_payload(sp.Payload(timestamp=150)) == bytes.fromhex("089601")
    assert sp.encode_payload(sp.Payload(seq=0)) == bytes.fromhex("1800")
    assert sp.encode_payload(sp.Payload(timestamp=2**64 - 1)) == bytes.fromhex("08ffffffffffffffffff01")
    assert sp.decode_payload(bytes.fromhex("08ac02")).timestamp == 300
    assert sp.decode_payload(b"") == sp.Payload()


def test_round_trip_of_every_value_type():
    p = sp.Payload(timestamp=ms(0), seq=255, metrics=[
        sp.Metric(name="i8", alias=1, timestamp=ms(1), datatype=sp.INT8, value=-3),
        sp.Metric(name="i16", alias=2, datatype=sp.INT16, value=-300),
        sp.Metric(name="i32", alias=3, datatype=sp.INT32, value=-70000),
        sp.Metric(name="i64", alias=4, datatype=sp.INT64, value=-(2**40)),
        sp.Metric(name="u8", datatype=sp.UINT8, value=200),
        sp.Metric(name="u16", datatype=sp.UINT16, value=60000),
        sp.Metric(name="u32", datatype=sp.UINT32, value=4_000_000_000),
        sp.Metric(name="u64", datatype=sp.UINT64, value=2**64 - 1),
        sp.Metric(name="float", datatype=sp.FLOAT, value=1.5),
        sp.Metric(name="double", datatype=sp.DOUBLE, value=3.3333),
        sp.Metric(name="true", datatype=sp.BOOLEAN, value=True),
        sp.Metric(name="false", datatype=sp.BOOLEAN, value=False),
        sp.Metric(name="string", datatype=sp.STRING, value="Druck über 9 bar ✓"),
        sp.Metric(name="text", datatype=sp.TEXT, value=""),
        sp.Metric(name="datetime", datatype=sp.DATETIME, value=ms(5)),
        sp.Metric(name="bytes", datatype=sp.BYTES, value=b"\x00\xff"),
        sp.Metric(name="null", datatype=sp.DOUBLE, is_null=True),
        sp.Metric(name="flags", datatype=sp.DOUBLE, value=0.0, is_historical=True, is_transient=True),
        sp.Metric(alias=9, timestamp=ms(2), value=-1.25, value_field=sp.DOUBLE_VALUE),    # DATA style: no datatype
    ])
    data = sp.encode_payload(p)
    back = sp.decode_payload(data)
    assert back == p
    assert sp.encode_payload(back) == data


def test_floats_are_32_bit_and_doubles_64_bit():
    m = sp.decode_payload(sp.encode_payload(sp.Payload(metrics=[
        sp.Metric(name="f", datatype=sp.FLOAT, value=0.1), sp.Metric(name="d", datatype=sp.DOUBLE, value=0.1)]))).metrics
    assert m[0].value == struct.unpack("<f", struct.pack("<f", 0.1))[0] != 0.1
    assert m[1].value == 0.1


def test_signed_integers_in_the_unsigned_fields():
    """Int8 to Int32 travel in uint32 and Int64 in uint64, two's complement;
    encoders that sign-extend and encoders that mask both decode right."""
    assert sp.encode_metric(sp.Metric(datatype=sp.INT32, value=-5)) == bytes.fromhex("2003" + "50fbffffff0f")
    for int_value in ("ff01", "ffffffff0f", "ffffffffffffffffff01"):     # 255, 2**32 - 1, 2**64 - 1
        metric = bytes.fromhex("2001" + "50" + int_value)                # datatype Int8, int_value
        data = bytes([0x12, len(metric)]) + metric
        assert sp.decode_payload(data).metrics[0].value == -1
    # without a datatype on the wire (DATA), the raw value stays; typed() reads it with the birth's datatype
    raw = sp.decode_payload(sp.encode_payload(sp.Payload(metrics=[
        sp.Metric(alias=1, value=-5, value_field=sp.INT_VALUE)]))).metrics[0]
    assert raw.value == 2**32 - 5
    assert sp.typed(raw.value, sp.INT32) == -5 and sp.typed(-5, sp.INT32) == -5
    assert sp.metric_number(raw, sp.INT16) == -5.0


def _field(number: int, wire: int, data: bytes) -> bytes:
    key = sp._key(number, wire)
    if wire == sp.LEN:
        return key + sp._varint(len(data)) + data
    return key + data


def test_unknown_fields_are_skipped():
    clean = sp.Payload(timestamp=ms(0), seq=1, metrics=[
        sp.Metric(name="a", alias=1, timestamp=ms(0), datatype=sp.DOUBLE, value=2.5)])
    metric = (_field(8, sp.LEN, _field(2, sp.LEN, b"text/plain"))     # metadata
              + _field(9, sp.LEN, _field(1, sp.LEN, b"engUnit"))     # properties
              + sp.encode_metric(clean.metrics[0])
              + _field(17, sp.LEN, b"\x08\x01")                      # a dataset value, then the double again
              + sp.encode_metric(sp.Metric(datatype=sp.DOUBLE, value=2.5))[2:]
              + _field(20, sp.VARINT, sp._varint(12345))
              + _field(21, sp.I32, b"\x00\x00\x80\x3f")
              + _field(22, sp.I64, b"\x00" * 8))
    data = (_field(1, sp.VARINT, sp._varint(ms(0)))
            + _field(4, sp.LEN, b"a uuid")                            # uuid
            + _field(2, sp.LEN, metric)
            + _field(5, sp.LEN, b"\x01\x02\x03")                      # body
            + _field(3, sp.VARINT, b"\x01")
            + _field(6, sp.VARINT, b"\x07")                           # extensions
            + _field(1000, sp.I64, b"\x01" * 8)
            + _field(1001, sp.I32, b"\x01" * 4))
    assert sp.decode_payload(data) == clean


@pytest.mark.parametrize("data, problem", [
    (b"\x08\x96", "truncated varint"),
    (b"\x12\x05\x01", "truncated length"),
    (b"\x09\x00\x00", "truncated 64-bit"),
    (b"\x00\x01", "field number 0"),
    (b"\x0b\x0c", "groups"),
    (b"\x0a\x01\x00", "wire type 2, expected 0"),                     # timestamp sent as bytes
    (b"\x12\x03\x0a\x01\xff", "not UTF-8"),                           # metric name
    (b"\x08" + b"\xff" * 10 + b"\x01", "longer than 10 bytes"),
    (b"\x08" + b"\xff" * 9 + b"\x7f", "does not fit 64 bits"),
])
def test_malformed_payloads_raise(data, problem):
    with pytest.raises(sp.DecodeError, match=problem):
        sp.decode_payload(data)


def test_text_is_not_a_payload():
    with pytest.raises(sp.DecodeError, match="bytes"):
        sp.decode_payload('{"seq": 1}')


@pytest.mark.parametrize("metric", [
    sp.Metric(name="x", datatype=sp.INT32, value=2**32),
    sp.Metric(name="x", datatype=sp.INT64, value=-(2**63) - 1),
    sp.Metric(name="x", datatype=sp.BOOLEAN, value=1),
    sp.Metric(name="x", datatype=sp.FLOAT, value=1e39),
    sp.Metric(name="x", datatype=sp.STRING, value=5),
    sp.Metric(name="x", datatype=sp.DATASET, value=b"\x00"),                  # no value field for it here
    sp.Metric(name="x", alias=-1),
])
def test_values_that_cannot_be_encoded(metric):
    with pytest.raises(sp.EncodeError):
        sp.encode_payload(sp.Payload(metrics=[metric]))


def test_codec_matches_the_protobuf_runtime():
    """Byte for byte against Google's implementation of the same message
    definition (the subset of sparkplug_b.proto), both ways."""
    pytest.importorskip("google.protobuf")
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

    F = descriptor_pb2.FieldDescriptorProto
    fdp = descriptor_pb2.FileDescriptorProto(name="sparkplug_b_subset.proto", package="spb", syntax="proto2")
    pl = fdp.message_type.add(name="Payload")
    metric = pl.nested_type.add(name="Metric")
    metric.oneof_decl.add(name="value")
    for name, number, ftype in (("name", 1, F.TYPE_STRING), ("alias", 2, F.TYPE_UINT64), ("timestamp", 3, F.TYPE_UINT64),
                                ("datatype", 4, F.TYPE_UINT32), ("is_historical", 5, F.TYPE_BOOL),
                                ("is_transient", 6, F.TYPE_BOOL), ("is_null", 7, F.TYPE_BOOL)):
        metric.field.add(name=name, number=number, type=ftype, label=F.LABEL_OPTIONAL)
    for name, number, ftype in (("int_value", 10, F.TYPE_UINT32), ("long_value", 11, F.TYPE_UINT64),
                                ("float_value", 12, F.TYPE_FLOAT), ("double_value", 13, F.TYPE_DOUBLE),
                                ("boolean_value", 14, F.TYPE_BOOL), ("string_value", 15, F.TYPE_STRING),
                                ("bytes_value", 16, F.TYPE_BYTES)):
        metric.field.add(name=name, number=number, type=ftype, label=F.LABEL_OPTIONAL, oneof_index=0)
    pl.field.add(name="timestamp", number=1, type=F.TYPE_UINT64, label=F.LABEL_OPTIONAL)
    pl.field.add(name="metrics", number=2, type=F.TYPE_MESSAGE, label=F.LABEL_REPEATED, type_name=".spb.Payload.Metric")
    pl.field.add(name="seq", number=3, type=F.TYPE_UINT64, label=F.LABEL_OPTIONAL)
    pl.field.add(name="uuid", number=4, type=F.TYPE_STRING, label=F.LABEL_OPTIONAL)
    pl.field.add(name="body", number=5, type=F.TYPE_BYTES, label=F.LABEL_OPTIONAL)
    pool = descriptor_pool.DescriptorPool()
    pool.AddSerializedFile(fdp.SerializeToString())
    desc = pool.FindMessageTypeByName("spb.Payload")
    try:
        Pb = message_factory.GetMessageClass(desc)
    except AttributeError:                                   # protobuf before 4.21
        Pb = message_factory.MessageFactory(pool).GetPrototype(desc)

    ours = sp.Payload(timestamp=ms(0), seq=7, metrics=[
        sp.Metric(name="a", alias=1, timestamp=ms(0), datatype=sp.DOUBLE, value=3.3333),
        sp.Metric(name="b", alias=2, datatype=sp.INT32, value=-5),
        sp.Metric(name="c", datatype=sp.BOOLEAN, value=True),
        sp.Metric(name="d", datatype=sp.FLOAT, value=1.5),
        sp.Metric(name="e", datatype=sp.STRING, value="ü"),
        sp.Metric(name="f", datatype=sp.UINT64, value=2**64 - 1),
        sp.Metric(name="g", datatype=sp.DOUBLE, is_null=True),
        sp.Metric(name="h", datatype=sp.BOOLEAN, value=False, is_historical=True),
        sp.Metric(alias=3, timestamp=ms(1), value=2.5, value_field=sp.DOUBLE_VALUE),
    ])
    theirs = Pb(timestamp=ms(0), seq=7)
    theirs.metrics.add(name="a", alias=1, timestamp=ms(0), datatype=sp.DOUBLE, double_value=3.3333)
    theirs.metrics.add(name="b", alias=2, datatype=sp.INT32, int_value=(-5) & 0xFFFFFFFF)
    theirs.metrics.add(name="c", datatype=sp.BOOLEAN, boolean_value=True)
    theirs.metrics.add(name="d", datatype=sp.FLOAT, float_value=1.5)
    theirs.metrics.add(name="e", datatype=sp.STRING, string_value="ü")
    theirs.metrics.add(name="f", datatype=sp.UINT64, long_value=2**64 - 1)
    theirs.metrics.add(name="g", datatype=sp.DOUBLE, is_null=True)
    theirs.metrics.add(name="h", datatype=sp.BOOLEAN, is_historical=True, boolean_value=False)
    theirs.metrics.add(alias=3, timestamp=ms(1), double_value=2.5)

    assert sp.encode_payload(ours) == theirs.SerializeToString()
    parsed = Pb()
    parsed.ParseFromString(sp.encode_payload(ours))
    assert parsed == theirs
    assert sp.decode_payload(theirs.SerializeToString()) == ours
    theirs.uuid, theirs.body = "unknown to this codec", b"\x00\x01"
    assert sp.decode_payload(theirs.SerializeToString()) == ours


# --- topics ------------------------------------------------------------------------------------

def test_topic_parsing():
    assert sp.parse_topic("spBv1.0/g/NBIRTH/e") == sp.SparkplugTopic("NBIRTH", "g", "e")
    assert sp.parse_topic("spBv1.0/g/DDATA/e/d") == sp.SparkplugTopic("DDATA", "g", "e", "d")
    assert sp.parse_topic("spBv1.0/STATE/host") == sp.SparkplugTopic("STATE", host_id="host")
    assert sp.parse_topic("spBv1.0/g/DCMD/e/d").is_command and sp.parse_topic("spBv1.0/g/NCMD/e").is_command
    assert not sp.parse_topic("spBv1.0/g/NDATA/e").is_command
    for kind in sp.NODE_TYPES:
        assert sp.parse_topic(sp.make_topic("g", kind, "e")).msg_type == kind
    for kind in sp.DEVICE_TYPES:
        assert sp.parse_topic(sp.make_topic("g", kind, "e", "d")).device == "d"
    for bad, problem in (("spAv1.0/g/NDATA/e", "namespace"), ("spBv1.0/g/NDATA/e/d", "takes no device"),
                         ("spBv1.0/g/DDATA/e", "needs a device"), ("spBv1.0/g/XDATA/e", "unknown message type"),
                         ("spBv1.0/g/NDATA", "is not"), ("spBv1.0/g/DDATA/e/d/x", "is not"),
                         ("spBv1.0/g//NDATA/e", "empty level"), ("spBv1.0/+/NDATA/e", "wildcard"),
                         ("spBv1.0/g/DDATA/e/#", "wildcard"), ("pdm/x/telemetry", "namespace")):
        with pytest.raises(sp.TopicError, match=problem):
            sp.parse_topic(bad)


# --- the Sparkplug adapter -------------------------------------------------------------------

def test_birth_then_data_gives_telemetry(profile):
    adapter, edge = sp.SparkplugAdapter(profile), Edge(profile)
    rows = [row(profile, k) for k in range(3)]
    assert feed(adapter, edge.nbirth(), edge.dbirth(rows[0])) == []          # a birth is not a record
    outs = feed(adapter, *[edge.ddata(rows[k], 10 * k) for k in range(3)])
    assert [o.topic for o in outs] == [profile["topics"]["telemetry"]] * 3
    for k, (out, doc) in enumerate(zip(outs, docs(outs))):
        assert validate("telemetry_json_v1", doc) == []
        assert out.payload == canonical_json(doc)
        assert doc == {"schema": profile["topics"]["payload_schema"], "asset": profile["asset"]["id"], "seq": k,
                       "ts": iso(10 * k), "signals": rows[k]}
    assert adapter.counts["records"] == 3 and adapter.counts["malformed"] == 0
    assert adapter.subscription == f"spBv1.0/{GROUP}/#"


def test_report_by_exception_carries_the_last_values(profile):
    adapter, edge = sp.SparkplugAdapter(profile), Edge(profile)
    first = row(profile, 0)
    changed = analog_names(profile)[0]
    feed(adapter, edge.nbirth(), edge.dbirth(first))
    (doc,) = docs(feed(adapter, edge.ddata({changed: 1.234}, 10)))
    assert doc["signals"] == dict(first, **{changed: 1.234})
    assert doc["ts"] == iso(10)


def test_no_record_until_every_signal_has_a_value(profile):
    adapter, edge = sp.SparkplugAdapter(profile), Edge(profile)
    full = row(profile, 0)
    last = signal_names(profile)[-1]
    partial = {s: v for s, v in full.items() if s != last}
    feed(adapter, edge.nbirth(), edge.dbirth(partial))
    assert feed(adapter, edge.ddata(partial, 10)) == []
    assert adapter.counts["incomplete"] == 1
    (doc,) = docs(feed(adapter, edge.ddata({last: full[last]}, 20, by="name")))   # no alias was declared for it
    assert doc["signals"] == full and doc["seq"] == 0


def test_names_aliases_and_metrics_outside_the_mapping(profile):
    adapter, edge = sp.SparkplugAdapter(profile), Edge(profile)
    first = analog_names(profile)[0]
    feed(adapter, edge.nbirth(), edge.dbirth(row(profile, 0)))
    assert len(feed(adapter, edge.ddata(row(profile, 1), 10, by="name"))) == 1
    (doc,) = docs(feed(adapter, edge.raw_ddata([
        sp.Metric(alias=999, value=1.0, value_field=sp.DOUBLE_VALUE, timestamp=ms(25)),
        sp.Metric(name="APU/not_a_signal", value=1.0, value_field=sp.DOUBLE_VALUE, timestamp=ms(25)),
        sp.Metric(alias=edge.alias[first], value=2.5, value_field=sp.DOUBLE_VALUE)], 30)))
    assert adapter.counts["unknown_alias"] == 1 and adapter.counts["unknown_metric"] == 1
    assert doc["signals"][first] == 2.5
    assert doc["ts"] == iso(30)            # the applied metric has no timestamp: the payload's


def test_other_groups_edge_nodes_and_devices_are_ignored(profile):
    adapter, edge = sp.SparkplugAdapter(profile), Edge(profile)
    feed(adapter, edge.nbirth(), edge.dbirth(row(profile, 0)))
    _, data = edge.ddata(row(profile, 1), 10)
    for other in (sp.make_topic("other_group", "DDATA", EDGE, DEVICE), sp.make_topic(GROUP, "DDATA", "other_node", DEVICE),
                  sp.make_topic(GROUP, "DDATA", EDGE, "other_device"), sp.make_topic(GROUP, "DDEATH", EDGE, "other_device"),
                  "spBv1.0/STATE/some_host"):
        assert adapter.handle(other, data) == []
    assert adapter.handle(profile["topics"]["telemetry"], b"{}") == []
    assert adapter.counts["other_source"] == 5 and adapter.counts["bad_topic"] == 1
    assert adapter.device_online and len(feed(adapter, edge.ddata(row(profile, 2), 20))) == 1


def test_data_before_a_birth_is_ignored(profile):
    adapter, edge = sp.SparkplugAdapter(profile), Edge(profile)
    assert feed(adapter, edge.ddata(row(profile, 0), 0)) == []
    assert adapter.counts["before_birth"] == 1 and adapter.counts["records"] == 0


def test_death_stops_records_until_the_next_birth(profile):
    adapter, edge = sp.SparkplugAdapter(profile), Edge(profile, bd_seq=4)
    feed(adapter, edge.nbirth(), edge.dbirth(row(profile, 0)))
    assert len(feed(adapter, edge.ddata(row(profile, 1), 10))) == 1
    feed(adapter, edge.ddeath(15))
    assert feed(adapter, edge.ddata(row(profile, 2), 20)) == []
    assert adapter.counts["before_birth"] == 1 and not adapter.device_online
    feed(adapter, edge.dbirth(row(profile, 3), 30))
    assert [d["seq"] for d in docs(feed(adapter, edge.ddata(row(profile, 4), 40)))] == [1]   # seq carries on
    # an NDEATH left over from an earlier session (another bdSeq) changes nothing ...
    feed(adapter, edge.ndeath(bd_seq=3))
    assert adapter.counts["stale_ndeath"] == 1 and len(feed(adapter, edge.ddata(row(profile, 5), 50))) == 1
    # ... this session's ends it, devices included
    feed(adapter, edge.ndeath())
    assert adapter.counts["ndeath"] == 1 and not adapter.node_online
    assert feed(adapter, edge.ddata(row(profile, 6), 60)) == []
    # after a new NBIRTH the device still needs its own birth
    feed(adapter, edge.nbirth(70))
    assert feed(adapter, edge.ddata(row(profile, 7), 70)) == []
    feed(adapter, edge.dbirth(row(profile, 8), 80))
    assert len(feed(adapter, edge.ddata(row(profile, 9), 90))) == 1
    assert adapter.counts["before_birth"] == 3


def test_malformed_payloads_are_counted_not_fatal(profile):
    adapter, edge = sp.SparkplugAdapter(profile), Edge(profile)
    feed(adapter, edge.nbirth(), edge.dbirth(row(profile, 0)))
    assert adapter.handle(topic("DDATA"), b"\x0b\x00 not protobuf") == []
    assert adapter.handle(topic("DBIRTH"), b"\x12\x05\x01") == []
    assert adapter.counts["malformed"] == 2
    assert len(feed(adapter, edge.ddata(row(profile, 1), 10))) == 1


def test_null_and_non_numeric_values_clear_their_signal(profile):
    adapter, edge = sp.SparkplugAdapter(profile), Edge(profile)
    first = analog_names(profile)[0]
    feed(adapter, edge.nbirth(), edge.dbirth(row(profile, 0)))
    alias = edge.alias[first]
    assert feed(adapter, edge.raw_ddata([sp.Metric(alias=alias, is_null=True, timestamp=ms(10))], 10)) == []
    assert adapter.counts["null"] == 1 and adapter.counts["incomplete"] == 1
    assert len(feed(adapter, edge.ddata({first: 2.0}, 20))) == 1                 # a value again: records again
    for value, field in (("high", sp.STRING_VALUE), (float("nan"), sp.DOUBLE_VALUE), (b"\x01", sp.BYTES_VALUE)):
        assert feed(adapter, edge.raw_ddata([sp.Metric(alias=alias, value=value, value_field=field,
                                                       timestamp=ms(30))], 30)) == []
        assert len(feed(adapter, edge.ddata({first: 2.0}, 40))) == 1
    assert adapter.counts["non_numeric"] == 3


def test_historical_values_do_not_change_the_current_record(profile):
    adapter, edge = sp.SparkplugAdapter(profile), Edge(profile)
    a, b = analog_names(profile)[:2]
    first = row(profile, 0)
    feed(adapter, edge.nbirth(), edge.dbirth(first))
    (doc,) = docs(feed(adapter, edge.raw_ddata([
        sp.Metric(alias=edge.alias[a], value=99.0, value_field=sp.DOUBLE_VALUE, timestamp=ms(5), is_historical=True),
        sp.Metric(alias=edge.alias[b], value=7.0, value_field=sp.DOUBLE_VALUE, timestamp=ms(20))], 21)))
    assert doc["signals"][a] == first[a] and doc["signals"][b] == 7.0
    assert doc["ts"] == iso(20) and adapter.counts["historical"] == 1


def test_sequence_gaps_are_counted_and_wrap_is_not_a_gap(profile):
    adapter, edge = sp.SparkplugAdapter(profile), Edge(profile)
    feed(adapter, edge.nbirth(), edge.dbirth(row(profile, 0)))
    edge.next_seq()                                                     # one message lost on the way
    assert len(feed(adapter, edge.ddata(row(profile, 1), 10))) == 1
    assert adapter.counts["seq_gaps"] == 1
    feed(adapter, *[edge.ddata(row(profile, k), 10 * k) for k in range(2, 300)])   # seq wraps 255 -> 0
    assert adapter.counts["seq_gaps"] == 1 and adapter.counts["records"] == 299


def test_the_mapping_must_cover_every_signal_once(base):
    signals = signal_names(base)
    full = {f"APU/{s}": s for s in signals}

    def adapter(metrics=None, **ids):
        cfg = dict({"group": GROUP, "edge_node": EDGE, "device": DEVICE, "metrics": metrics or full}, **ids)
        return sp.SparkplugAdapter(dict(base, south={"sparkplug": cfg}))

    adapter()
    missing = {k: v for k, v in full.items() if v != signals[-1]}
    with pytest.raises(sp.SouthConfigError, match=re.escape(f"not mapped: {signals[-1]}")):
        adapter(missing)
    with pytest.raises(sp.SouthConfigError, match=re.escape(f"mapped more than once: {signals[0]}")):
        adapter(dict(full, extra=signals[0]))
    with pytest.raises(sp.SouthConfigError, match="no_such_signal"):
        adapter(dict(full, extra="no_such_signal"))
    with pytest.raises(sp.SouthConfigError, match="wildcard"):
        adapter(edge_node="node/+")
    with pytest.raises(sp.SouthConfigError, match="device"):
        adapter(device="")
    with pytest.raises(sp.SouthConfigError, match="south.sparkplug"):
        sp.SparkplugAdapter({k: v for k, v in base.items() if k != "south"})


def test_a_telemetry_topic_in_the_sparkplug_namespace_is_refused(profile):
    bad = dict(profile, topics=dict(profile["topics"], telemetry=sp.make_topic(GROUP, "DCMD", EDGE, DEVICE)))
    with pytest.raises(sp.SouthConfigError, match="Sparkplug namespace"):
        sp.SparkplugAdapter(bad)
    with pytest.raises(sp.SouthConfigError, match="Sparkplug namespace"):
        opcua_client.OpcUaMapper(bad)


# --- read-only publishing (ING-15) -----------------------------------------------------------

class FakeMqtt:
    """Records what reaches the MQTT client."""

    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))


def test_the_guard_refuses_commands_and_every_other_topic(profile):
    adapter = sp.SparkplugAdapter(profile)
    adapter.guard_publish(profile["topics"]["telemetry"])                 # the one topic allowed
    for refused in (sp.make_topic(GROUP, "NCMD", EDGE), sp.make_topic(GROUP, "DCMD", EDGE, DEVICE),
                    sp.make_topic(GROUP, "DDATA", EDGE, DEVICE), sp.make_topic(GROUP, "NBIRTH", EDGE),
                    "spBv1.0/STATE/pdm-agent", "spBv1.0/anything/else", "spBv1.0",
                    profile["topics"]["status"], profile["topics"]["telemetry"] + "/x", "#", ""):
        with pytest.raises(sp.CommandRefused):
            adapter.guard_publish(refused)
    with pytest.raises(sp.CommandRefused, match="command to the plant"):
        sp.guard_publish(sp.make_topic(GROUP, "DCMD", EDGE, DEVICE), {sp.make_topic(GROUP, "DCMD", EDGE, DEVICE)})


def test_publishing_a_command_raises_before_the_client(profile):
    client = FakeMqtt()
    publisher = sp.GuardedPublisher(client, {profile["topics"]["telemetry"]})
    with pytest.raises(sp.CommandRefused, match="command"):
        publisher.publish(sp.make_topic(GROUP, "DCMD", EDGE, DEVICE), REBIRTH_CMD)
    with pytest.raises(sp.CommandRefused, match="command"):
        publisher.publish(sp.make_topic(GROUP, "NCMD", EDGE), REBIRTH_CMD)
    with pytest.raises(sp.CommandRefused):
        publisher.publish("spBv1.0/STATE/pdm-agent", b'{"online": true}')
    assert client.published == []
    publisher.publish(profile["topics"]["telemetry"], "{}")
    assert client.published == [(profile["topics"]["telemetry"], "{}", 1, False)]


def test_the_adapter_only_ever_returns_telemetry(profile):
    adapter, edge = sp.SparkplugAdapter(profile), Edge(profile)
    traffic = [edge.nbirth(), edge.dbirth(row(profile, 0))] + [edge.ddata(row(profile, k), 10 * k) for k in range(1, 5)]
    traffic += [(sp.make_topic(GROUP, "DCMD", EDGE, DEVICE), REBIRTH_CMD), (sp.make_topic(GROUP, "NCMD", EDGE), REBIRTH_CMD),
                edge.ddeath(60), edge.ndeath()]
    outs = feed(adapter, *traffic)
    assert len(outs) == 4 and {o.topic for o in outs} == {profile["topics"]["telemetry"]}
    assert adapter.counts["commands_seen"] == 2
    for out in outs:
        adapter.guard_publish(out.topic)


# --- the OPC UA mapper -----------------------------------------------------------------------

@pytest.fixture(scope="module")
def node_of(profile):
    return {s: n for n, s in profile["south"]["opcua"]["tags"].items()}


def notify_row(mapper, node_of, values, moment) -> list:
    return [out for s, v in values.items() for out in mapper.notify(node_of[s], v, moment)]


def test_opcua_one_record_per_interval(profile, node_of):
    mapper = opcua_client.OpcUaMapper(profile)
    rows = [row(profile, k) for k in range(4)]
    outs = []
    for k, values in enumerate(rows):
        outs += notify_row(mapper, node_of, values, at(10 * k))
    assert len(outs) == 2                  # intervals 0 and 1, closed by the notifications of 2 and 3
    outs += mapper.flush()
    assert [o.topic for o in outs] == [profile["topics"]["telemetry"]] * 4
    assert all(validate("telemetry_json_v1", d) == [] for d in docs(outs))
    assert [(d["seq"], d["ts"], d["signals"]) for d in docs(outs)] == [(k, iso(10 * k), rows[k]) for k in range(4)]
    assert mapper.flush() == [] and mapper.counts["records"] == 4


def test_opcua_unchanged_signals_keep_their_value(profile, node_of):
    mapper = opcua_client.OpcUaMapper(profile)
    first = row(profile, 0)
    a, d = analog_names(profile)[0], digital_names(profile)[0]
    outs = notify_row(mapper, node_of, first, at(0))
    outs += mapper.notify(node_of[a], 5.5, at(12))
    outs += mapper.notify(node_of[d], not bool(first[d]), at(25))   # OPC UA Booleans become 1.0 / 0.0
    got = docs(outs + mapper.flush())
    assert [g["ts"] for g in got] == [iso(0), iso(12), iso(25)]
    assert got[1]["signals"] == dict(first, **{a: 5.5})
    assert got[2]["signals"] == dict(first, **{a: 5.5, d: 1.0 - first[d]})


def test_opcua_late_unknown_and_unstamped_notifications(profile, node_of):
    mapper = opcua_client.OpcUaMapper(profile)
    a = analog_names(profile)[0]
    notify_row(mapper, node_of, row(profile, 0), at(0))
    assert len(notify_row(mapper, node_of, row(profile, 1), at(30))) == 1    # interval 0 closed
    assert mapper.notify(node_of[a], 1.0, at(5)) == [] and mapper.counts["late"] == 1
    assert mapper.notify("ns=2;s=APU.unknown", 1.0, at(31)) == [] and mapper.counts["unknown_node"] == 1
    assert mapper.notify(node_of[a], 1.0, None) == [] and mapper.counts["no_timestamp"] == 1
    assert mapper.notify(node_of[a], 1.0, "not a time") == [] and mapper.counts["no_timestamp"] == 2


def test_opcua_batch_delivered_tag_by_tag(profile, node_of):
    """A publish response can carry two samples per tag, tag by tag: still two records."""
    mapper = opcua_client.OpcUaMapper(profile)
    rows = [row(profile, 0), row(profile, 1)]
    outs = [out for s in signal_names(profile) for k in (0, 1) for out in mapper.notify(node_of[s], rows[k][s], at(10 * k))]
    outs += mapper.flush()
    assert [d["signals"] for d in docs(outs)] == rows and mapper.counts["late"] == 0


def test_opcua_two_samples_in_one_interval_give_the_later_one(profile, node_of):
    mapper = opcua_client.OpcUaMapper(profile)
    notify_row(mapper, node_of, row(profile, 0), at(1))
    notify_row(mapper, node_of, row(profile, 1), at(9))
    (doc,) = docs(mapper.flush())
    assert doc["signals"] == row(profile, 1) and doc["ts"] == iso(9)


def test_opcua_bad_values_clear_their_signal(profile, node_of):
    mapper = opcua_client.OpcUaMapper(profile)
    a, b = analog_names(profile)[:2]
    notify_row(mapper, node_of, row(profile, 0), at(0))
    mapper.notify(node_of[a], None, at(10))                      # Bad status
    mapper.notify(node_of[b], "high", at(10))
    assert len(mapper.flush()) == 1                              # interval 1 has no value for a or b
    assert (mapper.counts["null"], mapper.counts["non_numeric"], mapper.counts["incomplete"]) == (1, 1, 1)
    mapper.notify(node_of[a], 1.0, at(20))
    mapper.notify(node_of[b], 2.0, at(20))
    (doc,) = docs(mapper.flush())
    assert (doc["signals"][a], doc["signals"][b]) == (1.0, 2.0)


def test_opcua_tick_closes_after_a_quiet_spell(profile, node_of):
    now = [100.0]
    mapper = opcua_client.OpcUaMapper(profile, clock=lambda: now[0])
    notify_row(mapper, node_of, row(profile, 0), at(0))
    assert mapper.tick() == []
    now[0] += 19.9
    assert mapper.tick() == []                                   # two intervals (lateness 1) not yet over
    now[0] += 0.2
    assert len(mapper.tick()) == 1 and mapper.tick() == []


def test_opcua_mapper_configuration(base, profile):
    tags = dict(profile["south"]["opcua"]["tags"])
    tags.popitem()
    with pytest.raises(sp.SouthConfigError, match="not mapped"):
        opcua_client.OpcUaMapper(dict(base, south={"opcua": dict(profile["south"]["opcua"], tags=tags)}))
    with pytest.raises(sp.SouthConfigError, match="publishing_interval"):
        opcua_client.OpcUaMapper(dict(base, south={"opcua": dict(profile["south"]["opcua"], publishing_interval="soon")}))
    with pytest.raises(sp.SouthConfigError, match="south.opcua"):
        opcua_client.OpcUaMapper({k: v for k, v in base.items() if k != "south"})


class _Status:
    def __init__(self, good):
        self.good = good

    def is_good(self):
        return self.good


def _notification(source=None, server=None, good=True):
    """The parts of asyncua's DataChangeNotif the handler reads."""
    value = SimpleNamespace(SourceTimestamp=source, ServerTimestamp=server, StatusCode=_Status(good))
    return SimpleNamespace(monitored_item=SimpleNamespace(Value=value))


def test_subscription_handler(profile, node_of):
    mapper = opcua_client.OpcUaMapper(profile)
    sent = []
    handler = opcua_client.SubscriptionHandler(mapper, {("id", n): n for n in mapper.node_ids}, sent.append)
    first, second = row(profile, 0), row(profile, 1)
    a = analog_names(profile)[0]
    for s, v in first.items():                                  # naive source timestamps: UTC, as OPC UA
        handler.datachange_notification(SimpleNamespace(nodeid=("id", node_of[s])), v,
                                        _notification(source=at(0).replace(tzinfo=None)))
    for s, v in second.items():                                 # no source timestamp: the server's
        handler.datachange_notification(SimpleNamespace(nodeid=("id", node_of[s])), v, _notification(server=at(10)))
    handler.datachange_notification(SimpleNamespace(nodeid=("id", node_of[a])), 3.0,
                                    _notification(source=at(20), good=False))
    handler.datachange_notification(SimpleNamespace(nodeid=("id", "ns=9;s=elsewhere")), 3.0, _notification(source=at(20)))
    sent += mapper.flush()
    assert [(d["ts"], d["signals"]) for d in docs(sent)] == [(iso(0), first), (iso(10), second)]
    assert mapper.counts["null"] == 1 and mapper.counts["unknown_node"] == 1 and mapper.counts["incomplete"] == 1


# --- the read-only OPC UA client (ING-15) ----------------------------------------------------

class FakeNode:
    """Stands in for an asyncua Node; every call that reaches it is logged."""

    def __init__(self, log, nodeid, value=0.0, children=0):
        self.log, self.nodeid, self.value = log, nodeid, value
        self.children = [FakeNode(log, f"{nodeid}/{i}", value=float(i)) for i in range(children)]

    async def read_value(self):
        self.log.append(("read_value", self.nodeid))
        return self.value

    async def get_children(self):
        self.log.append(("get_children", self.nodeid))
        return self.children

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        self.log.append((name, self.nodeid))

        async def call(*args, **kwargs):
            self.log.append((name, self.nodeid, args))
        return call


class FakeSubscription:
    def __init__(self, log):
        self.log = log

    async def subscribe_data_change(self, nodes, queuesize=0, sampling_interval=0.0):
        self.log.append(("subscribe_data_change", [type(n).__name__ for n in nodes], queuesize, sampling_interval))
        return list(range(len(nodes)))

    async def unsubscribe(self, handle):
        self.log.append(("unsubscribe", handle))

    async def delete(self):
        self.log.append(("delete",))

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        self.log.append((name,))
        return lambda *a, **k: None


class FakeClient:
    """Stands in for asyncua.Client; every call that reaches it is logged."""

    def __init__(self):
        self.log = []

    async def connect(self):
        self.log.append(("connect",))

    async def disconnect(self):
        self.log.append(("disconnect",))

    def get_node(self, nodeid):
        self.log.append(("get_node", nodeid))
        return FakeNode(self.log, nodeid, value=1.5, children=2)

    async def read_values(self, nodes):
        self.log.append(("read_values", [type(n).__name__ for n in nodes]))
        return [n.value for n in nodes]

    async def get_namespace_index(self, uri):
        self.log.append(("get_namespace_index", uri))
        return 2

    async def get_namespace_array(self):
        return ["http://opcfoundation.org/UA/", "urn:server", "urn:plant"]

    async def create_subscription(self, period, handler, **kwargs):
        self.log.append(("create_subscription", period))
        return FakeSubscription(self.log)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        self.log.append((name,))

        async def call(*args, **kwargs):
            self.log.append((name, args))
        return call


CLIENT_REFUSED = ("write_values", "set_values", "add_nodes", "delete_nodes", "add_references", "delete_references",
                  "call", "register_nodes", "uaclient", "nodes", "load_data_type_definitions")
NODE_REFUSED = ("write_value", "write_attribute", "write_params", "set_value", "set_attribute", "set_writable",
                "set_read_only", "call_method", "add_folder", "add_object", "add_variable", "add_property",
                "add_method", "add_reference", "delete", "delete_reference", "register", "session", "server")
SUBSCRIPTION_REFUSED = ("server", "subscribe_events", "create_monitored_items", "modify_monitored_item",
                        "set_monitoring_mode", "set_publishing_mode")


def test_the_read_only_client_reads_browses_and_subscribes():
    fake = FakeClient()
    client = opcua_client.ReadOnlyClient(fake)

    async def session():
        async with client:
            node = client.get_node("ns=2;s=APU.x")
            children = await node.get_children()
            values = [await node.read_value()] + [await c.read_value() for c in children]
            sub = await client.create_subscription(10000.0, handler=None)
            handles = await sub.subscribe_data_change([node, *children], queuesize=10, sampling_interval=10000.0)
            await sub.unsubscribe(handles[0])
            await sub.delete()
            return (node, children, values, await client.read_values(children),
                    await client.get_namespace_index("urn:plant"), await client.get_namespace_array())

    node, children, values, read, index, namespaces = asyncio.run(session())
    assert isinstance(node, opcua_client.ReadOnlyNode) and node.nodeid == "ns=2;s=APU.x"
    assert all(isinstance(c, opcua_client.ReadOnlyNode) for c in children)
    assert values == [1.5, 0.0, 1.0] and read == [0.0, 1.0] and index == 2 and len(namespaces) == 3
    names = [entry[0] for entry in fake.log]
    assert names[0] == "connect" and names[-1] == "disconnect"
    # the wrappers hand asyncua its own objects, never themselves
    assert ("subscribe_data_change", ["FakeNode"] * 3, 10, 10000.0) in fake.log
    assert ("read_values", ["FakeNode"] * 2) in fake.log


def test_the_read_only_client_refuses_writes_before_asyncua_sees_them():
    """ING-15 negative test at the wrapper: every write, call, add, delete or raw
    handle raises WriteRefused, and nothing reaches the fake client."""
    fake = FakeClient()
    client = opcua_client.ReadOnlyClient(fake)
    node = client.get_node("ns=2;s=APU.x")
    (child,) = asyncio.run(node.get_children())[:1]
    sub = asyncio.run(client.create_subscription(1000.0, handler=None))
    before = list(fake.log)
    for name in CLIENT_REFUSED:
        with pytest.raises(opcua_client.WriteRefused, match="read-only"):
            getattr(client, name)
    for target in (node, child):
        for name in NODE_REFUSED:
            with pytest.raises(opcua_client.WriteRefused, match="read-only"):
                getattr(target, name)
    for name in SUBSCRIPTION_REFUSED:
        with pytest.raises(opcua_client.WriteRefused, match="read-only"):
            getattr(sub, name)

    async def writes():
        for attempt in (lambda: node.write_value(42.0), lambda: child.call_method("ns=2;s=Reset"),
                        lambda: node.add_variable(2, "x", 0.0), lambda: client.write_values([node], [1.0]),
                        lambda: client.delete_nodes([node])):
            with pytest.raises(opcua_client.WriteRefused):
                await attempt()

    asyncio.run(writes())
    assert fake.log == before                                   # not one call got through


# --- the simulated SCADA ---------------------------------------------------------------------

def test_read_rows_checks_columns_and_skips_bad_rows(tmp_path, profile):
    path = write_csv(tmp_path / "rows.csv", profile, rows=5)
    lines = path.read_text().splitlines()
    cells = lines[2].split(",")
    cells[3] = "n/a"                                            # row 1 (10 s): one value unreadable
    lines[2] = ",".join(cells)
    path.write_text("\n".join(lines) + "\n")
    counts = Counter()
    rows = list(scada_sim.read_rows(profile, path, counts=counts))
    assert [ts for ts, _ in rows] == [T0 + dt.timedelta(seconds=s) for s in (0, 20, 30, 40)]
    assert (counts["read"], counts["skipped"]) == (5, 1)
    assert rows[0][1] == {s: float(f"{v:.4f}") for s, v in row(profile, 0).items()}
    window = list(scada_sim.read_rows(profile, path, from_ts=T0 + dt.timedelta(seconds=20), limit=2))
    assert [ts for ts, _ in window] == [T0 + dt.timedelta(seconds=20), T0 + dt.timedelta(seconds=30)]
    dropped = signal_names(profile)[-1]
    header = lines[0].split(",")
    lines[0] = ",".join(c if c != dropped else "renamed" for c in header)
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(scada_sim.CsvError, match=re.escape(dropped)):
        list(scada_sim.read_rows(profile, path))


def test_edge_node_sim_session(profile):
    sim = scada_sim.EdgeNodeSim(profile, bd_seq=7)
    names = profile["south"]["sparkplug"]["metrics"]            # metric name -> signal
    digital = set(digital_names(profile))
    first = row(profile, 0)
    t, p = sim.node_birth(T0)
    nbirth = sp.decode_payload(p)
    assert sp.parse_topic(t) == sp.SparkplugTopic("NBIRTH", GROUP, EDGE)
    assert (nbirth.seq, nbirth.timestamp) == (0, ms(0))
    assert {m.name: m.value for m in nbirth.metrics} == {sp.BD_SEQ: 7, sp.REBIRTH: False}
    t, p = sim.device_birth(T0, first)
    dbirth = sp.decode_payload(p)
    assert sp.parse_topic(t) == sp.SparkplugTopic("DBIRTH", GROUP, EDGE, DEVICE) and dbirth.seq == 1
    assert {m.name for m in dbirth.metrics} == set(names)
    assert sorted(m.alias for m in dbirth.metrics) == list(range(1, len(names) + 1))
    for m in dbirth.metrics:
        assert m.datatype == (sp.BOOLEAN if names[m.name] in digital else sp.DOUBLE)
        assert sp.metric_number(m) == first[names[m.name]] and m.timestamp == ms(0)
    t, p = sim.device_data(T0 + dt.timedelta(seconds=10), row(profile, 1))
    ddata = sp.decode_payload(p)
    assert sp.parse_topic(t).msg_type == "DDATA" and ddata.seq == 2
    assert all(m.name is None and m.datatype is None and m.alias and m.timestamp == ms(10) for m in ddata.metrics)
    for _ in range(253):
        sim.device_data(T0, first)
    assert sp.decode_payload(sim.device_data(T0, first)[1]).seq == 0          # 255 wraps to 0
    t, p = sim.device_death(T0)
    assert sp.parse_topic(t).msg_type == "DDEATH" and sp.decode_payload(p).seq == 1
    t, p = sim.node_death()
    ndeath = sp.decode_payload(p)
    assert sp.parse_topic(t).msg_type == "NDEATH" and ndeath.seq is None and ndeath.timestamp is None
    assert [(m.name, m.value) for m in ndeath.metrics] == [(sp.BD_SEQ, 7)]


def test_digital_values_must_be_0_or_1(profile):
    values = dict(row(profile, 0), **{digital_names(profile)[0]: 0.5})
    with pytest.raises(ValueError, match="0 or 1"):
        scada_sim.EdgeNodeSim(profile).device_data(T0, values)
    with pytest.raises(ValueError, match="0 or 1"):
        scada_sim.opcua_updates(profile, [(T0, values)])


def test_step_wait_paces_like_the_replayer():
    t1 = T0 + dt.timedelta(seconds=10)
    assert scada_sim.step_wait(None, T0, 1.0, 60.0) == 0.0
    assert scada_sim.step_wait(T0, t1, 10.0, 60.0) == pytest.approx(1.0)
    assert scada_sim.step_wait(T0, T0 + dt.timedelta(days=3), 10.0, 60.0) == 60.0
    assert scada_sim.step_wait(t1, T0, 1.0, 60.0) == 0.0


# --- round trips: CSV -> simulated SCADA -> adapter -> telemetry_json_v1 ---------------------

def test_csv_through_sparkplug_matches_the_replayer_and_ingest_accepts_it(tmp_path, profile, spec):
    """ING-14's interoperability check without a broker: the same file through the
    replayer and through scada_sim + SparkplugAdapter gives the same records."""
    path = write_csv(tmp_path / "rows.csv", profile)
    published = replayed(profile, path)
    adapter = sp.SparkplugAdapter(profile)
    messages = scada_sim.sparkplug_messages(profile, scada_sim.read_rows(profile, path), bd_seq=3, death=True)
    assert [sp.parse_topic(t).msg_type for t, _ in messages] == ["NBIRTH", "DBIRTH"] + ["DDATA"] * 30 + ["DDEATH", "NDEATH"]
    outs = feed(adapter, *messages)
    assert_matches_replayer(outs, published, profile)
    report = adapter.report()
    assert report["records"] == 30 and report["seq_gaps"] == 0 and report["malformed"] == 0
    assert not report["device_online"] and not report["node_online"]
    service = ingest_service.Ingest(profile, "x" * 64, spec=spec)
    for out in outs:
        service.handle(out.payload)
    assert (service.counts["accepted"], service.counts["quarantined"], service.counts["lost"]) == (30, 0, 0)


def test_csv_through_opcua_matches_the_replayer(tmp_path, profile, spec):
    path = write_csv(tmp_path / "rows.csv", profile)
    published = replayed(profile, path)
    mapper = opcua_client.OpcUaMapper(profile)
    outs = [out for node_id, value, moment in scada_sim.opcua_updates(profile, scada_sim.read_rows(profile, path))
            for out in mapper.notify(node_id, value, moment)] + mapper.flush()
    assert_matches_replayer(outs, published, profile)
    service = ingest_service.Ingest(profile, "x" * 64, spec=spec)
    for out in outs:
        service.handle(out.payload)
    assert (service.counts["accepted"], service.counts["quarantined"]) == (30, 0)


class FakeServer:
    def __init__(self):
        self.rows = []

    async def write_row(self, ts, values):
        self.rows.append((ts, values))


def test_simulate_serves_both_interfaces(tmp_path, profile):
    path = write_csv(tmp_path / "rows.csv", profile, rows=5)
    sent, server = [], FakeServer()
    counts = asyncio.run(scada_sim.simulate(profile, scada_sim.read_rows(profile, path),
                                            publish=lambda t, p: sent.append((t, p)), server=server, speed=1e9))
    assert (counts["rows"], counts["messages"]) == (5, 9)        # NBIRTH, DBIRTH, 5 DDATA, DDEATH, NDEATH
    assert len(server.rows) == 5 and server.rows[0][0] == T0
    assert len(feed(sp.SparkplugAdapter(profile), *sent)) == 5


def test_scada_sim_dry_run(tmp_path, profile, capsys):
    """The command line, without a broker: the logged messages decode back to the rows."""
    prof = tmp_path / "asset_profile.yaml"
    prof.write_text(yaml.safe_dump(profile, sort_keys=False))
    path = write_csv(tmp_path / "rows.csv", profile, rows=12)
    log = tmp_path / "sim.log"
    scada_sim.main(["--profile", str(prof), "--data", str(path), "--dry-run", "--log", str(log), "--speed", "1e9"])
    assert "served 12 of 12 rows" in capsys.readouterr().out
    messages = [(t, bytes.fromhex(h)) for t, h in (line.split(" ") for line in log.read_text().splitlines())]
    assert_matches_replayer(feed(sp.SparkplugAdapter(profile), *messages), replayed(profile, path), profile)


def test_the_commands_stop_on_a_profile_without_a_south_section(tmp_path, base):
    prof = tmp_path / "asset_profile.yaml"
    prof.write_text(yaml.safe_dump({k: v for k, v in base.items() if k != "south"}, sort_keys=False))
    with pytest.raises(SystemExit, match="south.sparkplug"):
        sp.main(["--profile", str(prof)])
    with pytest.raises(SystemExit, match="south.opcua"):
        opcua_client.main(["--profile", str(prof)])
    with pytest.raises(SystemExit, match="south.sparkplug"):
        scada_sim.main(["--profile", str(prof), "--data", "unused.csv", "--dry-run"])


def test_the_proposed_profile_section_validates(base):
    """The south: block in services/south/README.md (or, once it has landed, the
    profile's own) validates under schema 0.3 and drives every component."""
    if "south" in yaml.safe_load(PROFILE_PATH.read_text()):
        data = load_profile(PROFILE_PATH)
    else:
        block = re.search(r"```yaml\n(south:.*?)```", README.read_text(), re.S).group(1)
        text = PROFILE_PATH.read_text() + "\n" + block
        data = yaml.safe_load(text)
        validate_profile(data, text, "asset_profile.yaml + the README's south section")
    assert sorted(data["south"]["sparkplug"]["metrics"].values()) == sorted(signal_names(base))
    assert sorted(data["south"]["opcua"]["tags"].values()) == sorted(signal_names(base))
    sp.SparkplugAdapter(data)
    opcua_client.OpcUaMapper(data)
    scada_sim.EdgeNodeSim(data)
    scada_sim.OpcUaServerSim(data)


def test_the_adapter_loop_reports_its_health_and_metrics(monkeypatch, profile):
    """run() with a stand-in MQTT client: /health follows the broker connection, a message counts
    on the standard metrics, and SIGTERM stops the loop and the metrics endpoint."""
    import signal
    import threading
    import urllib.request

    from pdm_common import metrics

    handlers, servers = {}, []
    monkeypatch.setattr(sp.signal, "signal", lambda sig, fn: handlers.setdefault(sig, fn))
    real_serve = metrics.serve
    monkeypatch.setattr(sp.metrics, "serve", lambda reg, port, host, **kw: servers.append(
        real_serve(reg, 0, "127.0.0.1", **kw)) or servers[-1])
    seen = {}

    class Client:
        connected = False
        on_connect = on_message = on_disconnect = None

        def is_connected(self):
            return self.connected

        def subscribe(self, *args, **kwargs):
            pass

        def publish(self, *args, **kwargs):
            pass

        def loop_start(self):
            def life():
                self.connected = True
                self.on_connect(self, None, None, SimpleNamespace(is_failure=False), None)
                url = f"http://127.0.0.1:{servers[0].port}"
                seen["health"] = urllib.request.urlopen(url + "/health").status
                self.on_message(self, None, SimpleNamespace(topic="spBv1.0/g/NDATA/n", payload=b"\x00junk"))
                seen["metrics"] = urllib.request.urlopen(url + "/metrics").read().decode()
                handlers[signal.SIGTERM]()
            threading.Thread(target=life).start()

        def loop_stop(self):
            pass

        def disconnect(self):
            self.connected = False

    monkeypatch.setattr(sp, "mqtt_client", lambda *a, **k: Client())
    monkeypatch.setattr(sp.broker, "connect", lambda *a, **k: None)
    sp.run(sp.SparkplugAdapter(profile))
    assert seen["health"] == 200
    assert 'pdm_messages_in_total{topic="spBv1.0/NDATA"}' in seen["metrics"]
    with pytest.raises(OSError):
        urllib.request.urlopen(f"http://127.0.0.1:{servers[0].port}/health", timeout=1)
