"""Broker hardening (T2, build guide R3.1; NFR-06): the secure Mosquitto config, the ACL,
the password file entries, the certificates and the client settings every service uses.

No broker runs here (no mosquitto binary, no Docker). Instead:

* the ACL is read by a small evaluator with Mosquitto's rules (deny lines first, then
  grants; + and # as in subscriptions; a topic starting with $ is not matched by a pattern
  starting with a wildcard) and checked topic by topic against what each service needs;
* the $7$ entries of tools/broker_users.py are re-derived here with hashlib;
* tools/make_certs.sh runs where openssl is installed, and Python's ssl client verifies the
  broker certificate in a real TLS handshake, as paho's tls_set would;
* run_service and read_retained run against a fake paho module.
"""
import base64
import hashlib
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from pdm_common import broker
from pdm_common.profile import load_profile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import broker_users  # noqa: E402

ACL = ROOT / "mosquitto" / "config" / "acl"
SECURE_CONF = ROOT / "mosquitto" / "config" / "mosquitto.secure.conf"
SECURITY_MD = ROOT / "docs" / "SECURITY.md"
PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
MAKE_CERTS = ROOT / "tools" / "make_certs.sh"
PW = "pw-" + "for-tests"           # assembled, so no line of this file looks like NAME=secret to the scan

# What each account reads and writes, by kind of topic (the T2 table; docs/SECURITY.md).
NEEDS = {
    "replayer": ((), ("telemetry",)),
    "south": (("sparkplug",), ("telemetry",)),
    "scada_sim": ((), ("sparkplug",)),
    "ingest": (("telemetry",), ("windows",)),
    "detector": (("windows",), ("scores",)),
    "prognostics": (("windows", "scores"), ("prognosis",)),
    "gate": (("scores", "windows", "prognosis", "drift"), ("incidents", "status")),
    "drift": (("telemetry",), ("drift",)),
    "agent": (("incidents",), ()),
    "opcua_north": (("scores", "prognosis", "status"), ()),
    "south-cip": ((), ("device",)),
    "mcp_server": (("device",), ()),
    "dashboard": (("status",), ()),
    "healthcheck": (("sys_version",), ()),
}
PROFILE_KINDS = ("telemetry", "windows", "scores", "prognosis", "incidents", "status", "device")
DRIFT_TOPIC = "pdm/_sys/drift"


# --- a Mosquitto ACL evaluator --------------------------------------------------------------

def valid_filter(topic: str) -> bool:
    """A topic filter: '+' only as a whole level, '#' only as the whole last level, and no
    blanks (none of our topics has one; a blank here means a trailing comment)."""
    if not topic or " " in topic or "\t" in topic:
        return False
    levels = topic.split("/")
    for i, level in enumerate(levels):
        if "+" in level and level != "+":
            return False
        if "#" in level and (level != "#" or i != len(levels) - 1):
            return False
    return True


def parse_acl(text: str) -> tuple[list, dict]:
    """(rules before the first user line, account -> [(access, pattern)]), refusing what
    Mosquitto would misread here: blanks around a line, trailing comments, pattern lines."""
    anonymous, users, current = [], {}, None
    for n, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.startswith("#"):
            continue
        assert raw == raw.strip(), f"acl line {n}: blanks at the start or end"
        word, _, rest = raw.partition(" ")
        if word == "user":
            assert rest and rest not in users, f"acl line {n}"
            current = users[rest] = []
        elif word == "topic":
            access, _, topic = rest.partition(" ")
            if access not in ("read", "write", "readwrite", "deny"):
                access, topic = "readwrite", rest          # Mosquitto: no access word means readwrite
            assert valid_filter(topic), f"acl line {n}: {topic!r} is not a topic filter (a trailing comment?)"
            (anonymous if current is None else current).append((access, topic))
        else:
            raise AssertionError(f"acl line {n}: {word!r} lines are not used here (pattern applies to everyone)")
    return anonymous, users


