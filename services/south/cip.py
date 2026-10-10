"""Read-only CIP client -- drive and controller status for device_health (T23, G8 stretch; INT-07, INT-08).

Polls each configured device with CIP Get services only: the Identity object
(vendor, product, revision, serial number and the status word with its fault
bits), a drive's Logic Status word and its fault queue. It decodes them and
publishes a device_event_v1 on the profile's ``topics.device`` for every
attribute that changed since the last poll, with a severity and an NE 107
class, for the agent's device_health tool; with a store, each event is also a
row of ``device_events``.

Read-only by construction (INT-08). ``ReadOnlyCIP`` is the only object that
holds the driver, and its only method is ``get``. ``get`` checks the service
code against an allowlist -- Get_Attributes_All (0x01), Get_Attribute_List
(0x03), Get_Attribute_Single (0x0E), and Multiple_Service_Packet (0x0A) only
when every request inside it is allowed -- and raises WriteRefused before the
driver is called for anything else. Messages are unconnected, so no Forward
Open is sent. The poller picks the services itself; the device file only says
which objects to read.

pycomm3 is imported only in main(). The wrapper, the decoders and the poller
work with any object that has pycomm3's ``generic_message``, which is how
tests/test_cip.py drives them with a fake driver through eight bench
scenarios. CIP.md has the decoding tables and what was not verified.

    python services/south/cip.py --profile profiles/metropt3_apu/asset_profile.yaml \\
        --devices services/south/devices.example.yaml [--once]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import struct
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, NamedTuple, Optional, Union

from pdm_common.hashing import canonical_json
from pdm_common.mqtt import Out, run_service
from pdm_common.profile import ProfileError, load_profile, read_yaml
from pdm_common.status import NE107_STATES
from pdm_common.timeutil import UTC, iso_utc

log = logging.getLogger("south.cip")

EVENT_SCHEMA = "device_event_v1"

# --- service codes and the allowlist (INT-08) -------------------------------------------------

# CIP common services (CIP Vol. 1, Appendix A).
GET_ATTRIBUTES_ALL = 0x01
SET_ATTRIBUTES_ALL = 0x02
GET_ATTRIBUTE_LIST = 0x03
SET_ATTRIBUTE_LIST = 0x04
RESET = 0x05
START = 0x06
STOP = 0x07
CREATE = 0x08
DELETE = 0x09
MULTIPLE_SERVICE_PACKET = 0x0A
APPLY_ATTRIBUTES = 0x0D
GET_ATTRIBUTE_SINGLE = 0x0E
SET_ATTRIBUTE_SINGLE = 0x10
SAVE = 0x16

SERVICE_NAMES = {
    0x01: "Get_Attributes_All", 0x02: "Set_Attributes_All", 0x03: "Get_Attribute_List",
    0x04: "Set_Attribute_List", 0x05: "Reset", 0x06: "Start", 0x07: "Stop", 0x08: "Create", 0x09: "Delete",
    0x0A: "Multiple_Service_Packet", 0x0D: "Apply_Attributes", 0x0E: "Get_Attribute_Single",
    0x10: "Set_Attribute_Single", 0x11: "Find_Next_Object_Instance", 0x15: "Restore", 0x16: "Save",
    0x17: "No_Operation", 0x18: "Get_Member", 0x19: "Set_Member", 0x1A: "Insert_Member", 0x1B: "Remove_Member",
    0x1C: "Group_Sync",
}

# The only services that reach a device. Everything else -- the Set services, Reset, Start,
# Stop, Create, Delete, Apply_Attributes, Save, and every object-specific or vendor service
# (0x32-0x7F, Logix Read/Write Tag among them) -- is refused.
READ_SERVICES = frozenset({GET_ATTRIBUTES_ALL, GET_ATTRIBUTE_LIST, GET_ATTRIBUTE_SINGLE})
MAX_NESTING = 4                     # Multiple_Service_Packets inside Multiple_Service_Packets


class WriteRefused(PermissionError):
    """A CIP service that is not a read; refused before anything was sent (INT-08)."""


class CommsLost(ConnectionError):
    """No reply from the device: a timeout or a broken connection."""


class CIPError(RuntimeError):
    """The device replied with an error status, for example an object it does not have."""


class DecodeError(ValueError):
    """A reply too short or malformed to decode."""


def service_name(code: int) -> str:
    if code in SERVICE_NAMES:
        return SERVICE_NAMES[code]
    if 0x32 <= code <= 0x7F:
        return f"object-specific service 0x{code:02X}"
    if code >= 0x80:
        return f"reply code 0x{code:02X}"
    return f"reserved service 0x{code:02X}"


def service_code(service) -> int:
    """A service as pycomm3 takes it -- an int, or one byte like pycomm3.Services -- as an int."""
    if isinstance(service, (bytes, bytearray)) and len(service) == 1:
        return service[0]
    if isinstance(service, int) and not isinstance(service, bool) and 0 <= service <= 0xFF:
        return service
    raise WriteRefused(f"{service!r} is not a CIP service code; nothing was sent")


def embedded_requests(request_data) -> list[tuple[int, bytes]]:
    """(service, request data) of each request inside a Multiple_Service_Packet.

    The packet's data (CIP Vol. 1, A-4.10): a UINT count, that many UINT offsets
    from the start of the data, then the requests, each a service code, a path
    size in words, the path and the request's own data. A packet that does not
    parse is refused; this module never builds one.
    """
    data = bytes(request_data)
    if len(data) < 2:
        raise WriteRefused("Multiple_Service_Packet without a request count")
    count = int.from_bytes(data[:2], "little")
    header = 2 + 2 * count
    if count == 0 or len(data) < header:
        raise WriteRefused(f"Multiple_Service_Packet with {count} request(s) in {len(data)} bytes")
    bounds = [int.from_bytes(data[2 + 2 * i:4 + 2 * i], "little") for i in range(count)] + [len(data)]
    requests = []
    for i in range(count):
        start, end = bounds[i], bounds[i + 1]
        if not header <= start < end <= len(data) or end - start < 2:
            raise WriteRefused(f"Multiple_Service_Packet request {i + 1} has a bad offset")
        req = data[start:end]
        body = 2 + 2 * req[1]
        if body > len(req):
            raise WriteRefused(f"Multiple_Service_Packet request {i + 1} has a bad path size")
        requests.append((req[0], req[body:]))
    return requests


def check_service(service, request_data=b"", depth: int = 0) -> int:
    """The service code if it is a read; WriteRefused otherwise. Nothing is sent here."""
    code = service_code(service)
    if code in READ_SERVICES:
        return code
    if code == MULTIPLE_SERVICE_PACKET:
        if depth >= MAX_NESTING:
            raise WriteRefused("Multiple_Service_Packet nested too deep; nothing was sent")
        for position, (inner, data) in enumerate(embedded_requests(request_data), 1):
            try:
                check_service(inner, data, depth + 1)
            except WriteRefused as exc:
                raise WriteRefused(f"Multiple_Service_Packet request {position}: {exc}") from None
        return code
    raise WriteRefused(f"{service_name(code)} (0x{code:02X}) is not a read service; nothing was sent")


def _logical(segment: int, value: int) -> bytes:
    if 0 <= value < 0x100:
        return bytes([segment, value])
    if value < 0x10000:
        return bytes([segment | 1, 0]) + value.to_bytes(2, "little")
    raise ValueError(f"{value} does not fit a 16-bit logical segment")


def message(service: int, class_code: int, instance: int, attribute: Optional[int] = None,
            data: bytes = b"") -> bytes:
    """One Message Router request (service, path size, logical path, data), for building
    Multiple_Service_Packets."""
    path = _logical(0x20, class_code) + _logical(0x24, instance)
    if attribute is not None:
        path += _logical(0x30, attribute)
    return bytes([service, len(path) // 2]) + path + bytes(data)


def multiple_service_packet(requests: list[bytes]) -> bytes:
    """The request data of a Multiple_Service_Packet carrying these requests."""
    offsets, at = [], 2 + 2 * len(requests)
    for r in requests:
        offsets.append(at)
        at += len(r)
    return (len(requests).to_bytes(2, "little") + b"".join(o.to_bytes(2, "little") for o in offsets)
            + b"".join(requests))


class ReadOnlyCIP:
    """The only way this module reaches a device: CIP Get services, checked before sending.

    ``driver`` is a pycomm3 CIPDriver or anything with its ``generic_message``; it is
    private and nothing else here calls it. ``comms_errors`` are what the driver raises
    when no reply comes (main() adds pycomm3's CommError). ``routed``: the path goes
    through a controller, so pycomm3 wraps each request in an Unconnected Send.
    """

    def __init__(self, driver, *, comms_errors=(OSError, TimeoutError), routed: bool = False):
        self._driver = driver
        self._comms_errors = tuple(comms_errors)
        self._routed = routed
        self.sent = 0
        self.refused = 0

    def get(self, class_code: int, instance: int, attribute: Optional[int] = None, service=None,
            request_data: bytes = b"", name: str = "cip_get") -> bytes:
        """The reply data of one Get request: Get_Attribute_Single with an attribute,
        Get_Attributes_All without, unless ``service`` names another read."""
        if service is None:
            service = GET_ATTRIBUTES_ALL if attribute is None else GET_ATTRIBUTE_SINGLE
        try:
            code = check_service(service, request_data)
        except WriteRefused as exc:
            self.refused += 1
            log.warning("refused %s to class 0x%02X instance %s: %s", name, class_code, instance, exc)
            raise
        self.sent += 1
        try:
            reply = self._driver.generic_message(
                service=bytes([code]), class_code=class_code, instance=instance,
                attribute=b"" if attribute is None else attribute, request_data=bytes(request_data),
                data_type=None, name=name, connected=False, unconnected_send=self._routed, route_path=self._routed)
        except self._comms_errors as exc:
            raise CommsLost(f"{type(exc).__name__}: {exc}") from exc
        error = getattr(reply, "error", None)
        if error:
            raise CIPError(str(error))
        value = getattr(reply, "value", None)
        if not isinstance(value, (bytes, bytearray)):
            raise CIPError(f"no data in the reply ({value!r})")
        return bytes(value)


# --- decoding ---------------------------------------------------------------------------------

class Decoded(NamedTuple):
    text: str
    severity: str                   # info, warning or fault: how notable the event is
    ne107: Optional[str]            # the device state it implies; None when it cannot be told


SEVERITIES = ("info", "warning", "fault")


def worst(conditions) -> tuple[str, str]:
    """(severity, NE 107 class) of the most severe of (severity, class) conditions; info, good if none."""
    if not conditions:
        return "info", "good"
    return (max((c[0] for c in conditions), key=SEVERITIES.index),
            min((c[1] for c in conditions), key=NE107_STATES.index))


def word_at(data: bytes, offset: int = 0) -> int:
    """The little-endian 16-bit word at ``offset``."""
    if len(data) < offset + 2:
        raise DecodeError(f"a 16-bit word at byte {offset} needs {offset + 2} bytes, the reply has {len(data)}")
    return int.from_bytes(data[offset:offset + 2], "little")


VENDORS = {1: "Rockwell Automation/Allen-Bradley"}
DEVICE_TYPES = {0x00: "generic device", 0x02: "AC drive", 0x0C: "communications adapter",
                0x0E: "programmable logic controller", 0x13: "DC drive"}


@dataclass(frozen=True)
class Identity:
    vendor_id: int
    device_type: int
    product_code: int
    revision: tuple
    status: int
    serial: int
    product_name: str

    @property
    def raw(self) -> str:
        return (f"{self.vendor_id}:{self.device_type}:{self.product_code}:"
                f"{self.revision[0]}.{self.revision[1]:03d}:{self.serial:08X}")


def decode_identity(data: bytes) -> Identity:
    """The Identity object's Get_Attributes_All reply (class 0x01): attributes 1-7 in order --
    vendor id, device type and product code (UINT), revision (USINT major, USINT minor),
    status (WORD), serial number (UDINT), product name (SHORT_STRING). Later attributes are
    ignored."""
    if len(data) < 15:
        raise DecodeError(f"an Identity reply needs at least 15 bytes, got {len(data)}")
    vendor, kind, code, major, minor, status, serial, n = struct.unpack_from("<HHHBBHIB", data)
    if len(data) < 15 + n:
        raise DecodeError(f"the product name is {n} bytes but only {len(data) - 15} follow")
    return Identity(vendor, kind, code, (major, minor), status, serial, data[15:15 + n].decode("ascii", "replace"))


def describe_identity(ident: Identity) -> str:
    vendor = VENDORS.get(ident.vendor_id, f"vendor {ident.vendor_id}")
    kind = DEVICE_TYPES.get(ident.device_type, f"device type 0x{ident.device_type:02X}")
    return (f"{ident.product_name or 'unnamed product'}: {vendor}, {kind}, product code {ident.product_code}, "
            f"revision {ident.revision[0]}.{ident.revision[1]:03d}, serial {ident.serial:08X}")


# Identity status word (attribute 5), CIP Vol. 1, Identity object: bits 4-7.
EXTENDED_STATUS = {
    0: "self-testing or unknown", 1: "firmware update in progress", 2: "at least one faulted I/O connection",
    3: "no I/O connections established", 4: "non-volatile configuration bad", 5: "major fault",
    6: "at least one I/O connection in run mode", 7: "at least one I/O connection established, all in idle mode",
}
IDENTITY_FAULTS = (                 # bit, meaning, (severity, NE 107 class)
    (8, "minor recoverable fault", ("warning", "out_of_specification")),
    (9, "minor unrecoverable fault", ("warning", "maintenance_required")),
    (10, "major recoverable fault", ("fault", "failure")),
    (11, "major unrecoverable fault", ("fault", "failure")),
)
EXTENDED_CONDITIONS = {1: ("warning", "function_check"), 2: ("warning", "function_check"),
                       4: ("warning", "maintenance_required")}


def identity_status(word: int) -> Decoded:
    """Bit 0 owned, bit 2 configured, bits 4-7 extended device status, bits 8-11 the
    minor/major recoverable/unrecoverable faults, bits 12-15 vendor specific."""
    faults = [(meaning, condition) for bit, meaning, condition in IDENTITY_FAULTS if word >> bit & 1]
    extended = word >> 4 & 0x0F
    meaning = EXTENDED_STATUS.get(extended, "reserved" if extended in (8, 9) else "vendor specific")
    conditions = [c for _, c in faults] + ([EXTENDED_CONDITIONS[extended]] if extended in EXTENDED_CONDITIONS else [])
    text = (f"{', '.join(m for m, _ in faults) or 'no fault'}; "
            f"{'owned' if word & 0x0001 else 'not owned'}, "
            f"{'configured' if word & 0x0004 else 'out-of-box configuration'}; "
            f"extended status {extended} ({meaning})")
    return Decoded(text, *worst(conditions))


# PowerFlex 525 Logic Status word, as we read the PowerFlex 520-series user manual
# (520-UM001). Check every bit against the manual for the drive on the bench.
READY, ACTIVE, COMMAND_FORWARD, ROTATING_FORWARD = 0x0001, 0x0002, 0x0004, 0x0008
ACCELERATING, DECELERATING, ALARM, FAULT, AT_SPEED = 0x0010, 0x0020, 0x0040, 0x0080, 0x0100
LOGIC_FLAGS = ((4, "accelerating"), (5, "decelerating"), (8, "at speed"), (6, "alarm"), (7, "fault"),
               (9, "frequency from comms"), (10, "start/stop from comms"), (11, "parameters locked"),
               (12, "digital input 1 on"), (13, "digital input 2 on"))
# Only these bits make an event: the others flip with every speed change or are configuration.
WATCHED = READY | ACTIVE | ALARM | FAULT


def logic_status(word: int) -> Decoded:
    """Fault: failure. Alarm: maintenance required. Not ready without a fault (stop or enable
    input, safe torque off, bus not charged): function check. Otherwise good."""
    parts = ["ready" if word & READY else "not ready", "running" if word & ACTIVE else "stopped"]
    if word & ACTIVE:
        parts.append("forward" if word & ROTATING_FORWARD else "reverse")
        if bool(word & COMMAND_FORWARD) != bool(word & ROTATING_FORWARD):
            parts.append("commanded " + ("forward" if word & COMMAND_FORWARD else "reverse"))
    parts += [label for bit, label in LOGIC_FLAGS if word >> bit & 1]
    parts += [f"bit {bit} set (unused in this bit map: check the drive)" for bit in (14, 15) if word >> bit & 1]
    conditions = []
    if word & FAULT:
        conditions.append(("fault", "failure"))
    if word & ALARM:
        conditions.append(("warning", "maintenance_required"))
    if not word & READY and not word & FAULT:
        conditions.append(("warning", "function_check"))
    return Decoded(", ".join(parts), *worst(conditions))


# PowerFlex 520-series fault codes, the subset we name (520-UM001 as we read it).
FAULT_CODES = {
    2: "Auxiliary In", 3: "Power Loss", 4: "UnderVoltage", 5: "OverVoltage", 6: "Motor Stalled",
    7: "Motor Overload", 8: "Heatsink OvrTmp", 12: "HW OverCurrent", 13: "Ground Fault", 33: "Auto Rstrt Tries",
    38: "Phase U to Gnd", 39: "Phase V to Gnd", 40: "Phase W to Gnd", 63: "SW OverCurrent", 64: "Drive Overload",
    81: "Comm Loss", 100: "Parameter Checksum",
}


def fault_text(code: int) -> str:
    if code == 0:
        return "no fault"
    name = FAULT_CODES.get(code)
    return f"F{code:03d} {name}" if name else f"unknown fault {code}"


def fault_queue(codes: tuple, faulted: Optional[bool], previous: Optional[tuple]) -> Decoded:
    """The fault queue, most recent first, read against the Logic Status Fault bit: while the
    drive is faulted the most recent entry is the active fault (failure); otherwise the queue
    is history (good). ``previous`` is the queue at the last read, to name a new entry."""
    logged = [c for c in codes if c]
    head = codes[0] if codes else 0
    if faulted:
        if not head:
            return Decoded("drive faulted, fault queue empty", "fault", "failure")
        earlier = ", ".join(fault_text(c) for c in logged[1:])
        return Decoded(f"active fault {fault_text(head)}" + (f"; earlier {earlier}" if earlier else ""),
                       "fault", "failure")
    history = f"logged {', '.join(fault_text(c) for c in logged)}" if logged else "no faults logged"
    state = "good" if faulted is False else None
    if head and previous is not None and previous[:1] != (head,):
        cleared = " and already cleared" if faulted is False else ""
        return Decoded(f"new fault {fault_text(head)}{cleared}; {history}", "warning", state)
    prefix = "no active fault" if faulted is False else "active fault unknown (logic status not read)"
    return Decoded(f"{prefix}; {history}", "info", state)


# --- polling ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class ObjectRead:
    """One object instance: Get_Attributes_All without an attribute, Get_Attribute_Single with one."""
    class_code: int
    instance: int
    attribute: Optional[int] = None
    offset: int = 0                 # byte offset of the 16-bit word in the reply


@dataclass(frozen=True)
class QueueRead:
    """One attribute of several instances, most recent entry first (the fault queue)."""
    class_code: int
    instances: tuple
    attribute: int


DEFAULT_READS = {"identity": ObjectRead(0x01, 1)}
READ_KINDS = ("identity", "logic_status", "fault_queue")


class Reading(NamedTuple):
    object: str
    attribute: str                  # unique per device: the store's key is (asset, ts, device, attribute)
    raw_value: Union[int, str]
    decoded: str
    severity: str
    ne107: Optional[str]
    key: object                     # what change detection compares


class DevicePoller:
    """One device: read, decode, and emit a device_event_v1 for each attribute that changed.

    The first poll, and the first good poll after a comms loss, emit every attribute (a
    new baseline), so the latest event of each attribute is always its current state.
    A poll with no reply emits one comms-loss event, however long the loss lasts.
    """

    def __init__(self, asset: str, device_name: str, cip: ReadOnlyCIP, topic: str,
                 reads: Optional[dict] = None, store=None):
        self.asset = asset
        self.device = device_name
        self.cip = cip
        self.topic = topic
        self.reads = dict(reads) if reads is not None else dict(DEFAULT_READS)
        unknown = set(self.reads) - set(READ_KINDS)
        if unknown:
            raise ValueError(f"unknown read(s) {', '.join(sorted(unknown))}; known: {', '.join(READ_KINDS)}")
        self.store = store
        self.last: dict[str, object] = {}         # attribute -> key of its latest event
        self.identity: Optional[str] = None       # kept across comms losses, to notice a swapped device
        self.fault_codes: Optional[tuple] = None  # likewise, to notice a fault logged while unreachable
        self._read_now: dict[str, object] = {}    # what this poll read; kept only if the whole poll answers
        self.comms: Optional[bool] = None         # None before the first poll
        self.counts = Counter({"polls": 0, "events": 0, "comms_losses": 0})

    def poll(self, now: dt.datetime) -> list[Out]:
        """Read the device at ``now`` (aware UTC, the events' ts) and return the events to publish."""
        self.counts["polls"] += 1
        try:
            readings = self.read()
        except CommsLost as exc:
            return self._lost(now, exc)
        self.identity = self._read_now.get("identity", self.identity)
        self.fault_codes = self._read_now.get("fault_codes", self.fault_codes)
        events = []
        if self.comms is not True:
            text = "comms restored" if self.comms is False else "comms established"
            events.append(Reading("connection", "comms", "ok", text, "info", "good", "ok"))
            self.comms = True
        for r in readings:
            if r.attribute not in self.last or self.last[r.attribute] != r.key:
                self.last[r.attribute] = r.key
                events.append(r)
        return [self._emit(now, r) for r in events]

    def _lost(self, now: dt.datetime, exc: CommsLost) -> list[Out]:
        if self.comms is False:
            return []
        self.comms = False
        self.last.clear()                          # the next good poll is a new baseline
        self.counts["comms_losses"] += 1
        return [self._emit(now, Reading("connection", "comms", str(exc), f"comms loss: {exc}", "warning",
                                        "function_check", "lost"))]

    def read(self) -> list[Reading]:
        """Every configured read, decoded. CommsLost when the device does not answer."""
        self._read_now = {}
        readings = []
        if "identity" in self.reads:
            readings += self._identity(self.reads["identity"])
        faulted = None
        if "logic_status" in self.reads:
            reading, faulted = self._logic_status(self.reads["logic_status"])
            readings.append(reading)
        if "fault_queue" in self.reads:
            readings.append(self._fault_queue(self.reads["fault_queue"], faulted))
        return readings

    def _failed(self, obj: str, attribute: str, exc: Exception) -> Reading:
        return Reading(obj, attribute, str(exc)[:200], f"read failed: {exc}", "warning", "function_check",
                       ("error", str(exc)))

    def _identity(self, spec: ObjectRead) -> list[Reading]:
        try:
            ident = decode_identity(self.cip.get(spec.class_code, spec.instance, name=f"{self.device} identity"))
        except (CIPError, DecodeError) as exc:
            return [self._failed("identity", "product", exc), self._failed("identity", "status", exc)]
        product = Decoded(describe_identity(ident), "info", "good")
        if self.identity is not None and self.identity != ident.raw:
            product = Decoded(f"identity changed (was {self.identity}): {product.text}", "warning", "good")
        self._read_now["identity"] = ident.raw
        status = identity_status(ident.status)
        return [Reading("identity", "product", ident.raw, product.text, product.severity, product.ne107, ident.raw),
                Reading("identity", "status", ident.status, status.text, status.severity, status.ne107, ident.status)]

    def _logic_status(self, spec: ObjectRead) -> tuple[Reading, Optional[bool]]:
        try:
            word = word_at(self.cip.get(spec.class_code, spec.instance, spec.attribute,
                                        name=f"{self.device} logic status"), spec.offset)
        except (CIPError, DecodeError) as exc:
            return self._failed("drive", "logic_status", exc), None
        d = logic_status(word)
        return Reading("drive", "logic_status", word, d.text, d.severity, d.ne107, word & WATCHED), bool(word & FAULT)

    def _fault_queue(self, spec: QueueRead, faulted: Optional[bool]) -> Reading:
        try:
            codes = tuple(word_at(self.cip.get(spec.class_code, instance, spec.attribute,
                                               name=f"{self.device} fault {i}"))
                          for i, instance in enumerate(spec.instances, 1))
        except (CIPError, DecodeError) as exc:
            return self._failed("drive", "fault_queue", exc)
        d = fault_queue(codes, faulted, self.fault_codes)
        self._read_now["fault_codes"] = codes
        return Reading("drive", "fault_queue", ",".join(str(c) for c in codes), d.text, d.severity, d.ne107,
                       (codes, faulted))

    def _emit(self, now: dt.datetime, r: Reading) -> Out:
        doc = {"schema": EVENT_SCHEMA, "asset": self.asset, "ts": iso_utc(now), "device": self.device,
               "object": r.object, "attribute": r.attribute, "raw_value": r.raw_value, "decoded": r.decoded,
               "severity": r.severity}
        if r.ne107 is not None:
            doc["ne107_class"] = r.ne107
        self.counts["events"] += 1
        if self.store is not None:
            self.store.insert("device_events", {
                "asset_id": self.asset, "ts": doc["ts"], "device": self.device, "object": r.object,
                "attribute": r.attribute, "raw_value": str(r.raw_value), "decoded": r.decoded,
                "severity": r.severity, "ne107_class": r.ne107}, ignore_conflict=True)
        log.info("%s %s.%s: %s [%s, %s]", self.device, r.object, r.attribute, r.decoded, r.severity, r.ne107 or "-")
        return Out(self.topic, canonical_json(doc))

    def report(self) -> dict:
        return dict(self.counts, device=self.device, comms=self.comms, sent=self.cip.sent, refused=self.cip.refused)


class Schedule:
    """Polls each device once its interval has passed; run_service calls ``tick``."""

    def __init__(self, pollers: list[tuple[DevicePoller, float]], clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], dt.datetime] = lambda: dt.datetime.now(UTC)):
        self.entries = [[0.0, poller, float(every)] for poller, every in pollers]
        self.clock = clock
        self.now = now

    def tick(self) -> list[Out]:
        outs = []
        for entry in self.entries:
            due, poller, every = entry
            t = self.clock()
            if t >= due:
                entry[0] = t + every
                outs += poller.poll(self.now())
        return outs


