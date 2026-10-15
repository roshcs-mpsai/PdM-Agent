"""Northbound OPC UA server (T21; INT-01 to INT-03).

The model tests run anywhere: score, prognosis and status payloads are built by
hand, checked against their schemas, and the address space is held to them.
The server test starts the asyncua server on a free port, browses, reads,
subscribes and tries to write; it needs asyncua and is skipped without it.
"""
import asyncio
import datetime as dt
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))

from opcua_north import server as north  # noqa: E402
from pdm_common import schemas  # noqa: E402
from pdm_common.hashing import canonical_json  # noqa: E402
from pdm_common.profile import ProfileError, load_profile  # noqa: E402
from pdm_common.status import NE107_STATES  # noqa: E402

PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
SECOND_PATH = ROOT / "tests" / "fixtures" / "minimal_asset_profile.yaml"
T0 = dt.datetime(2020, 7, 15, tzinfo=dt.timezone.utc)
SHA = "a" * 64

EXPECTED = {
    "Score": "Double", "Threshold": "Double", "Alert": "Boolean",
    "P24": "Double", "P24Lower": "Double", "P24Upper": "Double",
    "P72": "Double", "P72Lower": "Double", "P72Upper": "Double",
    "P168": "Double", "P168Lower": "Double", "P168Upper": "Double",
    "SkillMeetsBar": "Boolean", "NE107": "String", "NE107Code": "Int32", "Causes": "String",
    "IncidentId": "String", "IncidentState": "String", "DetectorRun": "String", "ModelRun": "String",
    "ProfileSha256": "String", "LastUpdate": "DateTime",
}
CODES = {"good": 0, "maintenance_required": 1, "out_of_specification": 2, "function_check": 3, "failure": 4}
PENDING = "incident open, diagnosis pending"


@pytest.fixture(scope="module")
def profile():
    return load_profile(PROFILE_PATH)


@pytest.fixture()
def service(profile):
    return north.NorthService(profile)


def z(t):
    return t.isoformat().replace("+00:00", "Z")


def score_doc(asset, minute=10, score=3.5, threshold=4.0):
    end = T0 + dt.timedelta(minutes=minute)
    return {"schema": "score_v1", "asset": asset, "window_start": z(end - dt.timedelta(minutes=10)),
            "window_end": z(end), "score": score, "threshold": threshold, "alert": score > threshold,
            "errors": {"feature_a": score}, "detector_run_id": "bundle-0123456789ab", "profile_sha256": SHA}


def prognosis_doc(asset, hour=1, horizons=None, meets_bar=False):
    return {"schema": "prognosis_v1", "asset": asset, "ts": z(T0 + dt.timedelta(hours=hour)),
            "horizons": horizons or {"24": {"p": 0.04, "lower": 0.01, "upper": 0.09},
                                     "72": {"p": 0.11, "lower": 0.05, "upper": 0.21},
                                     "168": {"p": 0.23, "lower": 0.12, "upper": 0.38}},
            "interval": 0.9, "inputs": {"score_max_24h": 4.2, "load_share_6h": None},
            "skill": {"brier_skill_by_horizon": {"skill_24": -0.03}, "mean": -0.08, "bar": 0.15,
                      "meets_bar": meets_bar},
            "model": "logistic", "model_run_id": "bundle-fedcba987654", "profile_sha256": SHA}


def status_doc(asset, state="good", causes=(), incident_id=None, incident_state=None, minute=10):
    return {"schema": "status_v1", "asset": asset, "ts": z(T0 + dt.timedelta(minutes=minute)), "ne107": state,
            "causes": list(causes), "score": 3.5, "threshold": 4.0, "alert": False,
            "detector_run_id": "bundle-0123456789ab", "incident_id": incident_id, "incident_state": incident_state,
            "episode_open": incident_id is not None, "prognosis": None, "profile_sha256": SHA}


def payload(doc) -> str:
    """Canonical JSON of a document that is valid against its own schema."""
    assert schemas.validate(doc["schema"], doc) == [], doc
    return canonical_json(doc)


def node(service, name):
    return service.space.node(f"ns=2;s={service.asset}.{name}")


