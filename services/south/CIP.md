# CIP device status (G8 stretch)

Read-only CIP client for the drives and controllers on the isolated bench
(T23; INT-07, INT-08): `services/south/cip.py`.

```bash
pip install 'pycomm3>=1.2'
cp services/south/devices.example.yaml services/south/devices.yaml     # then fill in the bench

# bring-up: poll every device once, print the events; no broker, no store
python services/south/cip.py --profile profiles/metropt3_apu/asset_profile.yaml \
    --devices services/south/devices.yaml --once

# service: poll every poll_seconds, publish, store
python services/south/cip.py --profile profiles/metropt3_apu/asset_profile.yaml \
    --devices services/south/devices.yaml [--store "$PDM_STORE" | --no-store]
```

Publishes `device_event_v1` on the profile's `topics.device`
(`pdm/<asset>/device`), not retained, and writes each event to the store's
`device_events` table, which is where the agent's device_health tool reads it
as of an incident. Ctrl-C prints each device's counts: polls, events, comms
losses, requests sent and refused.

## What it reads

| Read (device file key) | CIP request | Becomes |
|---|---|---|
| `identity` | Identity object (class 0x01, instance 1), Get_Attributes_All | `identity.product` (vendor, device type, product code, revision, serial number, product name) and `identity.status` (the status word) |
| `logic_status` | Get_Attribute_Single on the class, instance and attribute given; the 16-bit word at `offset` | `drive.logic_status` |
| `fault_queue` | Get_Attribute_Single on each instance listed, most recent first | `drive.fault_queue` |
| (always) | none | `connection.comms`: established, lost, restored |

A device with no `read` section gets `identity` only, which suits a
controller. In the example file the fault queue is parameters b007-b009
[Fault 1-3 Code], read through the Parameter object (class 0x0F, instance =
parameter number, attribute 1 = value).

## The allowlist (INT-08)

`ReadOnlyCIP` is the only object that holds the pycomm3 driver, and `get` is
its only method. Before the driver is called, the service code is checked:

- **Allowed:** Get_Attributes_All (0x01), Get_Attribute_List (0x03),
  Get_Attribute_Single (0x0E).
- **Multiple_Service_Packet (0x0A):** allowed only if every request inside it
  is allowed. The packet is parsed (count, offsets, then each request's
  service, path size and path), and a packet inside a packet is checked the
  same way, up to four levels deep. A packet that does not parse is refused.
- **Refused with WriteRefused**, nothing sent: Set_Attributes_All (0x02),
  Set_Attribute_List (0x04), Reset (0x05), Start (0x06), Stop (0x07),
  Create (0x08), Delete (0x09), Apply_Attributes (0x0D),
  Set_Attribute_Single (0x10), Restore (0x15), Save (0x16), the member
  services, every object-specific or vendor code (0x32-0x7F, including Logix
  Read Tag 0x4C and Write Tag 0x4D), reply codes (0x80 and up), and anything
  that is not one service code, such as a string, two bytes or a boolean.

Requests are unconnected (`connected=False`), so no Forward Open is sent. The
device file has no key for a service code, and an unknown key stops the
service at start-up.

On the wire there should then be only: Register Session and Unregister
Session (EtherNet/IP encapsulation commands that pycomm3 sends when it opens
and closes the connection), the Get requests, and, for a device with
`routed: true`, the Unconnected Send (0x52 to the Connection Manager) that
carries them through the controller.

The negative tests are in tests/test_cip.py. Every code from 0 to 255 other
than the three Gets is refused by `check_service`. Each named write service
sent through `ReadOnlyCIP.get` raises WriteRefused, and a recording driver
shows no call. The same holds for a Set inside a Multiple_Service_Packet,
for a Set inside a packet nested in a packet, and for a malformed packet.
The packet capture INT-08 asks for is a bench step (below).

## Decoding

### Identity status word (attribute 5)

From the Identity object in the CIP specification (Vol. 1):

| Bits | Meaning | Severity, NE 107 class |
|---|---|---|
| 0 | owned | — |
| 2 | configured (differs from the out-of-box configuration) | — |
| 4-7 = 1 | firmware update in progress | warning, function_check |
| 4-7 = 2 | at least one faulted I/O connection | warning, function_check |
| 4-7 = 4 | non-volatile configuration bad | warning, maintenance_required |
| 4-7 = 0, 3, 5, 6, 7 | self-testing or unknown; no I/O connections; major fault; an I/O connection in run mode; all I/O connections idle | — (text only) |
| 4-7 = 8, 9 / 10-15 | reserved / vendor specific | — (text only) |
| 8 | minor recoverable fault | warning, out_of_specification |
| 9 | minor unrecoverable fault | warning, maintenance_required |
| 10 | major recoverable fault | fault, failure |
| 11 | major unrecoverable fault | fault, failure |
| 12-15 | vendor specific | — |