def matches(pattern: str, topic: str) -> bool:
    """MQTT topic filter matching (MQTT 3.1.1 section 4.7)."""
    if topic.startswith("$") and pattern[:1] in ("+", "#"):
        return False
    p, t = pattern.split("/"), topic.split("/")
    for i, level in enumerate(p):
        if level == "#":
            return True
        if i >= len(t) or (level != "+" and level != t[i]):
            return False
    return len(p) == len(t)


def allowed(rules: list, topic: str, access: str) -> bool:
    """Mosquitto's acl_file check for one account: deny lines win, then any grant."""
    if any(a == "deny" and matches(p, topic) for a, p in rules):
        return False
    grants = {"read": ("read", "readwrite"), "write": ("write", "readwrite")}[access]
    return any(a in grants and matches(p, topic) for a, p in rules)


@pytest.fixture(scope="module")
def acl():
    return parse_acl(ACL.read_text())


@pytest.fixture(scope="module")
def topics():
    """Concrete topics of every kind, from the profile's topic tree (plus a second asset)."""
    profile = load_profile(PROFILE_PATH)
    group = profile["south"]["sparkplug"]["group"]
    edge, device = profile["south"]["sparkplug"]["edge_node"], profile["south"]["sparkplug"]["device"]
    kinds = {k: [profile["topics"][k], profile["topics"][k].replace(profile["asset"]["id"], "second_asset")]
             for k in PROFILE_KINDS}
    kinds["drift"] = [DRIFT_TOPIC]
    kinds["sparkplug"] = [f"spBv1.0/{group}/{t}/{edge}" for t in ("NBIRTH", "NDATA", "NDEATH", "NCMD")]
    kinds["sparkplug"] += [f"spBv1.0/{group}/{t}/{edge}/{device}" for t in ("DBIRTH", "DDATA", "DDEATH", "DCMD")]
    kinds["sparkplug"] += ["spBv1.0/STATE/pdm-agent", "spBv1.0/other_group/DDATA/edge/dev", "spBv1.0"]
    kinds["sys_version"] = ["$SYS/broker/version"]
    kinds["nobody"] = ["$SYS/broker/uptime", "$SYS/broker/clients/connected", "pdm/second_asset/unknown",
                       "pdm/_sys/other", "pdm", "other/topic"]
    return kinds


def test_the_acl_has_no_rules_for_clients_without_an_account(acl):
    anonymous, _ = acl
    assert anonymous == []


def test_each_account_can_do_exactly_what_its_service_needs(acl, topics):
    """The full matrix: every account, every kind of topic, read and write."""
    _, users = acl
    assert set(users) == set(NEEDS)
    for user, (reads, writes) in NEEDS.items():
        for kind, instances in topics.items():
            for topic in instances:
                assert allowed(users[user], topic, "read") == (kind in reads), (user, "read", topic)
                assert allowed(users[user], topic, "write") == (kind in writes), (user, "write", topic)


def test_ingest_cannot_publish_scores(acl, topics):
    """The build guide's R3.1 check."""
    _, users = acl
    assert allowed(users["ingest"], topics["telemetry"][0], "read")
    assert not allowed(users["ingest"], topics["scores"][0], "write")


def test_the_south_account_has_no_write_path_to_the_plant(acl, topics):
    """NFR-06 / ING-15: the adapters read Sparkplug and write telemetry, nothing else."""
    _, users = acl
    south = users["south"]
    for topic in topics["sparkplug"]:
        assert allowed(south, topic, "read")
        assert not allowed(south, topic, "write"), topic
    every = [t for instances in topics.values() for t in instances]
    assert {t for t in every if allowed(south, t, "write")} == set(topics["telemetry"])
    only_plant = [u for u, rules in users.items() if any(allowed(rules, t, "write") for t in topics["sparkplug"])]
    assert only_plant == ["scada_sim"]