def values(service):
    return {v.name: v.value for v in service.space.variables(service.asset)}


# --- the address space -------------------------------------------------------------------------

def test_every_variable_is_present_for_every_asset(profile):
    space = north.AddressSpace()
    first = north.NorthService(profile, space)
    second = north.NorthService(load_profile(SECOND_PATH), space)
    assert space.assets == [first.asset, second.asset]
    for asset in space.assets:
        variables = space.variables(asset)
        assert {v.name: v.data_type for v in variables} == EXPECTED
        for v in variables:
            assert v.node_id == f"ns=2;s={asset}.{v.name}" and v.browse_name == v.name
            assert v.writable is False and v.value is None and v.source_timestamp is None
            assert v.description and v.source
            assert space.node(v.node_id) is v
    assert space.methods == ()
    assert space.namespace_uri == "urn:pdm-agent" and space.root == "PdM"


def test_an_asset_appears_once(profile):
    space = north.AddressSpace()
    north.NorthService(profile, space)
    with pytest.raises(ValueError, match="already"):
        north.NorthService(profile, space)


def test_horizons_come_from_the_profile(profile):
    assert north.NorthService(profile).horizons == (24, 72, 168)
    assert north.NorthService(load_profile(SECOND_PATH)).horizons == (24, 72, 168)      # none declared
    other = dict(profile, prognosis={"horizons": ["12h", "2d"]})
    names = {v.name for v in north.NorthService(other).space.variables()}
    assert {"P12", "P12Lower", "P12Upper", "P48", "P48Lower", "P48Upper"} <= names and "P24" not in names
    with pytest.raises(ProfileError, match="whole number of hours"):
        north.NorthService(dict(profile, prognosis={"horizons": ["90min"]}))


def test_handlers_follow_the_profile_topics(service, profile):
    topics = profile["topics"]
    assert service.handlers() == {topics["scores"]: service.handle_score,
                                  topics["prognosis"]: service.handle_prognosis,
                                  topics["status"]: service.handle_status}


# --- values come from the payloads -------------------------------------------------------------

def test_a_score_sets_score_threshold_alert_and_versions(service):
    assert service.handle_score(payload(score_doc(service.asset, minute=10, score=3.5))) == []
    end = T0 + dt.timedelta(minutes=10)
    v = values(service)
    assert (v["Score"], v["Threshold"], v["Alert"]) == (3.5, 4.0, False)
    assert v["LastUpdate"] == end and v["DetectorRun"] == "bundle-0123456789ab" and v["ProfileSha256"] == SHA
    assert node(service, "Score").source_timestamp == end
    service.handle_score(payload(score_doc(service.asset, minute=11, score=6)))     # an integer score
    assert values(service)["Score"] == 6.0 and isinstance(values(service)["Score"], float)
    assert values(service)["Alert"] is True and values(service)["LastUpdate"] == end + dt.timedelta(minutes=1)
    assert values(service)["P24"] is None and values(service)["NE107"] is None    # other payloads, not set yet
    assert service.counts["scores"] == 2


def test_a_prognosis_sets_the_probabilities_and_their_bounds(service):
    service.handle_prognosis(payload(prognosis_doc(service.asset, hour=3)))
    v = values(service)
    assert (v["P24"], v["P24Lower"], v["P24Upper"]) == (0.04, 0.01, 0.09)
    assert (v["P72"], v["P72Lower"], v["P72Upper"]) == (0.11, 0.05, 0.21)
    assert (v["P168"], v["P168Lower"], v["P168Upper"]) == (0.23, 0.12, 0.38)
    assert v["SkillMeetsBar"] is False and v["ModelRun"] == "bundle-fedcba987654"
    assert node(service, "P72Upper").source_timestamp == T0 + dt.timedelta(hours=3)
    assert v["Score"] is None


def test_a_horizon_missing_from_a_prognosis_reads_as_no_value(service):
    service.handle_prognosis(payload(prognosis_doc(service.asset)))
    service.handle_prognosis(payload(prognosis_doc(service.asset, hour=2, meets_bar=True,
                                                   horizons={"24": {"p": 0.5, "lower": 0.4, "upper": 0.6}})))
    v = values(service)
    assert v["P24"] == 0.5 and v["P72"] is None and v["P168Upper"] is None and v["SkillMeetsBar"] is True


