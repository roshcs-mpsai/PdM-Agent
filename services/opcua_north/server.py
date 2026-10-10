"""Northbound OPC UA server -- read-only health, prognosis and NE 107 nodes per asset (T21; INT-01 to INT-03).

Subscribes to each profile's scores, prognosis and status topics and keeps one
folder of variables per asset under Objects/PdM/<asset>/, node ids
``ns=<idx>;s=<asset>.<Name>`` in the namespace ``urn:pdm-agent``:

* Score, Threshold, Alert, LastUpdate and DetectorRun from score_v1;
* P<h>, P<h>Lower and P<h>Upper for each horizon the profile declares (24, 72
  and 168 h), SkillMeetsBar and ModelRun from prognosis_v1;
* NE107, NE107Code, Causes, IncidentId and IncidentState from status_v1;
* ProfileSha256 from whichever of the three carries it last.

Every value comes from a payload; the server computes nothing. A variable's
source timestamp is the payload's stream time, its server timestamp wall time.
Until a payload sets it, a variable reads BadWaitingForInitialData.

Read-only (INT-02), in four layers: every variable's AccessLevel and
UserAccessLevel are CurrentRead and its write masks 0; the server adds no
methods; no session is ever given the Admin role (asyncua lets an Admin write
anything); and a permission ruleset passes only the read, browse and
subscription services, so Write, Call, HistoryUpdate and node management are
refused with BadUserAccessDenied for every session before they reach the
address space. At start-up the server reads its own nodes back and refuses to
run if any is writable or callable.

``AddressSpace`` is the address space as plain data and ``NorthService`` the
payload handling; the tests drive both without asyncua. ``OpcUaServer``
mirrors the model into an asyncua server (asyncua is imported only there).
main() runs asyncua on its own event loop in a background thread
(``ServerThread``) and MQTT in the main thread: run_service installs the
SIGINT/SIGTERM handlers, which Python allows only in the main thread. paho
calls the handlers on its network thread; each model update is handed to the
server's loop with call_soon_threadsafe and written by a single task, in
arrival order, so asyncua objects are only touched from their own loop.

    python services/opcua_north/server.py --profile profiles/metropt3_apu/asset_profile.yaml --anonymous
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hmac
import json
import logging
import os
import sys
import threading
from collections import Counter
from dataclasses import dataclass
from typing import Callable, NamedTuple, Optional

from pdm_common import schemas
from pdm_common.mqtt import Out, run_service
from pdm_common.profile import ProfileError, load_profile
from pdm_common.timeutil import UTC, parse_duration, to_utc

log = logging.getLogger("opcua_north")

NAMESPACE_URI = "urn:pdm-agent"
DEFAULT_ENDPOINT = "opc.tcp://0.0.0.0:4840/pdm/"
ROOT_FOLDER = "PdM"
DEFAULT_HORIZONS = (24, 72, 168)            # PRG-01; used when the profile declares none

DOUBLE, BOOLEAN, INT32, STRING, DATETIME = "Double", "Boolean", "Int32", "String", "DateTime"

# NE 107 as a number for SCADA alarming: higher is more severe, the order pdm_common.status
# applies when several conditions hold (failure first, good last). Fixed here rather than
# derived, so a SCADA configuration never shifts; tests/test_opcua_north.py checks the two agree.
NE107_CODE = {"good": 0, "maintenance_required": 1, "out_of_specification": 2, "function_check": 3, "failure": 4}

READ_ONLY = 0x01        # AccessLevel and UserAccessLevel: bit 0 (CurrentRead) only

# The service requests a read-only session needs. Everything else -- Write, HistoryUpdate,
# Call, AddNodes, DeleteNodes, AddReferences, DeleteReferences, RegisterServer, Query -- is
# refused for every session (ReadOnlyRuleset).
ALLOWED_REQUESTS = (
    "FindServersRequest", "FindServersOnNetworkRequest", "GetEndpointsRequest",
    "CreateSessionRequest", "ActivateSessionRequest", "CloseSessionRequest", "CancelRequest",
    "CloseSecureChannelRequest",
    "BrowseRequest", "BrowseNextRequest", "TranslateBrowsePathsToNodeIdsRequest",
    "RegisterNodesRequest", "UnregisterNodesRequest",
    "ReadRequest", "HistoryReadRequest",
    "CreateSubscriptionRequest", "ModifySubscriptionRequest", "SetPublishingModeRequest", "PublishRequest",
    "RepublishRequest", "TransferSubscriptionsRequest", "DeleteSubscriptionsRequest",
    "CreateMonitoredItemsRequest", "ModifyMonitoredItemsRequest", "SetMonitoringModeRequest",
    "SetTriggeringRequest", "DeleteMonitoredItemsRequest",
)


# --- the address space as plain data ----------------------------------------------------------

class AccessDenied(PermissionError):
    """A client write or method call: the server refuses both (INT-02)."""
    status = "BadUserAccessDenied"


class Spec(NamedTuple):
    name: str
    data_type: str
    source: str                 # the payload field the value comes from
    description: str


def variable_specs(horizons=DEFAULT_HORIZONS) -> list[Spec]:
    """The variables in each asset folder, in browse order."""
    specs = [
        Spec("Score", DOUBLE, "score_v1 score", "Anomaly score of the latest window"),
        Spec("Threshold", DOUBLE, "score_v1 threshold", "Detector threshold; Alert is Score over Threshold"),
        Spec("Alert", BOOLEAN, "score_v1 alert",
             "Latest score over the threshold; the persistence gate decides whether it becomes an incident"),
    ]
    for h in horizons:
        specs += [
            Spec(f"P{h}", DOUBLE, f"prognosis_v1 horizons.{h}.p", f"P(failure within {h} h)"),
            Spec(f"P{h}Lower", DOUBLE, f"prognosis_v1 horizons.{h}.lower", f"Lower bound of P(failure within {h} h)"),
            Spec(f"P{h}Upper", DOUBLE, f"prognosis_v1 horizons.{h}.upper", f"Upper bound of P(failure within {h} h)"),
        ]
    specs += [
        Spec("SkillMeetsBar", BOOLEAN, "prognosis_v1 skill.meets_bar",
             "Whether the survival model meets the PRD skill bar; when false, read the probabilities "
             "against the constant-hazard baseline"),
        Spec("NE107", STRING, "status_v1 ne107",
             "NAMUR NE 107 status: good, maintenance_required, out_of_specification, function_check or failure"),
        Spec("NE107Code", INT32, "status_v1 ne107",
             "NE 107 status as a number, higher is more severe: 0 good, 1 maintenance required, "
             "2 out of specification, 3 function check, 4 failure"),
        Spec("Causes", STRING, "status_v1 causes", "Every condition behind the NE 107 status, joined by '; '"),
        Spec("IncidentId", STRING, "status_v1 incident_id", "The incident not yet closed; empty when there is none"),
        Spec("IncidentState", STRING, "status_v1 incident_state",
             "open, diagnosing, awaiting_review, approved, rejected or sent; empty when there is no incident"),
        Spec("DetectorRun", STRING, "score_v1 detector_run_id", "Frozen detector run that scored the latest window"),
        Spec("ModelRun", STRING, "prognosis_v1 model_run_id", "Frozen survival-model run behind the probabilities"),
        Spec("ProfileSha256", STRING, "profile_sha256 of the latest score, prognosis or status",
             "SHA-256 of the asset profile in force"),
        Spec("LastUpdate", DATETIME, "score_v1 window_end", "Stream time of the latest scored window"),
    ]
    return specs


class Sample(NamedTuple):
    """One variable as of an update: what listeners (the asyncua mirror) receive."""
    node_id: str
    asset: str
    name: str
    data_type: str
    value: object
    source_timestamp: Optional[dt.datetime]


@dataclass
class Variable:
    asset: str
    name: str
    data_type: str
    source: str
    description: str
    ns_index: int
    value: object = None                              # None until a payload sets it
    source_timestamp: Optional[dt.datetime] = None

    @property
    def node_id(self) -> str:
        return f"ns={self.ns_index};s={self.asset}.{self.name}"

    @property
    def browse_name(self) -> str:
        return self.name

    @property
    def writable(self) -> bool:
        return False                                  # no variable is ever writable (INT-02)

    def sample(self) -> Sample:
        return Sample(self.node_id, self.asset, self.name, self.data_type, self.value, self.source_timestamp)


def coerce(data_type: str, value):
    """A payload value as the variable's type; None stays None (no value)."""
    if value is None:
        return None
    if data_type == DOUBLE:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{value!r} is not a number")
        return float(value)
    if data_type == BOOLEAN:
        if not isinstance(value, bool):
            raise TypeError(f"{value!r} is not a boolean")
        return value
    if data_type == INT32:
        if isinstance(value, bool) or not isinstance(value, int) or not -2**31 <= value < 2**31:
            raise TypeError(f"{value!r} is not an Int32")
        return value
    if data_type == STRING:
        if not isinstance(value, str):
            raise TypeError(f"{value!r} is not a string")
        return value
    if data_type == DATETIME:
        return to_utc(value)
    raise ValueError(f"unknown data type {data_type}")