def test_the_evaluator_matches_like_mosquitto():
    assert matches("pdm/+/telemetry", "pdm/a1/telemetry")
    assert not matches("pdm/+/telemetry", "pdm/a1/b/telemetry")
    assert matches("spBv1.0/#", "spBv1.0") and matches("spBv1.0/#", "spBv1.0/g/DDATA/e/d")
    assert not matches("#", "$SYS/broker/version") and not matches("+/broker/version", "$SYS/broker/version")
    assert matches("$SYS/broker/version", "$SYS/broker/version")
    rules = [("readwrite", "pdm/#"), ("deny", "pdm/+/status")]
    assert allowed(rules, "pdm/a1/scores", "write") and not allowed(rules, "pdm/a1/status", "read")
    assert valid_filter("spBv1.0/#") and valid_filter("pdm/+/status") and valid_filter("$SYS/broker/version")
    assert not any(valid_filter(t) for t in ("pdm/a+/x", "pdm/#/x", "pdm/x#", "", "pdm/+/status #"))
    with pytest.raises(AssertionError, match="not a topic filter"):
        parse_acl("user x\ntopic read pdm/+/status # who reads it")


def test_every_run_service_name_has_an_account(acl):
    """A service started through run_service logs in as its own name (pdm_common.mqtt._client)."""
    _, users = acl
    names = set()
    for py in (ROOT / "services").rglob("*.py"):
        names |= set(re.findall(r"run_service\(\s*\"([^\"]+)\"", py.read_text()))
    assert names, "no run_service calls found"
    assert names <= set(users), sorted(names - set(users))


def test_security_md_lists_every_account(acl):
    _, users = acl
    text = SECURITY_MD.read_text()
    assert [u for u in users if f"`{u}`" not in text] == []


# --- the secure broker config ------------------------------------------------------------------

LISTENER_OPTIONS = {"cafile", "capath", "certfile", "keyfile", "tls_version", "require_certificate", "ciphers",
                    "protocol", "bind_interface", "mount_point", "use_identity_as_username"}


def parse_conf(text: str) -> tuple[dict, list]:
    """(global option -> values, listeners as dicts) of a mosquitto.conf."""
    options, listeners = {}, []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition(" ")
        if name == "listener":
            listeners.append({"listener": value.strip()})
        elif name in LISTENER_OPTIONS:
            assert listeners, f"{name} before any listener"
            listeners[-1][name] = value.strip()
        else:
            options.setdefault(name, []).append(value.strip())
    return options, listeners


def test_the_secure_config_refuses_anonymous_clients():
    options, _ = parse_conf(SECURE_CONF.read_text())
    assert options["allow_anonymous"] == ["false"]
    assert "listener_allow_anonymous" not in options
    assert options.get("per_listener_settings", ["false"]) == ["false"]      # the three apply everywhere
    assert options["password_file"] == ["/mosquitto/config/passwd"]
    assert options["acl_file"] == ["/mosquitto/config/acl"]


def test_the_secure_config_has_tls_on_8883_and_plain_1883_inside_the_container():
    options, listeners = parse_conf(SECURE_CONF.read_text())
    by_port = {lst["listener"]: lst for lst in listeners}
    assert set(by_port) == {"8883", "1883"}
    tls = by_port["8883"]
    assert tls["cafile"] == "/mosquitto/certs/ca.crt"
    assert tls["certfile"] == "/mosquitto/certs/server.crt" and tls["keyfile"] == "/mosquitto/certs/server.key"
    assert tls["tls_version"] in ("tlsv1.2", "tlsv1.3")
    assert set(by_port["1883"]) == {"listener"}
    assert options["persistence"] == ["true"] and options["persistence_location"] == ["/mosquitto/data/"]


def test_the_development_config_is_still_the_r0_one():
    options, listeners = parse_conf((ROOT / "mosquitto" / "config" / "mosquitto.conf").read_text())
    assert options["allow_anonymous"] == ["true"] and [lst["listener"] for lst in listeners] == ["1883"]