@pytest.mark.parametrize("state", list(CODES))
def test_ne107_status_and_its_code(service, state):
    causes = [] if state == "good" else [f"cause of {state}", "a second cause"]
    service.handle_status(payload(status_doc(service.asset, state, causes)))
    v = values(service)
    assert v["NE107"] == state and v["NE107Code"] == CODES[state]
    assert v["Causes"] == "; ".join(causes)
    assert node(service, "NE107Code").source_timestamp == T0 + dt.timedelta(minutes=10)


def test_ne107_codes_rise_with_the_status_precedence():
    """Higher code, more severe: the order pdm_common.status uses when several conditions hold."""
    assert north.NE107_CODE == CODES
    assert sorted(CODES, key=CODES.get, reverse=True) == list(NE107_STATES)
    assert set(CODES) == set(schemas.validator("status_v1").schema["properties"]["ne107"]["enum"])


def test_status_carries_the_incident_and_logs_transitions(service):
    service.handle_status(payload(status_doc(service.asset, "maintenance_required", [PENDING],
                                             "INC-x-20200715T0010Z", "diagnosing")))
    v = values(service)
    assert (v["IncidentId"], v["IncidentState"]) == ("INC-x-20200715T0010Z", "diagnosing")
    service.handle_status(payload(status_doc(service.asset, "maintenance_required", ["still open"],
                                             "INC-x-20200715T0010Z", "awaiting_review", minute=11)))
    service.handle_status(payload(status_doc(service.asset, "good", minute=12)))
    v = values(service)
    assert (v["IncidentId"], v["IncidentState"], v["Causes"]) == ("", "", "")       # no incident: empty, not missing
    assert service.counts["statuses"] == 3 and service.counts["ne107_changes"] == 2 and service.ne107 == "good"


def test_listeners_see_every_update_in_order(service):
    seen = []
    service.space.listen(seen.append)
    service.handle_score(payload(score_doc(service.asset)))
    service.handle_status(payload(status_doc(service.asset, "failure", ["a cause"])))
    assert [s.name for s in seen[0]] == ["Score", "Threshold", "Alert", "DetectorRun", "LastUpdate", "ProfileSha256"]
    assert {s.name: s.value for s in seen[1]}["NE107Code"] == 4
    assert all(s.node_id == f"ns=2;s={service.asset}.{s.name}" for batch in seen for s in batch)


def test_a_failing_listener_does_not_lose_the_update(service):
    def broken(samples):
        raise RuntimeError("mirror down")
    service.space.listen(broken)
    service.handle_score(payload(score_doc(service.asset)))
    assert values(service)["Score"] == 3.5


def test_values_are_held_to_their_types(service):
    with pytest.raises(TypeError):
        service.space.update(service.asset, {"Score": "high"}, T0)
    with pytest.raises(TypeError):
        service.space.update(service.asset, {"Alert": 1}, T0)
    with pytest.raises(TypeError):
        service.space.update(service.asset, {"NE107Code": 2**31}, T0)
    with pytest.raises(KeyError):
        service.space.update(service.asset, {"Pressure": 1.0}, T0)


# --- read-only ----------------------------------------------------------------------------------

def test_client_writes_and_method_calls_are_refused(service):
    service.handle_score(payload(score_doc(service.asset)))
    for v in service.space.variables():
        before = v.value
        with pytest.raises(north.AccessDenied) as refused:
            service.space.write(v.node_id, 99.0)
        assert refused.value.status == "BadUserAccessDenied"
        assert v.value == before
    with pytest.raises(north.AccessDenied):
        service.space.write("ns=2;s=nowhere.Score", 1.0)
    with pytest.raises(north.AccessDenied):
        service.space.call(f"ns=2;s={service.asset}", f"ns=2;s={service.asset}.Reset")
    assert values(service)["Score"] == 3.5


class FakeNodeId:
    def __init__(self, identifier, namespace=0):
        self.Identifier, self.NamespaceIndex = identifier, namespace