class AddressSpace:
    """Objects/PdM/<asset>/<variable>: every variable read-only, no methods.

    ``update`` is the service's only way in; ``write`` and ``call`` are what a
    client can attempt, and both raise AccessDenied.
    """

    namespace_uri = NAMESPACE_URI
    root = ROOT_FOLDER
    methods: tuple = ()                               # the server exposes no callable method

    def __init__(self, ns_index: int = 2):
        self.ns_index = ns_index                      # the first namespace a server registers after ns 0 and 1
        self._folders: dict[str, dict[str, Variable]] = {}
        self._by_id: dict[str, Variable] = {}
        self._listeners: list[Callable[[list[Sample]], None]] = []

    def add_asset(self, asset: str, horizons=DEFAULT_HORIZONS) -> None:
        if asset in self._folders:
            raise ValueError(f"asset {asset} is already in the address space")
        folder = {}
        for spec in variable_specs(horizons):
            var = Variable(asset, spec.name, spec.data_type, spec.source, spec.description, self.ns_index)
            folder[spec.name] = var
            self._by_id[var.node_id] = var
        self._folders[asset] = folder

    @property
    def assets(self) -> list[str]:
        return list(self._folders)

    def variables(self, asset: Optional[str] = None) -> list[Variable]:
        if asset is not None:
            return list(self._folders[asset].values())
        return [v for folder in self._folders.values() for v in folder.values()]

    def node(self, node_id: str) -> Variable:
        try:
            return self._by_id[node_id]
        except KeyError:
            raise KeyError(f"no node {node_id}") from None

    def read(self, node_id: str):
        return self.node(node_id).value

    def write(self, node_id: str, value) -> None:
        """A client write: always refused, the value untouched (INT-02)."""
        raise AccessDenied(f"{node_id} is read-only")

    def call(self, object_id: str, method_id: str, *args) -> None:
        """A client method call: always refused; the server has no methods (INT-02)."""
        raise AccessDenied(f"{method_id} on {object_id}: the server exposes no methods")

    def listen(self, listener: Callable[[list[Sample]], None]) -> None:
        self._listeners.append(listener)

    def unlisten(self, listener: Callable[[list[Sample]], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def update(self, asset: str, values: dict, ts: dt.datetime) -> list[Sample]:
        """Set variables from one payload, all with source timestamp ``ts``, and tell the listeners."""
        folder = self._folders[asset]
        changed = []
        for name, value in values.items():
            var = folder[name]
            var.value = coerce(var.data_type, value)
            var.source_timestamp = ts
            changed.append(var.sample())
        for listener in list(self._listeners):
            try:
                listener(changed)
            except Exception:                         # the model stays right even if a mirror fails
                log.exception("listener failed on %d update(s) for %s", len(changed), asset)
        return changed


# --- payloads in --------------------------------------------------------------------------------

def horizons_of(profile: dict) -> tuple[int, ...]:
    """The prognosis horizons in hours, from the profile's prognosis.horizons."""
    declared = (profile.get("prognosis") or {}).get("horizons")
    if not declared:
        return DEFAULT_HORIZONS
    hours = []
    for h in declared:
        seconds = parse_duration(h).total_seconds()
        if seconds <= 0 or seconds % 3600:
            raise ProfileError(f"prognosis horizon {h} is not a whole number of hours")
        hours.append(int(seconds // 3600))
    return tuple(hours)


def _no_constants(name: str):
    raise ValueError(f"{name} is not JSON")


class NorthService:
    """The service minus MQTT and OPC UA: score, prognosis and status payloads in, model updates out.

    A payload for another asset is ignored and counted; one that is not JSON, not an
    object, of the wrong schema or invalid against its schema is ignored and counted as
    malformed, by reason. Nothing is published back to MQTT.
    """

    def __init__(self, profile: dict, space: Optional[AddressSpace] = None):
        self.asset = profile["asset"]["id"]
        self.topics = profile["topics"]
        self.horizons = horizons_of(profile)
        self.space = space if space is not None else AddressSpace()
        self.space.add_asset(self.asset, self.horizons)
        self.counts = Counter({"scores": 0, "prognoses": 0, "statuses": 0, "other_asset": 0, "malformed": 0})
        self.malformed_by: Counter = Counter()
        self.ne107: Optional[str] = None

    def handlers(self) -> dict:
        """Topic -> handler, for run_service."""
        return {self.topics["scores"]: self.handle_score, self.topics["prognosis"]: self.handle_prognosis,
                self.topics["status"]: self.handle_status}

    def _malformed(self, reason: str, detail) -> None:
        self.counts["malformed"] += 1
        self.malformed_by[reason] += 1
        log.warning("%s: ignored a malformed message (%s): %s", self.asset, reason, str(detail)[:200])
        return None

    def _accept(self, payload, schema: str) -> Optional[dict]:
        try:
            doc = json.loads(payload, parse_constant=_no_constants)
        except (ValueError, TypeError) as exc:        # includes bytes that are not UTF-8
            return self._malformed("not_json", exc)
        if not isinstance(doc, dict):
            return self._malformed("not_an_object", type(doc).__name__)
        if doc.get("schema") != schema:
            return self._malformed("wrong_schema", doc.get("schema"))
        if doc.get("asset") != self.asset:
            self.counts["other_asset"] += 1
            return None
        problems = schemas.validate(schema, doc)
        if problems:
            return self._malformed("invalid", "; ".join(problems))
        return doc

    @staticmethod
    def _versions(doc: dict) -> dict:
        return {"ProfileSha256": doc["profile_sha256"]} if "profile_sha256" in doc else {}

    def handle_score(self, payload) -> list[Out]:
        doc = self._accept(payload, "score_v1")
        if doc is None:
            return []
        ts = to_utc(doc["window_end"])
        self.space.update(self.asset, {"Score": doc["score"], "Threshold": doc["threshold"], "Alert": doc["alert"],
                                       "DetectorRun": doc["detector_run_id"], "LastUpdate": ts,
                                       **self._versions(doc)}, ts)
        self.counts["scores"] += 1
        return []

    def handle_prognosis(self, payload) -> list[Out]:
        doc = self._accept(payload, "prognosis_v1")
        if doc is None:
            return []
        values = {}
        for h in self.horizons:
            hv = doc["horizons"].get(str(h)) or {}        # a horizon missing from the payload reads as no value
            values.update({f"P{h}": hv.get("p"), f"P{h}Lower": hv.get("lower"), f"P{h}Upper": hv.get("upper")})
        values.update({"SkillMeetsBar": doc["skill"]["meets_bar"], "ModelRun": doc["model_run_id"],
                       **self._versions(doc)})
        self.space.update(self.asset, values, to_utc(doc["ts"]))
        self.counts["prognoses"] += 1
        return []

    def handle_status(self, payload) -> list[Out]:
        doc = self._accept(payload, "status_v1")
        if doc is None:
            return []
        state = doc["ne107"]
        self.space.update(self.asset, {"NE107": state, "NE107Code": NE107_CODE[state],
                                       "Causes": "; ".join(doc["causes"]),
                                       "IncidentId": doc.get("incident_id") or "",
                                       "IncidentState": doc.get("incident_state") or "",
                                       **self._versions(doc)}, to_utc(doc["ts"]))
        if state != self.ne107:
            log.info("%s NE 107 %s -> %s at %s (%s)", self.asset, self.ne107, state, doc["ts"],
                     "; ".join(doc["causes"]) or "no condition holds")
            self.counts["ne107_changes"] += 1
            self.ne107 = state
        self.counts["statuses"] += 1
        return []

    def report(self) -> dict:
        return dict(self.counts, asset=self.asset, ne107=self.ne107,
                    malformed_by=dict(sorted(self.malformed_by.items())))


# --- the asyncua binding ------------------------------------------------------------------------

class ReadOnlyRuleset:
    """asyncua permission ruleset: only the request types given (ALLOWED_REQUESTS), for every
    session whatever its role, so Write and Call never reach the address space."""

    def __init__(self, allowed_ids):
        self.allowed = frozenset(int(i) for i in allowed_ids)

    def check_validity(self, user, action_type_id, body) -> bool:
        ident = getattr(action_type_id, "Identifier", action_type_id)
        ok = getattr(action_type_id, "NamespaceIndex", 0) == 0 and ident in self.allowed
        if not ok:
            log.warning("refused a request of type %s from %s", ident, getattr(user, "name", None) or "anonymous")
        return ok


def allowed_request_ids(ua) -> set[int]:
    """The binary encoding ids of ALLOWED_REQUESTS that this asyncua knows."""
    ids = set()
    for name in ALLOWED_REQUESTS:
        value = getattr(ua.ObjectIds, f"{name}_Encoding_DefaultBinary", None)
        if value is not None:
            ids.add(int(value))
    return ids


class ReadOnlyUsers:
    """asyncua user manager that never returns the Admin role.

    Anonymous sessions are accepted only in the demo mode; otherwise only the one
    username and password configured. Every accepted session is a plain user, so the
    access levels apply to it.
    """

    def __init__(self, user_cls, roles, *, anonymous: bool, username: Optional[str] = None,
                 password: Optional[str] = None):
        self._user_cls = user_cls
        self._user_role = roles.User
        self._anonymous_role = getattr(roles, "Anonymous", roles.User)
        self.anonymous = anonymous
        self.username = username
        self._password = password.encode() if password else None

    def get_user(self, iserver, username=None, password=None, certificate=None):
        if username is None:
            return self._user_cls(role=self._anonymous_role) if self.anonymous else None
        given = password.encode() if isinstance(password, str) else bytes(password or b"")
        if (self.username is not None and self._password is not None
                and hmac.compare_digest(str(username).encode(), self.username.encode())
                and hmac.compare_digest(given, self._password)):
            return self._user_cls(role=self._user_role, name=self.username)
        log.warning("refused a login as %r", username)
        return None


PLACEHOLDER = {DOUBLE: 0.0, BOOLEAN: False, INT32: 0, STRING: "", DATETIME: dt.datetime(1601, 1, 1, tzinfo=UTC)}


def _datavalue(ua, variant, *, status=None, source_ts=None, server_ts=None):
    kwargs = {"Value": variant, "SourceTimestamp": source_ts, "ServerTimestamp": server_ts}
    if status is None:
        return ua.DataValue(**kwargs)
    try:
        return ua.DataValue(StatusCode_=status, **kwargs)
    except TypeError:                                 # asyncua before 1.0 called the field StatusCode
        return ua.DataValue(StatusCode=status, **kwargs)


class OpcUaServer:
    """An asyncua server that mirrors an AddressSpace, read-only.

    ``anonymous=True`` is the demo: no security, anonymous sessions. Otherwise the endpoint
    is Basic256Sha256 SignAndEncrypt with the certificate and key given, and the one
    username and password given log in.
    """

    def __init__(self, space: AddressSpace, endpoint: str = DEFAULT_ENDPOINT, *, anonymous: bool = False,
                 cert: Optional[str] = None, key: Optional[str] = None, username: Optional[str] = None,
                 password: Optional[str] = None, name: str = "PdM-Agent northbound"):
        if not anonymous and not (cert and key and username and password):
            raise ValueError("a secure endpoint needs a certificate, a key, a username and a password; "
                             "anonymous=True is the demo setting")
        self.space = space
        self.endpoint = endpoint
        self.anonymous = anonymous
        self.cert, self.key = cert, key
        self.username, self._password = username, password
        self.name = name
        self.idx: Optional[int] = None
        self._ua = None
        self._server = None
        self._root = None
        self._nodes: dict[str, tuple] = {}            # model node id -> (asyncua Node, VariantType)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue] = None
        self._writer: Optional[asyncio.Task] = None

    async def start(self) -> None:
        from asyncua import Server, ua
        from asyncua.server.users import User, UserRole

        self._ua = ua
        users = ReadOnlyUsers(User, UserRole, anonymous=self.anonymous, username=self.username,
                              password=self._password)
        try:
            server = Server(user_manager=users)
        except TypeError:                             # a Server() without the argument
            server = Server()
        await server.init()
        server.iserver.user_manager = users
        server.allow_remote_admin(False)              # asyncua would make a login named "admin" an Admin
        server.set_endpoint(self.endpoint)
        server.set_server_name(self.name)
        ruleset = ReadOnlyRuleset(allowed_request_ids(ua))
        policy = (ua.SecurityPolicyType.NoSecurity if self.anonymous
                  else ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt)
        try:
            server.set_security_policy([policy], permission_ruleset=ruleset)
        except TypeError as exc:
            raise RuntimeError("this asyncua has no permission rulesets, so calls could not be refused; "
                               "install asyncua>=1.1") from exc
        server.set_security_IDs(["Anonymous"] if self.anonymous else ["Username"])
        if not self.anonymous:
            await server.load_certificate(self.cert)
            await server.load_private_key(self.key)
        self.idx = await server.register_namespace(NAMESPACE_URI)
        if self.idx != self.space.ns_index:
            raise RuntimeError(f"{NAMESPACE_URI} got namespace index {self.idx}; the model uses {self.space.ns_index}")
        await self._build(server)
        await self._check_read_only()
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        await self._apply([v.sample() for v in self.space.variables()])
        await server.start()
        self._server = server
        self._writer = asyncio.create_task(self._write_loop())
        self.space.listen(self._on_update)
        log.info("OPC UA server at %s, namespace %s (ns=%d), %d asset(s), %s", self.endpoint, NAMESPACE_URI,
                 self.idx, len(self.space.assets),
                 "anonymous, no security: demo only" if self.anonymous else "Basic256Sha256 SignAndEncrypt, username")

    async def _build(self, server) -> None:
        ua, idx = self._ua, self.idx

        def nid(text):
            return ua.NodeId(Identifier=text, NamespaceIndex=idx)

        def qname(text):
            return ua.QualifiedName(Name=text, NamespaceIndex=idx)

        self._root = await server.nodes.objects.add_folder(nid(ROOT_FOLDER), qname(ROOT_FOLDER))
        for asset in self.space.assets:
            folder = await self._root.add_folder(nid(asset), qname(asset))
            for var in self.space.variables(asset):
                vtype = getattr(ua.VariantType, var.data_type)
                node = await folder.add_variable(nid(f"{asset}.{var.name}"), qname(var.name),
                                                 PLACEHOLDER[var.data_type], varianttype=vtype)
                for attr, value, kind in ((ua.AttributeIds.AccessLevel, READ_ONLY, ua.VariantType.Byte),
                                          (ua.AttributeIds.UserAccessLevel, READ_ONLY, ua.VariantType.Byte),
                                          (ua.AttributeIds.WriteMask, 0, ua.VariantType.UInt32),
                                          (ua.AttributeIds.UserWriteMask, 0, ua.VariantType.UInt32)):
                    await node.write_attribute(attr, ua.DataValue(ua.Variant(value, kind)))
                try:
                    await node.write_attribute(ua.AttributeIds.Description, ua.DataValue(
                        ua.Variant(ua.LocalizedText(Text=var.description), ua.VariantType.LocalizedText)))
                except Exception:                     # descriptions help people browsing; never a reason to stop
                    log.warning("no description on %s", var.node_id, exc_info=True)
                self._nodes[var.node_id] = (node, vtype)

    async def _check_read_only(self) -> None:
        """Read the PdM nodes back: refuse to run if any can be written or called."""
        ua = self._ua
        problems = []
        for node_id, (node, _) in self._nodes.items():
            for attr, expected in ((ua.AttributeIds.AccessLevel, READ_ONLY),
                                   (ua.AttributeIds.UserAccessLevel, READ_ONLY),
                                   (ua.AttributeIds.WriteMask, 0), (ua.AttributeIds.UserWriteMask, 0)):
                got = (await node.read_attribute(attr)).Value.Value
                if got != expected:
                    problems.append(f"{node_id} {attr.name}={got}")
        stack = [self._root]
        while stack:
            for child in await stack.pop().get_children():
                kind = await child.read_node_class()
                if kind == ua.NodeClass.Method:
                    problems.append(f"method {child.nodeid.to_string()}")
                elif kind == ua.NodeClass.Object:
                    stack.append(child)
        if problems:
            raise RuntimeError("the OPC UA nodes are not read-only: " + "; ".join(problems))

    def _on_update(self, samples: list[Sample]) -> None:
        """Called from any thread (paho's in main()); the write happens on the server's loop."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._queue.put_nowait, list(samples))
        except RuntimeError:                          # the loop closed while shutting down
            pass

    async def _write_loop(self) -> None:
        while True:
            samples = await self._queue.get()
            try:
                await self._apply(samples)
            except Exception:
                log.exception("could not mirror %d value(s) into the OPC UA server", len(samples))
            finally:
                self._queue.task_done()

    async def _apply(self, samples: list[Sample]) -> None:
        ua = self._ua
        now = dt.datetime.now(UTC)
        for s in samples:
            entry = self._nodes.get(s.node_id)
            if entry is None:                         # an asset added after start is not served
                continue
            node, vtype = entry
            if s.value is None:
                value = _datavalue(ua, ua.Variant(PLACEHOLDER[s.data_type], vtype), server_ts=now,
                                   status=ua.StatusCode(ua.StatusCodes.BadWaitingForInitialData))
            else:
                value = _datavalue(ua, ua.Variant(s.value, vtype), source_ts=s.source_timestamp, server_ts=now)
            await node.write_value(value)

    async def drain(self) -> None:
        """Wait until every update handed over so far is in the server."""
        if self._queue is not None:
            await self._queue.join()

    async def stop(self) -> None:
        self.space.unlisten(self._on_update)
        if self._writer is not None:
            self._writer.cancel()
            try:
                await self._writer
            except asyncio.CancelledError:
                pass
            self._writer = None
        if self._server is not None:
            await self._server.stop()
            self._server = None


class ServerThread:
    """An OpcUaServer on its own event loop in a daemon thread (main() and the tests)."""

    def __init__(self, server: OpcUaServer, start_timeout: float = 30.0):
        self.server = server
        self.start_timeout = start_timeout
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="opcua-north", daemon=True)
        self._ready = threading.Event()
        self._error: Optional[BaseException] = None

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.server.start())
        except BaseException as exc:                  # handed to start() in the calling thread
            self._error = exc
            self._ready.set()
            self._close()
            return
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            try:
                self.loop.run_until_complete(self.server.stop())
            except Exception:
                log.exception("the OPC UA server did not stop cleanly")
            self._close()

    def _close(self) -> None:
        pending = [t for t in asyncio.all_tasks(self.loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(self.start_timeout):
            raise RuntimeError(f"the OPC UA server did not start within {self.start_timeout:.0f} s")
        if self._error is not None:
            raise self._error

    def drain(self, timeout: float = 5.0) -> None:
        asyncio.run_coroutine_threadsafe(self.server.drain(), self.loop).result(timeout)

    def stop(self, timeout: float = 10.0) -> None:
        if self._thread.is_alive():
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(timeout)


# --- command line -------------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile", action="append", required=True,
                   help="asset profile YAML; repeat for one folder per asset")
    p.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    p.add_argument("--anonymous", action="store_true",
                   help="demo only: no security, anonymous sessions (still read-only)")
    p.add_argument("--cert", help="server certificate for the Basic256Sha256 SignAndEncrypt endpoint")
    p.add_argument("--key", help="the certificate's private key (PEM)")
    p.add_argument("--broker", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        profiles = [load_profile(path) for path in args.profile]
        space = AddressSpace()
        services = [NorthService(profile, space) for profile in profiles]
    except (ProfileError, ValueError) as exc:
        raise SystemExit(f"opcua_north: {exc}")
    handlers: dict = {}
    for service in services:
        for topic, handler in service.handlers().items():
            if topic in handlers:
                raise SystemExit(f"opcua_north: two profiles use the topic {topic}")
            handlers[topic] = handler
    username, password = os.environ.get("PDM_OPCUA_USER"), os.environ.get("PDM_OPCUA_PASSWORD")
    if not args.anonymous and not (args.cert and args.key and username and password):
        raise SystemExit("opcua_north: give --cert, --key and PDM_OPCUA_USER / PDM_OPCUA_PASSWORD for the secure "
                         "endpoint, or --anonymous for the demo (no security, still read-only)")
    try:
        import asyncua  # noqa: F401
    except ImportError:
        raise SystemExit("opcua_north: asyncua is not installed: pip install 'asyncua>=1.1'")
    runner = ServerThread(OpcUaServer(space, args.endpoint, anonymous=args.anonymous, cert=args.cert, key=args.key,
                                      username=username, password=password))
    try:
        runner.start()
    except Exception as exc:
        raise SystemExit(f"opcua_north: the OPC UA server did not start: {exc}")
    log.info("serving %s from %s", ", ".join(space.assets), ", ".join(handlers))
    try:
        run_service("opcua_north", handlers, host=args.broker, port=args.port)
    finally:
        runner.stop()
    print(json.dumps({s.asset: s.report() for s in services}, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
