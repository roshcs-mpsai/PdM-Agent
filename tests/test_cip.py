"""Read-only CIP client (T23, G8 stretch; INT-07, INT-08).

The allowlist is tested on every service code and on Multiple_Service_Packets,
with a fake driver that records what would have been sent: a refused request
never reaches it. The decoders are tested on built replies, and eight bench
scenarios run the poller against a fake drive; every event it publishes is
checked against device_event_v1. No PLC or drive is involved.
"""
import datetime as dt
import json
import struct
import sys
from pathlib import Path
from typing import NamedTuple, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))

from pdm_common import schemas  # noqa: E402
from pdm_common.store import Store  # noqa: E402
from south import cip  # noqa: E402

EXAMPLE = ROOT / "services" / "south" / "devices.example.yaml"
T0 = dt.datetime(2026, 11, 2, 9, 0, tzinfo=dt.timezone.utc)
ASSET, TOPIC = "metropt3_apu", "pdm/metropt3_apu/device"


class Tag(NamedTuple):                      # pycomm3's reply type: Tag(tag, value, type, error)
    tag: str
    value: object
    type: object = None
    error: Optional[str] = None


class RecordingDriver:
    """Records every generic_message; answers with ``reply`` (bytes, a Tag or an exception)."""

    def __init__(self, reply=b"\x00\x00"):
        self.calls = []
        self.reply = reply

    def generic_message(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply if isinstance(self.reply, Tag) else Tag(kwargs.get("name"), self.reply)


# --- the allowlist (INT-08) ---------------------------------------------------------------------

WRITES = [(0x02, "Set_Attributes_All"), (0x04, "Set_Attribute_List"), (0x05, "Reset"), (0x06, "Start"),
          (0x07, "Stop"), (0x08, "Create"), (0x09, "Delete"), (0x0D, "Apply_Attributes"),
          (0x10, "Set_Attribute_Single"), (0x16, "Save"), (0x15, "Restore"), (0x19, "Set_Member"),
          (0x4D, "object-specific service 0x4D"), (0x4E, "object-specific service 0x4E"),
          (0x53, "object-specific service 0x53"), (0x54, "object-specific service 0x54")]


@pytest.mark.parametrize("code", [0x01, 0x03, 0x0E])
def test_the_three_get_services_pass(code):
    assert cip.check_service(code) == code
    assert cip.check_service(bytes([code])) == code              # pycomm3.Services form


def test_every_other_service_code_is_refused():
    allowed = {0x01, 0x03, 0x0E}
    for code in range(256):
        if code in allowed:
            continue
        with pytest.raises(cip.WriteRefused):
            cip.check_service(code)                             # 0x0A without a packet is refused too


@pytest.mark.parametrize("code, name", WRITES)
def test_a_write_service_is_refused_before_anything_is_sent(code, name):
    driver = RecordingDriver()
    link = cip.ReadOnlyCIP(driver)
    with pytest.raises(cip.WriteRefused, match=name):
        link.get(0x0F, 41, 1, service=code)
    with pytest.raises(cip.WriteRefused, match=name):
        link.get(0x0F, 41, 1, service=bytes([code]), request_data=b"\x0a\x00")
    assert driver.calls == [] and link.sent == 0 and link.refused == 2


@pytest.mark.parametrize("service", [True, -1, 256, "0x0E", b"\x0e\x00", b"", 14.0, [0x0E]])
def test_anything_that_is_not_a_service_code_is_refused(service):
    driver = RecordingDriver()
    with pytest.raises(cip.WriteRefused):
        cip.ReadOnlyCIP(driver).get(1, 1, 5, service=service)
    assert driver.calls == []


def gets(n=2):
    return [cip.message(cip.GET_ATTRIBUTE_SINGLE, 0x0F, 7 + i, 1) for i in range(n)]


def test_a_multiple_service_packet_of_reads_is_sent_once():
    data = cip.multiple_service_packet(gets(3) + [cip.message(cip.GET_ATTRIBUTES_ALL, 0x01, 1)])
    assert [s for s, _ in cip.embedded_requests(data)] == [0x0E, 0x0E, 0x0E, 0x01]
    driver = RecordingDriver()
    cip.ReadOnlyCIP(driver).get(0x02, 1, service=cip.MULTIPLE_SERVICE_PACKET, request_data=data)
    assert len(driver.calls) == 1 and driver.calls[0]["service"] == b"\x0a" and driver.calls[0]["request_data"] == data


@pytest.mark.parametrize("inner", [cip.SET_ATTRIBUTE_SINGLE, cip.SET_ATTRIBUTES_ALL, cip.RESET, cip.SAVE, 0x4D])
def test_a_write_inside_a_multiple_service_packet_is_refused(inner):
    packet = cip.multiple_service_packet(gets() + [cip.message(inner, 0x0F, 41, 1, b"\x10\x00")])
    driver = RecordingDriver()
    with pytest.raises(cip.WriteRefused, match="request 3"):
        cip.ReadOnlyCIP(driver).get(0x02, 1, service=0x0A, request_data=packet)
    assert driver.calls == []


def test_a_nested_multiple_service_packet_with_a_write_inside_is_refused():
    write = cip.message(cip.SET_ATTRIBUTE_SINGLE, 0x0F, 41, 1, b"\x10\x00")
    inner = cip.message(cip.MULTIPLE_SERVICE_PACKET, 0x02, 1, data=cip.multiple_service_packet(gets() + [write]))
    outer = cip.multiple_service_packet(gets(1) + [inner])
    driver = RecordingDriver()
    with pytest.raises(cip.WriteRefused, match="request 2: Multiple_Service_Packet request 3"):
        cip.ReadOnlyCIP(driver).get(0x02, 1, service=0x0A, request_data=outer)
    assert driver.calls == []
    # the same nesting with reads only is allowed; nesting past the limit is not
    nested = cip.message(cip.MULTIPLE_SERVICE_PACKET, 0x02, 1, data=cip.multiple_service_packet(gets()))
    reads = cip.multiple_service_packet(gets(1) + [nested])
    assert cip.check_service(0x0A, reads) == 0x0A
    deep = cip.multiple_service_packet(gets(1))
    for _ in range(cip.MAX_NESTING):
        deep = cip.multiple_service_packet([cip.message(0x0A, 0x02, 1, data=deep)])
    with pytest.raises(cip.WriteRefused, match="too deep"):
        cip.check_service(0x0A, deep)


@pytest.mark.parametrize("data", [b"", b"\x01", b"\x00\x00", b"\x02\x00\x06\x00",       # no requests, short header
                                  b"\x01\x00\x02\x00\x0e\x01",                          # offset into the header
                                  b"\x01\x00\x04\x00\x0e\x05\x20\x01",                  # path longer than the request
                                  b"\x02\x00\x06\x00\x06\x00\x0e\x00",                  # empty second request
                                  b"\x01\x00\x09\x00\x0e\x00"])                         # offset past the end
def test_a_malformed_multiple_service_packet_is_refused(data):
    driver = RecordingDriver()
    with pytest.raises(cip.WriteRefused):
        cip.ReadOnlyCIP(driver).get(0x02, 1, service=0x0A, request_data=data)
    assert driver.calls == []


def test_get_is_the_only_way_to_the_driver():
    link = cip.ReadOnlyCIP(RecordingDriver())
    assert [n for n in dir(link) if not n.startswith("_") and callable(getattr(link, n))] == ["get"]
    assert not hasattr(link, "driver") and not hasattr(link, "generic_message")


def test_get_sends_unconnected_reads():
    driver = RecordingDriver(b"\x01\x00")
    link = cip.ReadOnlyCIP(driver)
    assert link.get(0x01, 1) == b"\x01\x00"
    assert link.get(0x0F, 7, 1) == b"\x01\x00"
    first, second = driver.calls
    assert first["service"] == b"\x01" and first["attribute"] == b"" and first["connected"] is False
    assert second["service"] == b"\x0e" and second["attribute"] == 1 and second["unconnected_send"] is False
    routed = RecordingDriver(b"\x00\x00")
    cip.ReadOnlyCIP(routed, routed=True).get(0x0F, 7, 1)
    assert routed.calls[0]["unconnected_send"] is True and routed.calls[0]["route_path"] is True
    assert link.sent == 2


class FakeCommError(Exception):                 # stands in for pycomm3.exceptions.CommError
    pass


def test_no_reply_is_comms_lost_and_an_error_reply_is_a_cip_error():
    with pytest.raises(cip.CommsLost, match="TimeoutError"):
        cip.ReadOnlyCIP(RecordingDriver(TimeoutError("timed out"))).get(1, 1)
    with pytest.raises(cip.CommsLost, match="FakeCommError"):
        cip.ReadOnlyCIP(RecordingDriver(FakeCommError("socket closed")), comms_errors=(FakeCommError,)).get(1, 1)
    with pytest.raises(cip.CIPError, match="Object does not exist"):
        cip.ReadOnlyCIP(RecordingDriver(Tag("x", None, None, "Object does not exist"))).get(4, 2, 3)
    with pytest.raises(cip.CIPError, match="no data"):
        cip.ReadOnlyCIP(RecordingDriver(Tag("x", None))).get(4, 2, 3)
    with pytest.raises(ValueError):             # anything else is a bug, not a comms loss
        cip.ReadOnlyCIP(RecordingDriver(ValueError("bad argument"))).get(1, 1)


# --- decoders -----------------------------------------------------------------------------------

def identity_reply(status=0x0065, serial=0x40A1B2C3, name="PowerFlex 525", revision=(5, 1), extra=b""):
    return struct.pack("<HHHBBHIB", 1, 0x02, 1234, revision[0], revision[1], status, serial, len(name)) \
        + name.encode() + extra


def test_identity_attributes():
    ident = cip.decode_identity(identity_reply(extra=b"\x03\x00\x00"))      # later attributes ignored
    assert (ident.vendor_id, ident.device_type, ident.product_code) == (1, 2, 1234)
    assert ident.revision == (5, 1) and ident.status == 0x0065 and ident.serial == 0x40A1B2C3
    assert ident.product_name == "PowerFlex 525" and ident.raw == "1:2:1234:5.001:40A1B2C3"
    text = cip.describe_identity(ident)
    assert "Rockwell Automation/Allen-Bradley" in text and "AC drive" in text and "revision 5.001" in text
    with pytest.raises(cip.DecodeError):
        cip.decode_identity(identity_reply()[:14])
    with pytest.raises(cip.DecodeError):
        cip.decode_identity(identity_reply()[:20])                          # name cut short


@pytest.mark.parametrize("word, text, severity, ne107", [
    (0x0000, "no fault; not owned, out-of-box configuration; extended status 0", "info", "good"),
    (0x0065, "no fault; owned, configured; extended status 6 (at least one I/O connection in run mode)",
     "info", "good"),
    (0x0165, "minor recoverable fault", "warning", "out_of_specification"),
    (0x0265, "minor unrecoverable fault", "warning", "maintenance_required"),
    (0x0455, "major recoverable fault", "fault", "failure"),
    (0x0855, "major unrecoverable fault", "fault", "failure"),
    (0x0315, "minor recoverable fault, minor unrecoverable fault", "warning", "function_check"),
    (0x0015, "extended status 1 (firmware update in progress)", "warning", "function_check"),
    (0x0025, "extended status 2 (at least one faulted I/O connection)", "warning", "function_check"),
    (0x0045, "extended status 4 (non-volatile configuration bad)", "warning", "maintenance_required"),
    (0x00A5, "extended status 10 (vendor specific)", "info", "good"),
    (0x0085, "extended status 8 (reserved)", "info", "good"),
])
def test_identity_status_bits(word, text, severity, ne107):
    d = cip.identity_status(word)
    assert text in d.text and (d.severity, d.ne107) == (severity, ne107)


RUNNING = cip.READY | cip.ACTIVE | cip.COMMAND_FORWARD | cip.ROTATING_FORWARD | cip.AT_SPEED     # 0x010F
STOPPED = cip.READY | cip.COMMAND_FORWARD | cip.ROTATING_FORWARD                                # 0x000D
FAULTED = cip.FAULT | cip.COMMAND_FORWARD | cip.ROTATING_FORWARD                                # 0x008C
NOT_READY = cip.COMMAND_FORWARD | cip.ROTATING_FORWARD                                          # 0x000C


@pytest.mark.parametrize("word, text, severity, ne107", [
    (RUNNING, "ready, running, forward, at speed", "info", "good"),
    (STOPPED, "ready, stopped", "info", "good"),
    (RUNNING | cip.ALARM, "ready, running, forward, at speed, alarm", "warning", "maintenance_required"),
    (FAULTED, "not ready, stopped, fault", "fault", "failure"),
    (FAULTED | cip.ALARM, "not ready, stopped, alarm, fault", "fault", "failure"),
    (NOT_READY, "not ready, stopped", "warning", "function_check"),
    (NOT_READY | cip.ALARM, "not ready, stopped, alarm", "warning", "function_check"),
    (cip.READY | cip.ACTIVE | cip.COMMAND_FORWARD | cip.DECELERATING,
     "running, reverse, commanded forward, decelerating", "info", "good"),
    (RUNNING | 0x0200 | 0x0400 | 0x0800 | 0x1000 | 0x2000,
     "frequency from comms, start/stop from comms, parameters locked, digital input 1 on, digital input 2 on",
     "info", "good"),
    (RUNNING | 0x8000, "bit 15 set (unused in this bit map: check the drive)", "info", "good"),
])
def test_logic_status_bits(word, text, severity, ne107):
    d = cip.logic_status(word)
    assert text in d.text and (d.severity, d.ne107) == (severity, ne107)


@pytest.mark.parametrize("code, text", [
    (0, "no fault"), (2, "F002 Auxiliary In"), (3, "F003 Power Loss"), (4, "F004 UnderVoltage"),
    (5, "F005 OverVoltage"), (6, "F006 Motor Stalled"), (7, "F007 Motor Overload"), (8, "F008 Heatsink OvrTmp"),
    (12, "F012 HW OverCurrent"), (13, "F013 Ground Fault"), (33, "F033 Auto Rstrt Tries"),
    (38, "F038 Phase U to Gnd"), (39, "F039 Phase V to Gnd"), (40, "F040 Phase W to Gnd"),
    (63, "F063 SW OverCurrent"), (64, "F064 Drive Overload"), (81, "F081 Comm Loss"),
    (100, "F100 Parameter Checksum"), (59, "unknown fault 59"), (999, "unknown fault 999"),
])
def test_fault_codes(code, text):
    assert cip.fault_text(code) == text


def test_the_fault_queue_reads_against_the_fault_bit():
    assert cip.fault_queue((4, 13, 0), True, None) == ("active fault F004 UnderVoltage; earlier F013 Ground Fault",
                                                       "fault", "failure")
    assert cip.fault_queue((0, 0, 0), True, None) == ("drive faulted, fault queue empty", "fault", "failure")
    assert cip.fault_queue((4, 13, 0), False, None) == ("no active fault; logged F004 UnderVoltage, F013 Ground Fault",
                                                        "info", "good")
    assert cip.fault_queue((0, 0, 0), False, (0, 0, 0)) == ("no active fault; no faults logged", "info", "good")
    assert cip.fault_queue((4, 0, 0), False, (0, 0, 0)) == (
        "new fault F004 UnderVoltage and already cleared; logged F004 UnderVoltage", "warning", "good")
    assert cip.fault_queue((4, 0, 0), None, (0, 0, 0)) == ("new fault F004 UnderVoltage; logged F004 UnderVoltage",
                                                           "warning", None)
    assert cip.fault_queue((4, 0, 0), None, None).text.startswith("active fault unknown")


# --- the bench scenarios -------------------------------------------------------------------------

@pytest.fixture(scope="module")
def device():
    devices = cip.load_devices(EXAMPLE)
    assert [d.name for d in devices] == ["drive_1"]
    return devices[0]


class Bench:
    """A fake drive answering the example device file's reads from a state:
    {"identity": status word, "logic": Logic Status word, "faults": (codes)} or "timeout"."""

    def __init__(self, reads):
        self.reads = reads
        self.state = None
        self.serial = 0x40A1B2C3
        self.calls = []

    def generic_message(self, **kw):
        self.calls.append(kw)
        if self.state == "timeout":
            raise TimeoutError("no reply within 2.0 s")
        service, key = kw["service"][0], (kw["class_code"], kw["instance"])
        ident, logic, queue = self.reads["identity"], self.reads["logic_status"], self.reads["fault_queue"]
        if service == 0x01 and key == (ident.class_code, ident.instance):
            return Tag("identity", identity_reply(self.state["identity"], serial=self.serial))
        if service == 0x0E and key == (logic.class_code, logic.instance) and kw["attribute"] == logic.attribute:
            return Tag("logic", struct.pack("<HH", self.state["logic"], 6000))      # Logic Status, then feedback
        if service == 0x0E and kw["class_code"] == queue.class_code and kw["instance"] in queue.instances:
            return Tag("fault", struct.pack("<H", self.state["faults"][queue.instances.index(kw["instance"])]))
        return Tag("other", None, None, "Object does not exist")


def healthy(**changes):
    return dict({"identity": 0x0065, "logic": RUNNING, "faults": (0, 0, 0)}, **changes)


BASELINE = [("connection", "comms", "info", "good"), ("identity", "product", "info", "good"),
            ("identity", "status", "info", "good"), ("drive", "logic_status", "info", "good"),
            ("drive", "fault_queue", "info", "good")]

# name: [(drive state, [(object, attribute, severity, NE 107 class) of the events that poll]), ...]
SCENARIOS = {
    "S1 healthy": [(healthy(), BASELINE)],
    "S2 undervoltage fault": [
        (healthy(), BASELINE),
        (healthy(logic=FAULTED, faults=(4, 0, 0)), [("drive", "logic_status", "fault", "failure"),
                                                    ("drive", "fault_queue", "fault", "failure")])],
    "S3 ground fault": [
        (healthy(faults=(4, 0, 0)), BASELINE),
        (healthy(logic=FAULTED, faults=(13, 4, 0)), [("drive", "logic_status", "fault", "failure"),
                                                     ("drive", "fault_queue", "fault", "failure")])],
    "S4 motor overload alarm, then fault": [
        (healthy(), BASELINE),
        (healthy(logic=RUNNING | cip.ALARM), [("drive", "logic_status", "warning", "maintenance_required")]),
        (healthy(logic=FAULTED, faults=(7, 0, 0)), [("drive", "logic_status", "fault", "failure"),
                                                    ("drive", "fault_queue", "fault", "failure")])],
    "S5 comms loss": [
        (healthy(), BASELINE),
        ("timeout", [("connection", "comms", "warning", "function_check")]),
        ("timeout", []),
        (healthy(), BASELINE)],
    "S6 drive not ready": [
        (healthy(), BASELINE),
        (healthy(logic=NOT_READY), [("drive", "logic_status", "warning", "function_check")])],
    "S7 identity minor recoverable fault": [
        (healthy(), BASELINE),
        (healthy(identity=0x0165), [("identity", "status", "warning", "out_of_specification")])],
    "S8 fault clears back to healthy": [
        (healthy(logic=FAULTED, faults=(5, 0, 0)), [("connection", "comms", "info", "good"),
                                                    ("identity", "product", "info", "good"),
                                                    ("identity", "status", "info", "good"),
                                                    ("drive", "logic_status", "fault", "failure"),
                                                    ("drive", "fault_queue", "fault", "failure")]),
        (healthy(faults=(5, 0, 0)), [("drive", "logic_status", "info", "good"),      # the queue keeps F005
                                     ("drive", "fault_queue", "info", "good")])],
}

DECODED = {   # what an event of each scenario's last poll says
    "S2 undervoltage fault": "active fault F004 UnderVoltage",
    "S3 ground fault": "active fault F013 Ground Fault; earlier F004 UnderVoltage",
    "S4 motor overload alarm, then fault": "active fault F007 Motor Overload",
    "S5 comms loss": "comms restored",
    "S6 drive not ready": "not ready, stopped",
    "S7 identity minor recoverable fault": "minor recoverable fault",
    "S8 fault clears back to healthy": "no active fault; logged F005 OverVoltage",
}


def run(device, states, store=None, bench=None):
    bench = bench or Bench(device.reads)
    poller = cip.DevicePoller(ASSET, device.name, cip.ReadOnlyCIP(bench), TOPIC, device.reads, store)
    polls = []
    for i, state in enumerate(states):
        bench.state = state
        outs = poller.poll(T0 + dt.timedelta(seconds=5 * i))
        assert all(o.topic == TOPIC and not o.retain for o in outs)
        polls.append([json.loads(o.payload) for o in outs])
    return polls, poller, bench


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_bench_scenario(device, name):
    steps = SCENARIOS[name]
    polls, poller, bench = run(device, [state for state, _ in steps])
    for i, (events, (_, expected)) in enumerate(zip(polls, steps)):
        got = [(e["object"], e["attribute"], e["severity"], e["ne107_class"]) for e in events]
        assert got == expected, f"{name}, poll {i + 1}"
        for e in events:
            assert schemas.validate("device_event_v1", e) == [], e
            assert (e["asset"], e["device"]) == (ASSET, device.name)
            assert e["ts"] == (T0 + dt.timedelta(seconds=5 * i)).isoformat().replace("+00:00", "Z")
    if name in DECODED:
        assert any(DECODED[name] in e["decoded"] for e in polls[-1]), polls[-1]
    assert all(c["service"] in (b"\x01", b"\x0e") and c["connected"] is False for c in bench.calls)
    assert poller.cip.refused == 0


def test_comms_loss_is_one_event_and_then_a_new_baseline(device):
    polls, poller, _ = run(device, [healthy(), "timeout", "timeout", "timeout", healthy()])
    lost = polls[1][0]
    assert lost["decoded"].startswith("comms loss: TimeoutError") and lost["raw_value"].startswith("TimeoutError")
    assert polls[2] == polls[3] == []
    assert polls[4][0]["decoded"] == "comms restored" and len(polls[4]) == 5
    assert poller.counts["comms_losses"] == 1 and poller.report()["comms"] is True


def test_unreachable_from_the_start(device):
    polls, _, _ = run(device, ["timeout", healthy()])
    assert [(e["attribute"], e["ne107_class"]) for e in polls[0]] == [("comms", "function_check")]
    assert polls[1][0]["decoded"] == "comms restored"


def test_a_fault_logged_and_reset_between_polls_is_reported(device):
    polls, _, _ = run(device, [healthy(), healthy(faults=(4, 0, 0))])
    (event,) = polls[1]
    assert (event["attribute"], event["severity"], event["ne107_class"]) == ("fault_queue", "warning", "good")
    assert event["decoded"].startswith("new fault F004 UnderVoltage and already cleared")


def test_a_fault_logged_while_unreachable_is_reported_after(device):
    polls, _, _ = run(device, [healthy(), "timeout", healthy(faults=(13, 0, 0))])
    queue = next(e for e in polls[2] if e["attribute"] == "fault_queue")
    assert queue["decoded"].startswith("new fault F013 Ground Fault") and queue["severity"] == "warning"


def test_a_swapped_device_is_reported(device):
    bench = Bench(device.reads)
    poller = cip.DevicePoller(ASSET, device.name, cip.ReadOnlyCIP(bench), TOPIC, device.reads)
    bench.state = healthy()
    poller.poll(T0)
    bench.serial = 0x40A1FFFF
    (event,) = [json.loads(o.payload) for o in poller.poll(T0 + dt.timedelta(seconds=5))]
    assert event["attribute"] == "product" and event["severity"] == "warning"
    assert event["decoded"].startswith("identity changed (was 1:2:1234:5.001:40A1B2C3)")


def test_a_device_swapped_during_a_dropped_poll_is_still_reported(device):
    class DropsAfterIdentity(Bench):                     # while dropping: the identity answers, nothing else
        dropping = False

        def generic_message(self, **kw):
            if self.dropping and kw["class_code"] != self.reads["identity"].class_code:
                raise TimeoutError("no reply within 2.0 s")
            return super().generic_message(**kw)

    bench = DropsAfterIdentity(device.reads)
    bench.state = healthy()
    poller = cip.DevicePoller(ASSET, device.name, cip.ReadOnlyCIP(bench), TOPIC, device.reads)
    poller.poll(T0)
    bench.serial, bench.dropping = 0x40A1FFFF, True      # swapped, and the link drops mid-poll
    (lost,) = [json.loads(o.payload) for o in poller.poll(T0 + dt.timedelta(seconds=5))]
    assert lost["attribute"] == "comms"
    bench.dropping = False
    events = {e["attribute"]: e for e in (json.loads(o.payload) for o in poller.poll(T0 + dt.timedelta(seconds=10)))}
    assert events["product"]["severity"] == "warning"
    assert events["product"]["decoded"].startswith("identity changed (was 1:2:1234:5.001:40A1B2C3)")


def test_only_watched_logic_bits_make_an_event(device):
    polls, _, _ = run(device, [healthy(logic=STOPPED), healthy(logic=STOPPED | cip.DECELERATING),
                               healthy(logic=STOPPED | 0x1000), healthy(logic=RUNNING)])
    assert polls[1] == polls[2] == []                       # deceleration and a digital input alone
    assert [(e["attribute"], e["raw_value"]) for e in polls[3]] == [("logic_status", RUNNING)]


def test_a_read_the_drive_does_not_support_is_reported_not_fatal(device):
    class NoAssembly(Bench):
        def generic_message(self, **kw):
            if kw["class_code"] == self.reads["logic_status"].class_code:
                self.calls.append(kw)
                return Tag("logic", None, None, "Object does not exist")
            return super().generic_message(**kw)

    polls, _, _ = run(device, [healthy(logic=FAULTED, faults=(4, 0, 0))], bench=NoAssembly(device.reads))
    by_attribute = {e["attribute"]: e for e in polls[0]}
    assert by_attribute["logic_status"]["decoded"] == "read failed: Object does not exist"
    assert (by_attribute["logic_status"]["severity"], by_attribute["logic_status"]["ne107_class"]) == \
        ("warning", "function_check")
    assert "ne107_class" not in by_attribute["fault_queue"]          # active or not cannot be told
    assert by_attribute["fault_queue"]["decoded"].startswith("active fault unknown")
    for e in polls[0]:
        assert schemas.validate("device_event_v1", e) == []


def test_events_go_to_the_store(device):
    store = Store("sqlite://")
    store.init_schema()
    run(device, [healthy(), healthy(logic=FAULTED, faults=(4, 0, 0))], store=store)
    rows = store.select("device_events", order="ts, attribute")
    assert len(rows) == 7
    last = [r for r in rows if r["attribute"] == "fault_queue"][-1]
    assert (last["raw_value"], last["severity"], last["ne107_class"]) == ("4,0,0", "fault", "failure")
    assert last["ts"] == "2026-11-02T09:00:05Z" and last["device"] == "drive_1"


def test_the_identity_only_default(device):
    driver = RecordingDriver(identity_reply())
    poller = cip.DevicePoller(ASSET, "plc_1", cip.ReadOnlyCIP(driver), TOPIC)
    events = [json.loads(o.payload) for o in poller.poll(T0)]
    assert [e["attribute"] for e in events] == ["comms", "product", "status"]
    with pytest.raises(ValueError, match="unknown read"):
        cip.DevicePoller(ASSET, "x", cip.ReadOnlyCIP(driver), TOPIC, {"set_speed": None})


def test_the_schedule_polls_each_device_on_its_interval(device):
    clock = [0.0]
    bench = Bench(device.reads)
    bench.state = healthy()
    fast = cip.DevicePoller(ASSET, "fast", cip.ReadOnlyCIP(bench), TOPIC, device.reads)
    slow = cip.DevicePoller(ASSET, "slow", cip.ReadOnlyCIP(bench), TOPIC, device.reads)
    schedule = cip.Schedule([(fast, 1.0), (slow, 5.0)], clock=lambda: clock[0], now=lambda: T0)
    for step in range(11):
        clock[0] = step * 0.5
        schedule.tick()
    assert fast.counts["polls"] == 6 and slow.counts["polls"] == 2


# --- the device file ------------------------------------------------------------------------------

def test_the_example_device_file(device):
    assert (device.path, device.routed) == ("192.168.1.20", False)
    assert (device.poll_seconds, device.timeout_seconds) == (5.0, 2.0)
    assert device.reads["identity"] == cip.ObjectRead(0x01, 1)
    assert device.reads["logic_status"] == cip.ObjectRead(0x04, 2, 3, 0)
    assert device.reads["fault_queue"] == cip.QueueRead(0x0F, (7, 8, 9), 1)


@pytest.mark.parametrize("text, problem", [
    ("devices: []", "non-empty list"),
    ("devices:\n  - {path: 10.0.0.1}", "name is required"),
    ("devices:\n  - {name: a}", "path is required"),
    ("devices:\n  - {name: a, path: 10.0.0.1}\n  - {name: a, path: 10.0.0.2}", "appears twice"),
    ("devices:\n  - {name: a, path: 10.0.0.1, poll_seconds: 0}", "poll_seconds"),
    ("devices:\n  - {name: a, path: 10.0.0.1, routed: yes please}", "routed"),
    ("devices:\n  - {name: a, path: 10.0.0.1, write: true}", "unknown key"),
    ("devices:\n  - {name: a, path: 10.0.0.1, read: {identity: {class: 1, instance: 1, service: 0x10}}}",
     "unknown key\\(s\\) service"),
    ("devices:\n  - {name: a, path: 10.0.0.1, read: {speed_reference: {class: 1, instance: 1}}}", "unknown read"),
    ("devices:\n  - {name: a, path: 10.0.0.1, read: {fault_queue: {class: 15, instances: [], attribute: 1}}}",
     "instances"),
    ("devices:\n  - {name: a, path: 10.0.0.1, read: {logic_status: {class: 4, instance: 2}}}", "attribute"),
    ("devices:\n  - {name: a, path: [10.0.0.1}", "not valid YAML"),
])
def test_a_bad_device_file_names_the_problem(tmp_path, text, problem):
    path = tmp_path / "devices.yaml"
    path.write_text(text)
    with pytest.raises(cip.ConfigError, match=problem):
        cip.load_devices(path)


def test_a_device_without_reads_gets_the_identity(tmp_path):
    path = tmp_path / "devices.yaml"
    path.write_text("devices:\n  - {name: plc_1, path: 10.0.0.5}\n")
    (d,) = cip.load_devices(path)
    assert d.reads == {"identity": cip.ObjectRead(0x01, 1)}


def test_the_link_reconnects_after_a_failure():
    opened = []

    class Driver:
        def __init__(self, path):
            self.path, self.socket_timeout, self.closed = path, 5.0, False
            opened.append(self)

        def open(self):
            return True

        def close(self):
            self.closed = True

        def generic_message(self, **kw):
            if len(opened) == 1:
                raise FakeCommError("connection reset")
            return Tag("x", b"\x01\x00")

    link = cip.Pycomm3Link(Driver, "192.168.1.20", 2.0)
    reader = cip.ReadOnlyCIP(link, comms_errors=(FakeCommError,))
    with pytest.raises(cip.CommsLost):
        reader.get(0x0F, 7, 1)
    assert opened[0].closed and opened[0].socket_timeout == 2.0
    assert reader.get(0x0F, 7, 1) == b"\x01\x00" and len(opened) == 2
    with pytest.raises(cip.WriteRefused):
        reader.get(0x0F, 41, 1, service=cip.SET_ATTRIBUTE_SINGLE)
    assert len(opened) == 2
    link.close()
    assert opened[1].closed


def test_a_session_that_does_not_register_is_comms_lost():
    class Refusing:
        def __init__(self, path):
            self.closed = False

        def open(self):
            return False                           # pycomm3: "Session not registered"

        def close(self):
            self.closed = True

    link = cip.Pycomm3Link(Refusing, "192.168.1.20", 2.0)
    with pytest.raises(cip.CommsLost, match="no EtherNet/IP session"):
        cip.ReadOnlyCIP(link).get(0x01, 1)