### Logic Status word

The PowerFlex 525 bit map, as we read the PowerFlex 520-series user manual
(520-UM001). **Check it against the manual for the drive on the bench.**

| Bit | 1 means |
|---|---|
| 0 | Ready |
| 1 | Active (running) |
| 2 | Command direction forward |
| 3 | Actual direction forward |
| 4 | Accelerating |
| 5 | Decelerating |
| 6 | Alarm |
| 7 | Fault |
| 8 | At speed (at reference) |
| 9 | Frequency reference from comms |
| 10 | Start/stop from comms |
| 11 | Parameters locked |
| 12, 13 | Digital input 1, 2 on |
| 14, 15 | Not used; if either is set, the text says so, since it suggests a different bit map |

Classes: Fault bit set → fault, failure. Alarm → warning,
maintenance_required. Not Ready without a fault (a stop or enable input, safe
torque off, the bus not yet charged) → warning, function_check: the drive
cannot run, but nothing in it has failed. Otherwise info, good. Only Ready,
Active, Alarm and Fault make an event; the other bits flip with every speed
change or are configuration, and they appear in the text of the next event.

### Fault codes

The subset we name, as we read 520-UM001. Any other code reads
`unknown fault <n>`.

| Code | Name | Code | Name |
|---|---|---|---|
| F002 | Auxiliary In | F033 | Auto Rstrt Tries |
| F003 | Power Loss | F038 | Phase U to Gnd |
| F004 | UnderVoltage | F039 | Phase V to Gnd |
| F005 | OverVoltage | F040 | Phase W to Gnd |
| F006 | Motor Stalled | F063 | SW OverCurrent |
| F007 | Motor Overload | F064 | Drive Overload |
| F008 | Heatsink OvrTmp | F081 | Comm Loss |
| F012 | HW OverCurrent | F100 | Parameter Checksum |
| F013 | Ground Fault | | |

### Fault queue

The queue is history: a drive keeps its last faults after they are reset. It
is read against the Logic Status Fault bit from the same poll:

- **Drive faulted:** the most recent entry is the active fault, so the event
  is fault, failure (`active fault F004 UnderVoltage; earlier F013 Ground
  Fault`).
- **Not faulted:** info, good (`no active fault; logged F004 UnderVoltage`).
- **Not faulted, but a new entry since the last read** (a fault that tripped
  and was reset between polls, or while the device was unreachable):
  warning, good (`new fault F004 UnderVoltage and already cleared`).
- **Logic Status not read:** whether a fault is active cannot be told, so the
  event has no `ne107_class`.

## Events

- One `device_event_v1` for each attribute that changed since the last poll:
  `identity.product`, `identity.status`, `drive.logic_status` (its watched
  bits), `drive.fault_queue` (the codes, or the Fault bit) and
  `connection.comms`.
- The first poll, and the first good poll after a comms loss, emit every
  attribute. The latest event of each attribute is therefore its current
  state, which is what device_health needs to read as of an incident.
- No reply (a timeout or a broken connection) gives one comms-loss event,
  warning and function_check, however long the loss lasts, then `comms
  restored` and a new baseline.
- A read the device refuses (for example, `Object does not exist` from a
  wrong instance number) gives `read failed: ...`, warning and
  function_check, for that attribute only; the rest of the poll goes on.
- A different serial number or revision gives a warning event,
  `identity changed (was ...)`, including after a comms loss.
- `ts` is the poll time in UTC. For a live device, stream time is wall time.
- Attribute names are unique per device, because the store's key is
  (asset_id, ts, device, attribute).

| Field | Meaning |
|---|---|
| `schema`, `asset`, `ts` | `device_event_v1`, the profile's asset id, the poll time |
| `device` | the device's name in the device file |
| `object`, `attribute` | `identity` / `product`, `status`; `drive` / `logic_status`, `fault_queue`; `connection` / `comms` |
| `raw_value` | what was read: the status word as an integer, the identity as `vendor:type:product:revision:serial`, the fault codes as `4,13,0`, or the error text |
| `decoded` | the decoding above, in words |
| `severity` | `info`, `warning` or `fault`: how notable the event is |
| `ne107_class` | the device state the event implies; left out when it cannot be told |