# --- the password file ($7$, PBKDF2-SHA512) ----------------------------------------------------

def rederive(password: str, entry: str) -> bool:
    """Mosquitto's check, written out again: $7$<iterations>$<b64 salt>$<b64 PBKDF2-HMAC-SHA512>."""
    kind, iterations, salt, digest = entry.split("$")[1:]
    assert kind == "7"
    salt, digest = base64.b64decode(salt, validate=True), base64.b64decode(digest, validate=True)
    assert len(salt) == 12 and len(digest) == 64
    return hashlib.pbkdf2_hmac("sha512", password.encode(), salt, int(iterations), 64) == digest


def test_entries_have_the_mosquitto_passwd_format():
    entry = broker_users.hash_password(PW)
    assert re.fullmatch(r"\$7\$1000\$[A-Za-z0-9+/]{16}\$[A-Za-z0-9+/]{86}==", entry)
    assert rederive(PW, entry) and not rederive(PW + "x", entry)
    salt = bytes(range(12))
    expected = "$7$101$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(
        hashlib.pbkdf2_hmac("sha512", PW.encode(), salt, 101, 64)).decode()
    assert broker_users.hash_password(PW, 101, salt) == expected
    assert broker_users.hash_password(PW) != broker_users.hash_password(PW)           # a new salt each time


def test_verify_accepts_both_salt_sizes_and_rejects_bad_entries():
    long_salt = bytes(64)
    digest = hashlib.pbkdf2_hmac("sha512", PW.encode(), long_salt, 1000, 64)
    v21 = f"$7$1000${base64.b64encode(long_salt).decode()}${base64.b64encode(digest).decode()}"
    assert broker_users.verify(PW, v21)                                              # Mosquitto 2.1's salts
    good = broker_users.hash_password(PW)
    assert broker_users.verify(PW, good) and not broker_users.verify("other", good)
    for bad in ("", "$6$abc$def", good.replace("$7$1000$", "$7$0$"), good[:-4] + "$",
                "$7$1000$" + base64.b64encode(bytes(8)).decode() + "$" + good.split("$")[-1]):
        assert broker_users.parse_hash(bad) is None and not broker_users.verify(PW, bad)


def run_users(*args, env=None, passwd=None, capsys=None):
    argv = ["--env", str(env), "--passwd", str(passwd), *args]
    code = broker_users.main(argv)
    out = capsys.readouterr() if capsys is not None else None
    return code, out


def test_broker_users_generates_passwords_and_a_passwd_that_verifies(tmp_path, capsys):
    env, passwd = tmp_path / ".env", tmp_path / "passwd"
    env.write_text("PG_PASSWORD=change-me\n")
    code, out = run_users(env=env, passwd=passwd, capsys=capsys)
    assert code == 0
    users = broker_users.acl_users(ACL)
    _, values = broker_users.read_env(env)
    entries = broker_users.read_passwd(passwd)
    assert list(entries) == users
    for user in users:
        key = broker.env_key(user)
        password = values[f"PDM_MQTT_PASSWORD_{key}"]
        assert values[f"PDM_MQTT_USER_{key}"] == user and len(password) >= 40
        assert rederive(password, entries[user]), user
        assert password not in out.out + out.err                                   # never printed
    assert values["PG_PASSWORD"] == "change-me"                                      # the rest of .env is kept
    assert env.stat().st_mode & 0o777 == 0o600 and passwd.stat().st_mode & 0o777 == 0o600
    assert len(set(entries.values())) == len(users)

    before = (env.read_bytes(), passwd.read_bytes())
    assert run_users(env=env, passwd=passwd, capsys=capsys)[0] == 0
    assert (env.read_bytes(), passwd.read_bytes()) == before                         # a second run changes nothing
    code, out = run_users("--check", env=env, passwd=passwd, capsys=capsys)
    assert code == 0 and out.out.startswith("ok:")