# --- the device file --------------------------------------------------------------------------

class ConfigError(ValueError):
    """The device file cannot be read or does not make sense."""


@dataclass(frozen=True)
class Device:
    name: str
    path: str                       # pycomm3 path: an IP address, or a route through a controller
    poll_seconds: float = 5.0
    timeout_seconds: float = 2.0
    routed: bool = False
    reads: dict = field(default_factory=lambda: dict(DEFAULT_READS))


READ_KEYS = {"identity": {"class", "instance"}, "logic_status": {"class", "instance", "attribute", "offset"},
             "fault_queue": {"class", "instances", "attribute"}}


def _int(value, where: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{where} must be an integer from {low} to {high}, got {value!r}")
    return value


def _seconds(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 3600:
        raise ConfigError(f"{where} must be a number of seconds above 0, got {value!r}")
    return float(value)


def _reads(raw, where: str) -> dict:
    if not isinstance(raw, dict) or not raw:
        raise ConfigError(f"{where} must map {', '.join(READ_KINDS)} to the objects to read")
    reads = {}
    for kind, spec in raw.items():
        at = f"{where}.{kind}"
        if kind not in READ_KEYS:
            raise ConfigError(f"{at}: unknown read; known: {', '.join(READ_KINDS)}")
        if not isinstance(spec, dict):
            raise ConfigError(f"{at} must be a mapping")
        extra = set(spec) - READ_KEYS[kind]
        if extra:                   # a service code, for one, cannot be configured
            raise ConfigError(f"{at}: unknown key(s) {', '.join(sorted(map(str, extra)))}")
        class_code = _int(spec.get("class"), f"{at}.class", 1, 0xFFFF)
        if kind == "fault_queue":
            instances = spec.get("instances")
            if not isinstance(instances, list) or not instances:
                raise ConfigError(f"{at}.instances must be a list, most recent entry first")
            reads[kind] = QueueRead(class_code, tuple(_int(x, f"{at}.instances", 0, 0xFFFF) for x in instances),
                                    _int(spec.get("attribute"), f"{at}.attribute", 1, 0xFFFF))
        elif kind == "identity":
            reads[kind] = ObjectRead(class_code, _int(spec.get("instance"), f"{at}.instance", 0, 0xFFFF))
        else:
            reads[kind] = ObjectRead(class_code, _int(spec.get("instance"), f"{at}.instance", 0, 0xFFFF),
                                     _int(spec.get("attribute"), f"{at}.attribute", 1, 0xFFFF),
                                     _int(spec.get("offset", 0), f"{at}.offset", 0, 0xFFFF))
    return reads


def load_devices(path) -> list[Device]:
    """The devices to poll, from a YAML file like services/south/devices.example.yaml."""
    raw = read_yaml(Path(path), error=ConfigError, what="device file")
    entries = raw.get("devices")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{path}: 'devices' must be a non-empty list")
    devices: list[Device] = []
    for i, entry in enumerate(entries):
        at = f"{path}: devices[{i}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{at} must be a mapping")
        extra = set(entry) - {"name", "path", "poll_seconds", "timeout_seconds", "routed", "read"}
        if extra:
            raise ConfigError(f"{at}: unknown key(s) {', '.join(sorted(map(str, extra)))}")
        name, route = entry.get("name"), entry.get("path")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{at}.name is required")
        if any(d.name == name for d in devices):
            raise ConfigError(f"{at}: device {name} appears twice")
        if not isinstance(route, str) or not route:
            raise ConfigError(f"{at}.path is required")
        routed = entry.get("routed", False)
        if not isinstance(routed, bool):
            raise ConfigError(f"{at}.routed must be true or false")
        devices.append(Device(name, route, _seconds(entry.get("poll_seconds", 5.0), f"{at}.poll_seconds"),
                              _seconds(entry.get("timeout_seconds", 2.0), f"{at}.timeout_seconds"), routed,
                              _reads(entry.get("read", {"identity": {"class": 1, "instance": 1}}), f"{at}.read")))
    return devices


# --- pycomm3 and the command line ---------------------------------------------------------------

class Pycomm3Link:
    """A pycomm3 CIPDriver opened on first use and closed after any failure, so the next
    poll reconnects. Only ReadOnlyCIP calls it. Opening sends Register Session, an
    EtherNet/IP encapsulation command, not a CIP service."""

    def __init__(self, driver_cls, path: str, timeout: float):
        self._driver_cls = driver_cls
        self.path = path
        self.timeout = timeout
        self._driver = None

    def generic_message(self, **kwargs):
        if self._driver is None:
            driver = self._driver_cls(self.path)
            if hasattr(driver, "socket_timeout"):
                driver.socket_timeout = self.timeout
            try:
                if driver.open() is False:            # pycomm3 returns False when no session is registered
                    raise ConnectionError(f"no EtherNet/IP session with {self.path}")
            except Exception:
                _close_quietly(driver)
                raise
            self._driver = driver
        try:
            return self._driver.generic_message(**kwargs)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._driver is not None:
            _close_quietly(self._driver)
            self._driver = None


def _close_quietly(driver) -> None:
    try:
        driver.close()
    except Exception:                             # already gone
        pass


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile", required=True, help="asset profile YAML (the asset id and topics.device)")
    p.add_argument("--devices", required=True, help="device file, see services/south/devices.example.yaml")
    p.add_argument("--once", action="store_true",
                   help="poll every device once, print the events and exit (no broker, no store)")
    p.add_argument("--broker", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--store", default=None, help="store URL (default $PDM_STORE or sqlite:///runs/pdm.db)")
    p.add_argument("--no-store", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        profile = load_profile(args.profile)
        devices = load_devices(args.devices)
    except (ProfileError, ConfigError) as exc:
        raise SystemExit(f"south-cip: {exc}")
    try:
        from pycomm3 import CIPDriver
        from pycomm3.exceptions import CommError
    except ImportError:
        raise SystemExit("south-cip: pycomm3 is not installed: pip install 'pycomm3>=1.2'")
    store = None
    if not (args.no_store or args.once):
        from pdm_common.store import open_store
        store = open_store(args.store)
    asset, topic = profile["asset"]["id"], profile["topics"]["device"]
    links, pollers = [], []
    for d in devices:
        link = Pycomm3Link(CIPDriver, d.path, d.timeout_seconds)
        cip = ReadOnlyCIP(link, comms_errors=(OSError, TimeoutError, CommError), routed=d.routed)
        links.append(link)
        pollers.append((DevicePoller(asset, d.name, cip, topic, d.reads, store), d.poll_seconds))
        log.info("device %s at %s: %s every %.0f s -> %s", d.name, d.path, ", ".join(d.reads), d.poll_seconds, topic)
    try:
        if args.once:
            for poller, _ in pollers:
                for out in poller.poll(dt.datetime.now(UTC)):
                    print(out.payload)
            return
        run_service("south-cip", {}, host=args.broker, port=args.port, tick=(0.5, Schedule(pollers).tick))
    finally:
        for link in links:
            link.close()
    print(json.dumps([p.report() for p, _ in pollers], sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