def test_the_ruleset_passes_only_the_listed_requests():
    read, browse, write, call = 631, 527, 673, 712          # binary encoding ids from the OPC UA nodeset
    rules = north.ReadOnlyRuleset({read, browse})
    assert rules.check_validity(None, FakeNodeId(read), None) is True
    assert rules.check_validity(None, FakeNodeId(browse), None) is True
    assert rules.check_validity(None, FakeNodeId(write), None) is False
    assert rules.check_validity(None, FakeNodeId(call), None) is False
    assert rules.check_validity(None, FakeNodeId(read, 2), None) is False      # same number, another namespace
    assert rules.check_validity(None, read, None) is True                      # a plain id
    forbidden = {"WriteRequest", "HistoryUpdateRequest", "CallRequest", "AddNodesRequest", "AddReferencesRequest",
                 "DeleteNodesRequest", "DeleteReferencesRequest", "RegisterServerRequest", "RegisterServer2Request",
                 "QueryFirstRequest", "QueryNextRequest"}
    assert not forbidden & set(north.ALLOWED_REQUESTS)


class FakeRoles:
    Admin, Anonymous, User = "admin", "anonymous", "user"


class FakeUser:
    def __init__(self, role, name=None):
        self.role, self.name = role, name


def test_no_login_becomes_an_admin():
    demo = north.ReadOnlyUsers(FakeUser, FakeRoles, anonymous=True)
    assert demo.get_user(None).role == FakeRoles.Anonymous
    assert demo.get_user(None, username="admin", password="admin") is None
    secure = north.ReadOnlyUsers(FakeUser, FakeRoles, anonymous=False, username="scada", password="s3cret")
    assert secure.get_user(None) is None
    assert secure.get_user(None, username="scada", password="wrong") is None
    assert secure.get_user(None, username="admin", password="s3cret") is None
    user = secure.get_user(None, username="scada", password="s3cret")
    assert user.role == FakeRoles.User and user.name == "scada"
    assert secure.get_user(None, username="scada", password=b"s3cret").role == FakeRoles.User


def test_a_secure_server_needs_its_credentials():
    with pytest.raises(ValueError, match="anonymous"):
        north.OpcUaServer(north.AddressSpace(), cert="cert.der", key="key.pem")
    north.OpcUaServer(north.AddressSpace(), anonymous=True)                 # the demo needs none


# --- what the service ignores -------------------------------------------------------------------

def test_other_assets_are_ignored_and_counted(service):
    other = "demo_pump"
    assert service.handle_score(payload(score_doc(other))) == []
    assert service.handle_prognosis(payload(prognosis_doc(other))) == []
    assert service.handle_status(payload(status_doc(other, "failure"))) == []
    assert service.counts["other_asset"] == 3 and service.counts["malformed"] == 0
    assert all(value is None for value in values(service).values())


def _broken(asset):
    score = score_doc(asset)
    no_errors = {k: v for k, v in score.items() if k != "errors"}
    return [
        ("handle_score", "not_json", b"{not json"),
        ("handle_score", "not_json", b"\xff\xfe\xfa"),
        ("handle_score", "not_json", canonical_json(score).replace("3.5", "NaN", 1)),
        ("handle_score", "not_json", None),
        ("handle_score", "not_an_object", b"[1, 2]"),
        ("handle_score", "wrong_schema", canonical_json(dict(score, schema="window_v1"))),
        ("handle_score", "invalid", canonical_json(no_errors)),
        ("handle_score", "invalid", canonical_json(dict(score, score="high"))),
        ("handle_score", "invalid", canonical_json(dict(score, window_end="15 Jul 2020"))),
        ("handle_prognosis", "invalid",
         canonical_json(dict(prognosis_doc(asset), horizons={"24": {"p": 1.4, "lower": 0.1, "upper": 0.2}}))),
        ("handle_prognosis", "wrong_schema", canonical_json(score)),
        ("handle_status", "invalid", canonical_json(dict(status_doc(asset), ne107="broken"))),
        ("handle_status", "invalid", canonical_json(dict(status_doc(asset), extra=1))),
    ]