def test_check_reports_missing_mismatched_and_extra_entries(tmp_path, capsys):
    env, passwd = tmp_path / ".env", tmp_path / "passwd"
    assert run_users(env=env, passwd=passwd, capsys=capsys)[0] == 0
    lines = passwd.read_text().splitlines()
    kept = [line for line in lines if not line.startswith("gate:")]
    kept.append("stranger:" + broker_users.hash_password(PW))
    detector = next(i for i, line in enumerate(kept) if line.startswith("detector:"))
    kept[detector] = "detector:" + broker_users.hash_password(PW)                    # not the password in .env
    passwd.write_text("\n".join(kept) + "\n")
    code, out = run_users("--check", env=env, passwd=passwd, capsys=capsys)
    assert code == 1
    assert "gate: no entry" in out.out and "stranger: has a password entry but no 'user' line" in out.out
    assert "detector: the entry" in out.out and "does not match PDM_MQTT_PASSWORD_DETECTOR" in out.out
    assert run_users("--check", env=env, passwd=tmp_path / "none", capsys=capsys)[0] == 1


def test_a_weak_or_mismatched_env_entry_stops_the_tool(tmp_path, capsys):
    env, passwd = tmp_path / ".env", tmp_path / "passwd"
    weak = "short$1"
    env.write_text(f"PDM_MQTT_PASSWORD_GATE={weak}\n")
    code, out = run_users(env=env, passwd=passwd, capsys=capsys)
    assert code == 2 and "PDM_MQTT_PASSWORD_GATE needs at least 16 characters" in out.err
    assert weak not in out.err + out.out and not passwd.exists()
    env.write_text("PDM_MQTT_USER_GATE=someone_else\n")
    code, out = run_users(env=env, passwd=passwd, capsys=capsys)
    assert code == 2 and "the ACL account is 'gate'" in out.err


def test_env_reading_follows_compose(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nexport A=1\nB='two words'\nC=\"x#y\"\nD=plain # note\nE=\nF=\"q\" # note\nG=a=b\n")
    _, values = broker_users.read_env(env)
    assert values == {"A": "1", "B": "two words", "C": "x#y", "D": "plain", "E": "", "F": "q", "G": "a=b"}


def test_an_empty_password_line_is_replaced_not_duplicated(tmp_path, capsys):
    env, passwd = tmp_path / ".env", tmp_path / "passwd"
    env.write_text("PDM_MQTT_PASSWORD_GATE=\nPG_PASSWORD=change-me\n")
    assert run_users(env=env, passwd=passwd, capsys=capsys)[0] == 0
    text = env.read_text()
    assert text.count("PDM_MQTT_PASSWORD_GATE=") == 1 and "PG_PASSWORD=change-me" in text


# --- client settings: pdm_common.broker -------------------------------------------------------

class FakeClient:
    """Records what pdm_common does to a paho client."""
    instances: list = []
    hook = None                       # called at the end of loop_start, with the client

    def __init__(self, callback_api_version=None, client_id=""):
        self.client_id, self.calls, self.connected = client_id, [], False
        self.on_connect = self.on_disconnect = self.on_message = None
        FakeClient.instances.append(self)

    def username_pw_set(self, username, password=None):
        self.calls.append(("username_pw_set", username, password))

    def tls_set(self, ca_certs=None, **kwargs):
        self.calls.append(("tls_set", ca_certs))

    def connect(self, host, port=1883, keepalive=60, **kwargs):
        self.calls.append(("connect", host, port))

    def subscribe(self, topic, qos=0):
        self.calls.append(("subscribe", topic, qos))

    def is_connected(self):
        return self.connected

    def loop_start(self):
        self.calls.append(("loop_start",))
        self.connected = True
        self.on_connect(self, None, None, types.SimpleNamespace(is_failure=False), None)
        if FakeClient.hook is not None:
            FakeClient.hook(self)

    def loop_stop(self):
        self.calls.append(("loop_stop",))

    def disconnect(self):
        self.connected = False
        self.calls.append(("disconnect",))