## Bench scenarios

The eight fixtures in tests/test_cip.py run the poller against a fake drive.
Each is a sequence of polls, and each poll's events are compared with the
expected (object, attribute, severity, NE 107 class) list. "Baseline" means
the five info/good events of a first poll.

| | Scenario | Drive state, poll by poll | Events after the baseline |
|---|---|---|---|
| S1 | healthy | ready, running forward at speed, queue empty | baseline only |
| S2 | undervoltage fault | healthy; Fault bit, queue F004 | logic_status fault/failure; fault_queue `active fault F004 UnderVoltage`, fault/failure |
| S3 | ground fault | healthy with F004 logged; Fault bit, queue F013, F004 | logic_status and fault_queue fault/failure (`active fault F013 Ground Fault; earlier F004 UnderVoltage`) |
| S4 | motor overload alarm, then fault | healthy; Alarm bit; Fault bit, queue F007 | logic_status warning/maintenance_required; then logic_status and fault_queue fault/failure (`F007 Motor Overload`) |
| S5 | comms loss | healthy; no reply; no reply; healthy | one comms event warning/function_check; nothing; `comms restored` and a new baseline |
| S6 | drive not ready | healthy; Ready clear, no fault | logic_status warning/function_check |
| S7 | identity minor recoverable fault | healthy; identity status bit 8 | identity.status warning/out_of_specification |
| S8 | fault clears back to healthy | Fault bit, queue F005; healthy, F005 still logged | first poll's logic_status and fault_queue fault/failure; then both info/good (`no active fault; logged F005 OverVoltage`) |

These fixtures pin down what the decoder reports for each drive state.
Whether the agent then puts an incident down to the process or to the device
(INT-07: right in 6 of 8 scenarios) needs the device_health tool, the agent
and the bench. On the bench, a team member induces each fault at the drive
keypad or in Studio 5000, never PdM-Agent (PR-20).

## On the bench

1. Fill in `devices.yaml` and run with `--once`. Compare every decoded value
   with the drive's display or Studio 5000: the identity, each Logic Status
   bit against the manual's table, and the fault queue against b007-b009.
2. Confirm where the Logic Status word comes from. The example's Assembly
   object instance 2 is an assumption; take the instance (or another object)
   from the drive manual's EtherNet/IP chapter.
3. Packet capture for INT-08: capture the bench segment in Wireshark (filter
   `enip`) while running this from `services/south`:

   ```python
   from pycomm3 import CIPDriver
   from pycomm3.exceptions import CommError
   from cip import Pycomm3Link, ReadOnlyCIP, WriteRefused, SET_ATTRIBUTE_SINGLE

   link = Pycomm3Link(CIPDriver, "192.168.1.20", 2.0)
   reader = ReadOnlyCIP(link, comms_errors=(OSError, TimeoutError, CommError))
   reader.get(0x01, 1)                                        # one Get_Attributes_All
   try:
       reader.get(0x0F, 41, 1, service=SET_ATTRIBUTE_SINGLE)  # refused: nothing on the wire
   except WriteRefused as exc:
       print(exc)
   reader.get(0x01, 1)                                        # a second Get_Attributes_All
   link.close()
   ```

   The capture should hold Register Session, two Get_Attributes_All requests
   and their replies, Unregister Session, and nothing else.
4. Run the eight scenarios with the agent and record the attributions as case
   studies.

## Not verified here

There was no PLC, no drive and no pycomm3 where this was written.

- Every reply in the tests was built by hand from the formats above.
  `main()` has run only in `--once` mode, against a stand-in for pycomm3.
- pycomm3's API is used as we know it from pycomm3 1.2: `CIPDriver(path)`,
  `open()`, `close()`, `generic_message(service=, class_code=, instance=,
  attribute=, request_data=, data_type=None, name=, connected=False,
  unconnected_send=, route_path=)`, the reply's `value` and `error`,
  `pycomm3.exceptions.CommError`, and `socket_timeout`. `socket_timeout` is
  set only if the driver has it; otherwise pycomm3's own timeout applies.
- The Logic Status bit map and the fault names come from our reading of
  520-UM001. They were not checked against the manual here.
- The Identity status bits come from the CIP specification as we know it.
- The example's Logic Status source (Assembly object, instance 2, attribute
  3) is an assumption, and so is reading the fault queue through the
  Parameter object with parameter numbers as instances.
- The packet capture and the process-versus-device attribution (INT-07) are
  still to do on the bench.