@pytest.mark.parametrize("case", range(len(_broken("x"))))
def test_malformed_payloads_are_ignored_and_counted(service, case):
    handler, reason, raw = _broken(service.asset)[case]
    assert getattr(service, handler)(raw) == []
    assert service.counts["malformed"] == 1 and service.malformed_by == {reason: 1}
    assert all(value is None for value in values(service).values())
    assert service.report()["malformed_by"] == {reason: 1}


# --- the asyncua server -------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def wait_for(condition, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.05)


def test_the_asyncua_server_serves_read_only_nodes(profile):
    """INT-01, INT-02: browse, read and subscribe work; write, call and node management are refused."""
    pytest.importorskip("asyncua")
    space = north.AddressSpace()
    service = north.NorthService(profile, space)
    service.handle_score(payload(score_doc(service.asset, score=3.5)))
    service.handle_status(payload(status_doc(service.asset, "maintenance_required", [PENDING],
                                             "INC-x-20200715T0010Z", "open")))
    url = f"opc.tcp://127.0.0.1:{free_port()}/pdm/"
    runner = north.ServerThread(north.OpcUaServer(space, url, anonymous=True))
    runner.start()
    try:
        asyncio.run(_client_checks(url, service))
    finally:
        runner.stop()


async def _client_checks(url, service):
    from asyncua import Client, ua

    async with Client(url=url, timeout=10) as client:
        idx = await client.get_namespace_index(north.NAMESPACE_URI)
        folder = await client.nodes.objects.get_child([f"{idx}:{north.ROOT_FOLDER}", f"{idx}:{service.asset}"])
        children = await folder.get_children()
        assert {(await c.read_browse_name()).Name for c in children} == set(EXPECTED)
        for child in children:
            assert await child.read_node_class() == ua.NodeClass.Variable
            for attr in (ua.AttributeIds.AccessLevel, ua.AttributeIds.UserAccessLevel):
                assert (await child.read_attribute(attr)).Value.Value == north.READ_ONLY

        score = await folder.get_child(f"{idx}:Score")
        assert score.nodeid.to_string() == f"ns={idx};s={service.asset}.Score"
        assert await score.read_value() == pytest.approx(3.5)
        assert await (await folder.get_child(f"{idx}:NE107")).read_value() == "maintenance_required"
        assert await (await folder.get_child(f"{idx}:NE107Code")).read_value() == 1
        assert await (await folder.get_child(f"{idx}:IncidentState")).read_value() == "open"
        with pytest.raises(ua.uaerrors.BadWaitingForInitialData):          # no prognosis yet
            await (await folder.get_child(f"{idx}:P24")).read_value()

        received = []

        class Handler:
            def datachange_notification(self, node, val, data):
                received.append(val)

        subscription = await client.create_subscription(50, Handler())
        await subscription.subscribe_data_change(score)
        await wait_for(lambda: received)
        # an update from this thread, off the server's loop, as paho's thread would make it
        service.handle_score(payload(score_doc(service.asset, minute=11, score=7.25)))
        await wait_for(lambda: 7.25 in received)

        with pytest.raises((ua.uaerrors.BadUserAccessDenied, ua.uaerrors.BadNotWritable)):
            await score.write_value(ua.DataValue(ua.Variant(1.0, ua.VariantType.Double)))
        with pytest.raises(ua.uaerrors.BadUserAccessDenied):
            await client.nodes.server.call_method(ua.NodeId(ua.ObjectIds.Server_GetMonitoredItems),
                                                  ua.Variant(1, ua.VariantType.UInt32))
        with pytest.raises(ua.uaerrors.BadUserAccessDenied):
            await folder.call_method(ua.NodeId(Identifier=f"{service.asset}.Reset", NamespaceIndex=idx))
        with pytest.raises(ua.uaerrors.BadUserAccessDenied):
            await folder.add_variable(idx, "Injected", 1.0)
        with pytest.raises(ua.uaerrors.BadUserAccessDenied):
            await score.delete()
        assert await score.read_value() == pytest.approx(7.25)
        await subscription.delete()

    admin = Client(url=url, timeout=5)
    admin.set_user("admin")
    admin.set_password("admin")
    with pytest.raises(Exception):                   # the demo endpoint takes anonymous sessions only
        async with admin:
            pass