@pytest.fixture
def clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith("PDM_MQTT_"):
            monkeypatch.delenv(name)
    return monkeypatch


@pytest.fixture
def ca_file(tmp_path):
    path = tmp_path / "ca.crt"
    path.write_text("a CA certificate; only its presence is checked here\n")
    return path


@pytest.fixture
def fake_paho(monkeypatch):
    client_module = types.ModuleType("paho.mqtt.client")
    client_module.Client = FakeClient
    client_module.CallbackAPIVersion = types.SimpleNamespace(VERSION2="VERSION2")
    mqtt_module = types.ModuleType("paho.mqtt")
    mqtt_module.client = client_module
    paho_module = types.ModuleType("paho")
    paho_module.mqtt = mqtt_module
    monkeypatch.setitem(sys.modules, "paho", paho_module)
    monkeypatch.setitem(sys.modules, "paho.mqtt", mqtt_module)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", client_module)
    FakeClient.instances, FakeClient.hook = [], None
    yield FakeClient
    FakeClient.hook = None


def test_no_variables_means_the_r0_behaviour(clean_env):
    options = broker.client_options("ingest")
    assert options == broker.BrokerOptions() and not options.tls
    client = FakeClient()
    broker.apply(client, options)
    assert client.calls == []
    assert broker.address("localhost", 1883) == ("localhost", 1883)


def test_the_service_account_wins_over_the_generic_one(clean_env):
    clean_env.setenv("PDM_MQTT_USER", "dashboard")
    clean_env.setenv("PDM_MQTT_PASSWORD", PW + "-generic")
    clean_env.setenv("PDM_MQTT_USER_SOUTH_CIP", "south-cip")
    clean_env.setenv("PDM_MQTT_PASSWORD_SOUTH_CIP", PW)
    cip = broker.client_options("south-cip")
    assert (cip.username, cip.password) == ("south-cip", PW)
    other = broker.client_options("ingest")
    assert (other.username, other.password) == ("dashboard", PW + "-generic")
    assert broker.client_options("").username == "dashboard"
    assert PW not in repr(cip)
    assert broker.env_key("south-cip") == "SOUTH_CIP" and broker.env_key("opcua_north") == "OPCUA_NORTH"


def test_tls_and_the_address(clean_env, ca_file):
    clean_env.setenv("PDM_MQTT_TLS_CA", str(ca_file))
    options = broker.client_options("gate")
    assert options.tls and broker.address("localhost", 1883, options) == ("localhost", 8883)
    clean_env.setenv("PDM_MQTT_HOST", "broker.test")
    clean_env.setenv("PDM_MQTT_PORT", "18883")
    assert broker.address("localhost", 1883) == ("broker.test", 18883)
    client = FakeClient()
    broker.apply(client, broker.client_options("gate"))
    assert client.calls == [("tls_set", str(ca_file))]


@pytest.mark.parametrize("name, value, message", [
    ("PDM_MQTT_USER_GATE", "gate", "neither PDM_MQTT_PASSWORD_GATE nor PDM_MQTT_PASSWORD is"),
    ("PDM_MQTT_PASSWORD", PW, "is set but neither PDM_MQTT_USER_GATE nor PDM_MQTT_USER is"),
    ("PDM_MQTT_PORT", "eighty", "PDM_MQTT_PORT must be a port number"),
    ("PDM_MQTT_PORT", "70000", "PDM_MQTT_PORT must be a port number"),
    ("PDM_MQTT_TLS_CA", "/no/such/ca.crt", "no such file"),
])
def test_inconsistent_variables_are_refused(clean_env, name, value, message):
    clean_env.setenv(name, value)
    with pytest.raises(broker.BrokerConfigError, match=re.escape(message)):
        broker.client_options("gate")


def test_connect_applies_the_options_then_connects(clean_env, ca_file):
    clean_env.setenv("PDM_MQTT_USER_REPLAYER", "replayer")
    clean_env.setenv("PDM_MQTT_PASSWORD_REPLAYER", PW)
    clean_env.setenv("PDM_MQTT_TLS_CA", str(ca_file))
    client = FakeClient()
    assert broker.connect(client, "replayer", "localhost", 1883) == ("localhost", 8883)
    assert client.calls == [("username_pw_set", "replayer", PW), ("tls_set", str(ca_file)),
                            ("connect", "localhost", 8883)]


# --- run_service and read_retained pick the options up ------------------------------------------

@pytest.fixture
def quiet_service(monkeypatch):
    """No metrics port, a private registry, and the signal handlers restored afterwards."""
    from pdm_common import metrics

    monkeypatch.setattr(metrics, "REGISTRY", metrics.Registry())
    monkeypatch.setenv("PDM_METRICS_PORT", "off")
    saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


def stop_the_service(client):
    signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)        # what SIGTERM would do


def test_run_service_logs_in_as_the_service(fake_paho, clean_env, ca_file, quiet_service):
    from pdm_common.mqtt import run_service

    clean_env.setenv("PDM_MQTT_HOST", "broker.test")
    clean_env.setenv("PDM_MQTT_TLS_CA", str(ca_file))
    clean_env.setenv("PDM_MQTT_USER_INGEST", "ingest")
    clean_env.setenv("PDM_MQTT_PASSWORD_INGEST", PW)
    clean_env.setenv("PDM_MQTT_USER_GATE", "gate")                 # another service's account is not used
    clean_env.setenv("PDM_MQTT_PASSWORD_GATE", PW + "-gate")
    fake_paho.hook = stop_the_service
    run_service("ingest", {"pdm/a1/telemetry": lambda payload: []}, host="localhost", port=1883)
    client = fake_paho.instances[-1]
    assert client.client_id == "pdm-ingest"
    assert client.calls[:4] == [("username_pw_set", "ingest", PW), ("tls_set", str(ca_file)),
                                ("connect", "broker.test", 8883), ("loop_start",)]
    assert ("subscribe", "pdm/a1/telemetry", 1) in client.calls and client.calls[-1] == ("disconnect",)


def test_run_service_without_variables_is_unchanged(fake_paho, clean_env, quiet_service):
    from pdm_common.mqtt import run_service

    fake_paho.hook = stop_the_service
    run_service("south-cip", {}, host="broker.lan", port=1884)
    client = fake_paho.instances[-1]
    assert client.client_id == "pdm-south-cip"
    assert client.calls[:2] == [("connect", "broker.lan", 1884), ("loop_start",)]


def test_run_service_stops_on_inconsistent_variables(fake_paho, clean_env, quiet_service):
    from pdm_common.mqtt import run_service

    clean_env.setenv("PDM_MQTT_USER_DETECTOR", "detector")
    with pytest.raises(broker.BrokerConfigError, match="PDM_MQTT_USER_DETECTOR is set"):
        run_service("detector", {}, host="localhost", port=1883)
    assert fake_paho.instances == []


def test_read_retained_uses_the_generic_account(fake_paho, clean_env):
    from pdm_common.mqtt import read_retained

    clean_env.setenv("PDM_MQTT_USER", "dashboard")
    clean_env.setenv("PDM_MQTT_PASSWORD", PW)
    clean_env.setenv("PDM_MQTT_USER_INGEST", "ingest")
    clean_env.setenv("PDM_MQTT_PASSWORD_INGEST", PW + "-ingest")
    clean_env.setenv("PDM_MQTT_PORT", "1884")
    fake_paho.hook = lambda c: c.on_message(c, None, types.SimpleNamespace(topic="pdm/a1/status", payload=b"{}"))
    assert read_retained("pdm/a1/status", host="localhost", port=1883, timeout=2) == b"{}"
    client = fake_paho.instances[-1]
    assert client.client_id == ""
    assert client.calls[:2] == [("username_pw_set", "dashboard", PW), ("connect", "localhost", 1884)]
    assert ("subscribe", "pdm/a1/status", 1) in client.calls


# --- certificates: tools/make_certs.sh and a TLS handshake ----------------------------------------

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None or shutil.which("bash") is None,
                                   reason="make_certs.sh needs bash and openssl")


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    if shutil.which("openssl") is None or shutil.which("bash") is None:
        pytest.skip("make_certs.sh needs bash and openssl")
    out = tmp_path_factory.mktemp("certs")
    env = dict(os.environ, PDM_CERTS_DIR=str(out))
    done = subprocess.run(["bash", str(MAKE_CERTS), "DNS:pdm-host.test", "IP:192.0.2.10"], env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return out


def handshake(certs: Path, server_hostname: str) -> str:
    """A TLS handshake on the loopback: our broker certificate on the server side, a client
    that trusts only ca.crt and checks the host name, as paho's tls_set(ca_certs=...) does."""
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(certs / "server.crt", certs / "server.key")
    client_ctx = ssl.create_default_context(cafile=str(certs / "ca.crt"))
    with socket.create_server(("127.0.0.1", 0)) as listener:
        listener.settimeout(10)

        def serve():
            try:
                conn, _ = listener.accept()
                with server_ctx.wrap_socket(conn, server_side=True) as tls:
                    tls.recv(1)
            except (OSError, ssl.SSLError):
                pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            with socket.create_connection(listener.getsockname(), timeout=10) as raw:
                with client_ctx.wrap_socket(raw, server_hostname=server_hostname) as tls:
                    return tls.version()
        finally:
            thread.join(10)


@needs_openssl
def test_make_certs_writes_a_ca_and_a_broker_certificate(certs):
    for name, mode in (("ca.key", 0o600), ("server.key", 0o600), ("ca.crt", 0o644), ("server.crt", 0o644)):
        assert (certs / name).stat().st_mode & 0o777 == mode, name
    text = subprocess.run(["openssl", "x509", "-in", str(certs / "server.crt"), "-noout", "-text"],
                          capture_output=True, text=True, check=True).stdout
    for name in ("DNS:localhost", "DNS:mosquitto", "IP Address:127.0.0.1", "DNS:pdm-host.test",
                 "IP Address:192.0.2.10", "TLS Web Server Authentication", "CA:FALSE"):
        assert name in text, name


@needs_openssl
@pytest.mark.parametrize("name", ["localhost", "mosquitto", "127.0.0.1", "pdm-host.test"])
def test_a_client_trusting_the_ca_accepts_the_broker_under_its_names(certs, name):
    assert handshake(certs, name) in ("TLSv1.2", "TLSv1.3")


@needs_openssl
def test_a_client_refuses_the_broker_under_another_name(certs):
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(certs, "elsewhere.test")


@needs_openssl
def test_make_certs_keeps_the_ca_and_rejects_bad_names(certs, tmp_path):
    out = tmp_path / "again"
    shutil.copytree(certs, out)
    env = dict(os.environ, PDM_CERTS_DIR=str(out))
    ca_before, server_before = (out / "ca.crt").read_bytes(), (out / "server.crt").read_bytes()
    done = subprocess.run(["bash", str(MAKE_CERTS)], env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert (out / "ca.crt").read_bytes() == ca_before and (out / "server.crt").read_bytes() != server_before
    assert "keeping the CA" in done.stdout
    bad = subprocess.run(["bash", str(MAKE_CERTS), "pdm-host.test"], env=env, capture_output=True, text=True)
    assert bad.returncode == 2 and "DNS:<name> or IP:<address>" in bad.stderr
