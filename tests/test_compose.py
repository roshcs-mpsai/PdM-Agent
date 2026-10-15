"""The compose stack, its image and CI, checked without Docker (build plan T35; FRD section 12,
NFR-04 to NFR-06, OPS-08).

compose.yaml is read as YAML and its ${...} references are resolved the way Compose resolves them,
with no .env: a required variable gets a placeholder, the others their defaults. The tests check
that

- every service has a pinned image, a health check (one exception, below) and a restart policy;
- every Python service's command names a file in the repository, ``python <file> --help`` works,
  and every --flag in the command is one that program accepts;
- ports agree with pdm_common.metrics.PORTS and ops/prometheus/prometheus.yml, each health check
  asks the service's own /health, and nothing is published beyond 127.0.0.1 by default;
- the networks follow FRD section 12: core internal, egress for the agent alone, south for the
  plant side only, north for the operator- and SCADA-facing endpoints;
- secrets come from .env by reference and never inline (tools/secret_scan.py agrees);
- the secure-broker override (mosquitto/compose.secure.yaml) mounts what docs/SECURITY.md lists
  and gives each broker client its own account from mosquitto/config/acl;
- the image installs the requirements and the library and runs unprivileged, and .dockerignore
  keeps the dataset, runs/, notebooks, .env and keys out of it;
- CI runs the secret scan, the tests and the harness on 3.10 and 3.11, with no Docker and no key.

Where the docker CLI is installed, ``docker compose config -q`` checks both files with Compose's
own parser as well (it needs no daemon); elsewhere that test is skipped.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import secret_scan  # noqa: E402
from pdm_common.broker import env_key  # noqa: E402
from pdm_common.metrics import PORTS  # noqa: E402

COMPOSE = ROOT / "compose.yaml"
SECURE = ROOT / "mosquitto" / "compose.secure.yaml"
DOCKERFILE = ROOT / "docker" / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
CI = ROOT / ".github" / "workflows" / "ci.yml"
VERIFY = ROOT / "tools" / "verify_local.sh"
PDM_IMAGE = "pdm-agent:local"
LOCALHOST = "127.0.0.1"
MQTT_PORT = 1883

# FRD section 12, with the ops network for the admin UIs published on localhost.
NETWORKS = {
    "mosquitto": {"core", "south"},
    "postgres": {"core"},
    "mlflow": {"core", "ops"},
    "replayer": {"core"},
    "ingest": {"core"},
    "detector": {"core"},
    "gate": {"core"},
    "prognostics": {"core"},
    "drift": {"core"},
    "mcp_server": {"core"},
    "agent": {"core", "egress"},
    "api": {"core", "north"},
    "dashboard": {"north"},
    "opcua_north": {"core", "north"},
    "connector": {"core", "north"},
    "south": {"south", "core"},
    "scada-sim": {"south"},
    "workorder-sim": {"north"},
    "prometheus": {"core", "ops"},
    "grafana": {"core", "ops"},
    "otel-collector": {"core", "ops"},
    "jaeger": {"core", "ops"},
}
# What the host sees: {service: {(host port, container port)}}, all on 127.0.0.1 by default.
PUBLISHED = {
    "mosquitto": {(1883, 1883)},
    "mlflow": {(5000, 5000)},
    "api": {(8000, 8000)},
    "dashboard": {(8501, 8501)},
    "opcua_north": {(4840, 4840)},
    "workorder-sim": {(8088, 8088)},
    "prometheus": {(9090, 9090)},
    "grafana": {(3000, 3000)},
    "otel-collector": {(4317, 4317), (4318, 4318)},
    "jaeger": {(16686, 16686)},
}
NORTH_BIND = "PDM_NORTH_BIND"                       # opens these to the LAN when set
NORTH_BIND_SERVICES = {"api", "dashboard", "workorder-sim"}
ONE_SHOT = {"replayer": "replay", "scada-sim": "scada"}     # service: its profile
HTTP_HEALTH = {"api": 8000, "dashboard": 8501, "workorder-sim": 8088}  # /health on their own port
BROKER_CONNECTION_HEALTH = {"replayer", "scada-sim"}                   # they serve no /health
NO_HEALTHCHECK = {"otel-collector"}   # no shell or HTTP client in the image; verify_local.sh asks :13133
STORE_USERS = {"ingest", "detector", "gate", "prognostics", "drift", "mcp_server", "api", "connector"}
STORE_URL = "postgresql://pdm:${PG_PASSWORD}@postgres:5432/pdm"
BROKER_ACCOUNTS = {"replayer": "replayer", "ingest": "ingest", "detector": "detector", "gate": "gate",
                   "prognostics": "prognostics", "drift": "drift", "agent": "agent", "opcua_north": "opcua_north",
                   "south": "south", "scada-sim": "scada_sim"}
REQUIRED = {"PG_PASSWORD", "PDM_TOKEN_AGENT", "PDM_TOKEN_CONNECTOR", "GRAFANA_PASSWORD"}
DRY_RUN = "--scripted services/agent/scripts/dry_run.json"
THIS_TASK = ["compose.yaml", "mosquitto/compose.secure.yaml", "docker/Dockerfile", ".dockerignore",
             ".github/workflows/ci.yml", "tools/verify_local.sh", "docs/RUNBOOK.md", "README.md",
             "tests/test_compose.py"]


# --- reading compose files ----------------------------------------------------------------------

class Tagged:
    """A value carrying a Compose merge tag (!override, !reset)."""

    def __init__(self, tag: str, value):
        self.tag, self.value = tag, value


class _Loader(yaml.SafeLoader):
    pass


def _tagged(loader, suffix, node):
    if isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    elif isinstance(node, yaml.MappingNode):
        value = loader.construct_mapping(node, deep=True)
    else:
        value = loader.construct_scalar(node)
    return Tagged(suffix, value)


_Loader.add_multi_constructor("!", _tagged)

_REF = re.compile(r"\$(?:\$|\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?])([^}]*))?\}|([A-Za-z_][A-Za-z0-9_]*))")


def interpolate(text: str, env: dict, required: set) -> str:
    """Compose's ${VAR}, ${VAR:-default}, ${VAR-default}, ${VAR:?message}, ${VAR?message} and $$.
    A required variable that is missing is added to ``required`` and becomes <VAR>."""
    def sub(m):
        if m.group(0) == "$$":
            return "$"
        name, op, arg = m.group(1) or m.group(4), m.group(2), m.group(3)
        value = env.get(name)
        if op in (":-", "-"):
            return arg if value is None or (op == ":-" and value == "") else value
        if op in (":?", "?"):
            required.add(name)
            return f"<{name}>" if value is None or (op == ":?" and value == "") else value
        return value or ""
    return _REF.sub(sub, text)


def resolve(node, env: dict, required: set):
    if isinstance(node, str):
        return interpolate(node, env, required)
    if isinstance(node, dict):
        return {k: resolve(v, env, required) for k, v in node.items()}
    if isinstance(node, list):
        return [resolve(v, env, required) for v in node]
    if isinstance(node, Tagged):
        return Tagged(node.tag, resolve(node.value, env, required))
    return node


def load(path: Path, env: dict | None = None) -> tuple[dict, dict, set]:
    """(raw document, resolved document, required variables)."""
    raw = yaml.load(path.read_text(), Loader=_Loader)
    required: set = set()
    return raw, resolve(raw, env or {}, required), required


@pytest.fixture(scope="module")
def raw():
    return load(COMPOSE)[0]


@pytest.fixture(scope="module")
def doc():
    return load(COMPOSE)[1]


@pytest.fixture(scope="module")
def services(doc):
    return doc["services"]


def argv(service: dict) -> list[str]:
    """entrypoint + command, split as Compose splits a string."""
    parts: list[str] = []
    for key in ("entrypoint", "command"):
        value = service.get(key)
        if value is not None:
            parts += shlex.split(value) if isinstance(value, str) else [str(v) for v in value]
    return parts


def ours(services: dict) -> dict:
    """The services built from docker/Dockerfile."""
    return {name: s for name, s in services.items() if s.get("image") == PDM_IMAGE}


def health_test(service: dict) -> str:
    test = (service.get("healthcheck") or {}).get("test")
    return " ".join(test) if isinstance(test, list) else str(test or "")


def published(service: dict) -> list[tuple[str, int, int]]:
    """(host ip, host port, container port) for each short-syntax port."""
    out = []
    for spec in service.get("ports", []):
        parts = str(spec).split("/")[0].split(":")
        assert len(parts) == 3, f"{spec}: give the host address too (127.0.0.1:port:port)"
        out.append((parts[0], int(parts[1]), int(parts[2])))
    return out


def networks_of(service: dict) -> set:
    nets = service.get("networks") or []
    return set(nets if isinstance(nets, list) else nets.keys())


def depends(service: dict) -> dict:
    dep = service.get("depends_on") or {}
    return {d: {"condition": "service_started"} for d in dep} if isinstance(dep, list) else dep


# --- services, images, restart, health --------------------------------------------------------------

def test_the_stack_has_every_service_of_frd_section_12(services):
    assert set(services) == set(NETWORKS)


def test_the_variables_compose_requires_from_env_are_the_documented_ones():
    _, _, required = load(COMPOSE)
    assert required == REQUIRED
    example = (ROOT / ".env.example").read_text()
    assert "PG_PASSWORD" in example and "PDM_TOKEN_AGENT" in example and "PDM_TOKEN_CONNECTOR" in example


def test_images_are_pinned_and_ours_come_from_one_dockerfile(services):
    for name, s in services.items():
        image = s.get("image", "")
        if image == PDM_IMAGE:
            assert s["build"] == {"context": ".", "dockerfile": "docker/Dockerfile"}, name
        else:
            assert "build" not in s, name
            tag = image.rsplit(":", 1)[-1] if ":" in image.split("/")[-1] else ""
            assert re.fullmatch(r"v?\d+\.\d+(\.\d+)?(-[\w.]+)?", tag), f"{name}: pin {image} to a version tag"
    expected_ours = set(NETWORKS) - {"mosquitto", "postgres", "mlflow", "prometheus", "grafana", "otel-collector",
                                     "jaeger"}
    assert set(ours(services)) == expected_ours


def test_long_running_services_restart_and_one_shot_jobs_do_not(services):
    for name, s in services.items():
        if name in ONE_SHOT:
            assert s.get("profiles") == [ONE_SHOT[name]], name
            assert s["restart"] == "no", f"{name} ends by itself; unless-stopped would run it again"
        else:
            assert "profiles" not in s, f"{name} must start with the stack"
            assert s["restart"] == "unless-stopped", name


def test_every_service_has_a_health_check_on_its_own_health_endpoint(services):
    for name, s in services.items():
        if name in NO_HEALTHCHECK:
            assert "healthcheck" not in s, name
            continue
        check = s.get("healthcheck") or {}
        assert check.get("test") and check["test"][0] in ("CMD", "CMD-SHELL"), name
        assert check.get("interval") and check.get("timeout") and check.get("retries"), name
    for name, port in PORTS.items():
        if name in BROKER_CONNECTION_HEALTH or name in HTTP_HEALTH:
            continue
        assert f"http://{LOCALHOST}:{port}/health" in health_test(services[name]), \
            f"{name}: its health check should ask /health on {port} (pdm_common.metrics.PORTS)"
    for name, port in HTTP_HEALTH.items():
        assert f"http://{LOCALHOST}:{port}/" in health_test(services[name]), name
    for name in BROKER_CONNECTION_HEALTH:
        test = health_test(services[name])
        assert "/proc/net/tcp" in test and f":{MQTT_PORT:04X}'" in test, name
    assert "socket.create_connection(('127.0.0.1', 8765)" in health_test(services["mcp_server"])
    assert "socket.create_connection(('127.0.0.1', 4840)" in health_test(services["opcua_north"])


def test_the_broker_connection_check_reads_proc_net_tcp_as_intended(services):
    """Run the health check's one-liner on made-up /proc/net/tcp lines."""
    code = services["replayer"]["healthcheck"]["test"][-1]
    header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"
    connected = "   0: 0300120A:A1B2 0200120A:075B 01 00000000:00000000 00:00000000 00000000 10001 0 1 1"
    waiting = "   1: 0300120A:A1B4 0200120A:075B 02 00000000:00000000 00:00000000 00000000 10001 0 2 1"
    other = "   2: 0300120A:A1B6 0200120A:1F90 01 00000000:00000000 00:00000000 00000000 10001 0 3 1"

    def run(lines):
        fake = "\n".join([header, *lines]) + "\n"
        src = code.replace("open('/proc/net/tcp').read()", repr(fake))
        return subprocess.run([sys.executable, "-c", src], capture_output=True).returncode

    assert run([connected]) == 0
    assert run([waiting, other]) == 1
    assert run([]) == 1


# --- commands ------------------------------------------------------------------------------------

def _help_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PDM_", "ANTHROPIC_"))}
    env["PDM_METRICS_PORT"] = "off"
    return env


@pytest.fixture(scope="module")
def helps(services):
    """{script: CompletedProcess of `python <script> --help`}, run in parallel."""
    scripts = sorted({argv(s)[1] for s in ours(services).values() if argv(s)[0] == "python"})

    def run(script):
        return script, subprocess.run([sys.executable, script, "--help"], cwd=ROOT, capture_output=True, text=True,
                                      timeout=300, env=_help_env())

    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(run, scripts))


def _repo_paths(args: list[str]) -> list[str]:
    """Arguments that name a file of the repository (not data/ or runs/, which are mounted)."""
    return [a for a in args if re.fullmatch(r"[\w./()-]+\.(?:py|ya?ml|json)", a) and "/" in a
            and not a.startswith(("data/", "runs/"))]


def _flags(args: list[str]) -> list[str]:
    return [a.split("=", 1)[0] for a in args if a.startswith("--")]


def test_every_python_service_runs_a_file_of_the_repository_with_flags_it_accepts(services, helps):
    problems = []
    for name, s in sorted(ours(services).items()):
        args = argv(s)
        if args[0] == "streamlit":
            assert args[1] == "run" and (ROOT / args[2]).is_file(), name
            assert all(re.fullmatch(r"--[a-z]+\.[A-Za-z]+=\S+", a) for a in args[3:]), name
            continue
        assert args[0] == "python", f"{name}: {args}"
        script = args[1]
        if not (ROOT / script).is_file():
            problems.append(f"{name}: {script} does not exist")
            continue
        proc = helps[script]
        if proc.returncode != 0:
            problems.append(f"{name}: python {script} --help failed:\n{proc.stderr[-2000:]}")
            continue
        for flag in _flags(args[2:]):
            if not re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", proc.stdout):
                problems.append(f"{name}: {script} has no option {flag}")
        for path in _repo_paths(args[2:]):
            if not (ROOT / path).exists():
                problems.append(f"{name}: {path} does not exist")
    assert not problems, "\n".join(problems)


def test_the_agent_writes_through_the_api_and_reads_through_the_tools(raw, services, helps):
    args = argv(services["agent"])
    assert args[args.index("--api") + 1] == "http://api:8000"
    assert args[args.index("--tools") + 1] == "http://mcp_server:8765/mcp"
    assert "--store" not in args and "PDM_STORE" not in services["agent"]["environment"]
    env = raw["services"]["agent"]["environment"]
    assert env["ANTHROPIC_API_KEY"] == "${ANTHROPIC_API_KEY:-}"
    assert env["PDM_TOKEN_AGENT"].startswith("${PDM_TOKEN_AGENT:?")
    # the dry run without a key: PDM_AGENT_ARGS in .env adds the script
    dry = load(COMPOSE, {"PDM_AGENT_ARGS": DRY_RUN})[1]["services"]["agent"]
    extra = argv(dry)[len(args):]
    assert extra == DRY_RUN.split() and (ROOT / extra[1]).is_file()
    assert "--scripted" in helps[args[1]].stdout
    mcp = argv(services["mcp_server"])
    assert mcp[mcp.index("--host") + 1] == "0.0.0.0" and mcp[mcp.index("--port") + 1] == "8765"


def test_the_replayer_and_the_simulated_plant_take_the_dataset_from_a_read_only_mount(services):
    for name in ("replayer", "scada-sim"):
        s = services[name]
        data = argv(s)[argv(s).index("--data") + 1]
        assert data.startswith("data/"), name
        assert "./data:/app/data:ro" in s["volumes"], name
    for name, s in services.items():
        for v in s.get("volumes", []):
            spec = v if isinstance(v, str) else f"{v.get('source')}:{v.get('target')}"
            source = spec.split(":")[0]
            assert source not in (".", "./", "./.env", ".env"), f"{name} mounts {source}"
            if source.rstrip("/") == "./data":
                assert spec.endswith(":ro"), f"{name} mounts the dataset writable"


# --- ports and metrics --------------------------------------------------------------------------------

def test_published_ports_are_the_frd_ones_on_localhost(raw, services):
    for name, s in services.items():
        ports = published(s)
        assert {(h, c) for _, h, c in ports} == PUBLISHED.get(name, set()), name
        for ip, _, _ in ports:
            assert ip == LOCALHOST, f"{name} publishes on {ip} by default"
        if ports:
            assert networks_of(s) - {"core"}, f"{name} publishes ports but is on internal networks only"
    for name, s in raw["services"].items():
        uses_bind = any(NORTH_BIND in str(p) for p in s.get("ports", []))
        assert uses_bind == (name in NORTH_BIND_SERVICES), name
    opened = load(COMPOSE, {NORTH_BIND: "0.0.0.0"})[1]["services"]
    assert {n for n, s in opened.items() if any(ip == "0.0.0.0" for ip, _, _ in published(s))} == NORTH_BIND_SERVICES
    metrics_ports = set(PORTS.values())
    assert not {h for s in services.values() for _, h, _ in published(s)} & metrics_ports


def test_metrics_ports_agree_with_pdm_common_and_prometheus(services):
    prom = yaml.safe_load((ROOT / "ops" / "prometheus" / "prometheus.yml").read_text())
    jobs = {j["job_name"]: [t for sc in j["static_configs"] for t in sc["targets"]] for j in prom["scrape_configs"]}
    assert set(jobs) - {"prometheus"} == set(PORTS)
    for name, port in PORTS.items():
        assert jobs[name] == [f"{name}:{port}"], name
        assert name in services and "core" in networks_of(services[name]), name
        assert services[name]["environment"].get("PDM_METRICS_HOST") == "0.0.0.0", name
    assert "core" in networks_of(services["prometheus"])
    for name, s in ours(services).items():
        assert s["environment"].get("PDM_METRICS_HOST") == "0.0.0.0", name


# --- networks -------------------------------------------------------------------------------------------

def test_networks_follow_frd_section_12(doc, services):
    nets = doc["networks"]
    assert set(nets) == {"core", "south", "north", "egress", "ops"}
    assert nets["core"].get("internal") is True
    masquerade = "com.docker.network.bridge.enable_ip_masquerade"
    for name in ("south", "north", "ops"):
        assert not (nets[name] or {}).get("internal"), f"{name} publishes ports, so it cannot be internal"
        assert (nets[name] or {}).get("driver_opts", {}).get(masquerade) == "false", \
            f"{name}: no route off the host, egress is the only one"
    assert not (nets["egress"] or {}).get("internal") and masquerade not in (nets["egress"] or {}).get("driver_opts", {})
    for name, s in services.items():
        assert networks_of(s) == NETWORKS[name], name

    def members(net):
        return {n for n, s in services.items() if net in networks_of(s)}

    assert members("egress") == {"agent"}
    assert members("south") == {"mosquitto", "south", "scada-sim"}
    assert members("north") == {"api", "dashboard", "opcua_north", "connector", "workorder-sim"}
    assert networks_of(services["dashboard"]) == {"north"}, "the dashboard reaches the store only through the api"


# --- environment, dependencies, secrets ----------------------------------------------------------------

def test_store_broker_and_tracing_settings(raw, services):
    for name, s in raw["services"].items():
        env = s.get("environment") or {}
        if name in STORE_USERS:
            assert env.get("PDM_STORE") == STORE_URL, name
            assert depends(services[name]).get("postgres") == {"condition": "service_healthy"}, name
        else:
            assert "PDM_STORE" not in env, f"{name} has no business in the store"
        if name in BROKER_ACCOUNTS:
            assert env.get("PDM_MQTT_HOST") == "mosquitto" and str(env.get("PDM_MQTT_PORT")) == str(MQTT_PORT), name
            assert depends(services[name]).get("mosquitto") == {"condition": "service_healthy"}, name
        else:
            assert "PDM_MQTT_HOST" not in env, name
        # the R0 broker is anonymous; accounts come with mosquitto/compose.secure.yaml
        assert not {"PDM_MQTT_USER", "PDM_MQTT_PASSWORD"} & set(env), name
    for name in ("agent", "mcp_server"):
        assert services[name]["environment"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://otel-collector:4318"
    api_tokens = {k for k in raw["services"]["api"]["environment"] if k.startswith("PDM_TOKEN_")}
    assert "PDM_TOKEN_AGENT" in api_tokens and "PDM_TOKEN_CONNECTOR" not in api_tokens
    assert any(k.startswith("PDM_TOKEN_REVIEWER") for k in api_tokens)
    connector = raw["services"]["connector"]["environment"]
    assert connector["PDM_WORKORDER_URL"] == "http://workorder-sim:8088/api/work-orders"
    assert raw["services"]["workorder-sim"]["environment"]["WORKORDER_SIM_TOKEN"].startswith("${PDM_TOKEN_CONNECTOR:?")


def test_dependencies_exist_and_wait_for_health(services):
    for name, s in services.items():
        for dep, how in depends(s).items():
            assert dep in services, f"{name} depends on {dep}, which is not defined"
            if name not in ONE_SHOT:
                assert dep not in ONE_SHOT, f"{name} waits for a one-shot job"
            if dep in NO_HEALTHCHECK or name in ("grafana", "otel-collector"):
                continue
            assert how.get("condition") == "service_healthy", f"{name} -> {dep}"


_SECRETISH = re.compile(r"PASSWORD|TOKEN|SECRET|API_KEY|SALT")


def test_no_secret_is_written_inline(raw):
    for name, s in raw["services"].items():
        assert "env_file" not in s, f"{name}: list its variables; no container gets the whole .env"
        env = s.get("environment") or {}
        assert isinstance(env, dict), name
        for key, value in env.items():
            value = str(value)
            if _SECRETISH.search(key):
                assert value.startswith("${") and value.endswith("}"), f"{name}: {key} is written inline"
            for userinfo in re.findall(r"://[^/@\s]+:([^/@\s]+)@", value):
                assert userinfo == "${PG_PASSWORD}", f"{name}: {key} holds a password"


def test_the_stack_files_hold_no_secret():
    rules, allowlist = secret_scan.load_config(ROOT / ".gitleaks.toml")
    files = list(secret_scan.given_files(ROOT, [str(ROOT / p) for p in THIS_TASK if (ROOT / p).exists()]))
    findings, read = secret_scan.scan(files, rules, allowlist)
    assert [str(f) for f in findings] == []
    assert read >= 8


def test_volumes_and_config_mounts(services):
    for name, s in services.items():
        for v in s.get("volumes", []):
            if isinstance(v, str) and v.startswith("./"):
                source = v.split(":")[0]
                assert (ROOT / source).exists() or source == "./data", f"{name}: {source} is not in the repository"
    assert "./ops/prometheus:/etc/prometheus:ro" in services["prometheus"]["volumes"]
    grafana_path = yaml.safe_load((ROOT / "ops/grafana/provisioning/dashboards/dashboards.yml").read_text())[
        "providers"][0]["options"]["path"]
    assert f"./ops/grafana/dashboards:{grafana_path}:ro" in services["grafana"]["volumes"]
    assert "./ops/grafana/provisioning:/etc/grafana/provisioning:ro" in services["grafana"]["volumes"]
    config = argv(services["otel-collector"])[0].split("=", 1)[1]
    assert f"./ops/otel/collector.yml:{config}:ro" in services["otel-collector"]["volumes"]
    collector = yaml.safe_load((ROOT / "ops" / "otel" / "collector.yml").read_text())
    assert collector["exporters"]["otlp/jaeger"]["endpoint"] == "jaeger:4317"
    # each init file on its own, so the TimescaleDB image's own scripts in that folder still run
    for sql in sorted((ROOT / "db" / "init").iterdir()):
        mount = f"./db/init/{sql.name}:/docker-entrypoint-initdb.d/{sql.name}:ro"
        assert mount in services["postgres"]["volumes"], f"mount {sql.name} into the postgres service"
    assert not [v for v in services["postgres"]["volumes"] if v.endswith(":/docker-entrypoint-initdb.d:ro")]


# --- the secure broker ------------------------------------------------------------------------------

def _acl_users() -> set:
    lines = (ROOT / "mosquitto" / "config" / "acl").read_text().splitlines()
    return {line.split(None, 1)[1].strip() for line in lines if line.startswith("user ")}


def test_the_secure_override_mounts_what_security_md_lists_and_logs_each_service_in():
    raw_secure, secure, required = load(SECURE)
    broker = secure["services"]["mosquitto"]
    assert isinstance(broker["ports"], Tagged) and broker["ports"].tag == "override"
    assert broker["ports"].value == [f"{LOCALHOST}:8883:8883"], "1883 must not be published with accounts on"
    mounts = {(v if isinstance(v, str) else f"{v['source']}:{v['target']}{':ro' if v.get('read_only') else ''}")
              for v in broker["volumes"]}
    assert mounts == {
        "./mosquitto/config/mosquitto.secure.conf:/mosquitto/config/mosquitto.conf:ro",
        "./mosquitto/config/acl:/mosquitto/config/acl:ro",
        "./mosquitto/config/passwd:/mosquitto/config/passwd:ro",
        "./mosquitto/certs:/mosquitto/certs:ro",
        "mosquitto-data:/mosquitto/data",
    }
    long_syntax = [v for v in broker["volumes"] if isinstance(v, dict)]
    assert {v["target"] for v in long_syntax} == {"/mosquitto/config/passwd", "/mosquitto/certs"}
    assert all(v["type"] == "bind" for v in long_syntax)
    test = " ".join(broker["healthcheck"]["test"])
    assert "-p 8883" in test and "--cafile /mosquitto/certs/ca.crt" in test and "-u healthcheck" in test
    assert "$PDM_MQTT_PASSWORD_HEALTHCHECK" in test
    assert raw_secure["services"]["mosquitto"]["healthcheck"]["test"][-1].count("$$") == 2
    assert "mosquitto-data" in secure["volumes"]
    users = _acl_users()
    assert set(secure["services"]) - {"mosquitto"} == set(BROKER_ACCOUNTS)
    for name, account in BROKER_ACCOUNTS.items():
        env = raw_secure["services"][name]["environment"]
        assert env["PDM_MQTT_USER"] == account and account in users, name
        assert env["PDM_MQTT_PASSWORD"].startswith(f"${{PDM_MQTT_PASSWORD_{env_key(account)}:?"), name
        assert set(raw_secure["services"][name]) == {"environment"}, f"{name}: only the account changes"
    assert required == {f"PDM_MQTT_PASSWORD_{env_key(a)}" for a in set(BROKER_ACCOUNTS.values()) | {"healthcheck"}}
    conf = (ROOT / "mosquitto" / "config" / "mosquitto.secure.conf").read_text()
    assert "listener 8883" in conf and "allow_anonymous false" in conf


# --- the image -----------------------------------------------------------------------------------------

def _instructions(text: str) -> list[str]:
    joined = re.sub(r"\\\n", " ", text)
    return [" ".join(line.split()) for line in joined.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def test_the_image_installs_the_requirements_and_the_library_and_runs_unprivileged():
    text = DOCKERFILE.read_text()
    lines = _instructions(text)
    base = dict(re.findall(r"^ARG (\w+)=(\S+)", text, re.M)).get("PYTHON_IMAGE", "")
    assert re.fullmatch(r"python:3\.1[01]\.\d+-slim-\w+", base), f"pin the base image to a patch version: {base}"
    assert [line for line in lines if line.startswith("FROM ")] == ["FROM ${PYTHON_IMAGE}"]
    assert "WORKDIR /app" in lines
    runs = " ".join(line for line in lines if line.startswith("RUN "))
    assert "pip install -r requirements.txt -r services/dashboard/requirements.txt" in runs
    assert re.search(r"pip install (?:-e )?\./lib\b", runs)
    copies = [line.split()[1:] for line in lines if line.startswith("COPY ")]
    assert [".", "."] in copies
    for source, _ in copies:
        assert (ROOT / source).exists(), source
    users = [line.split()[1] for line in lines if line.startswith("USER ")]
    assert users and users[-1] not in ("root", "0")
    assert not [line for line in lines if line.startswith(("ENV ", "ARG ")) and _SECRETISH.search(line)]


def _ignore_patterns() -> list[tuple[bool, re.Pattern]]:
    out = []
    for line in DOCKERIGNORE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        pattern = (line[1:] if negate else line).strip().strip("/")
        rx, i = "", 0
        while i < len(pattern):
            if pattern.startswith("**/", i):
                rx, i = rx + "(?:.*/)?", i + 3
            elif pattern.startswith("**", i):
                rx, i = rx + ".*", i + 2
            elif pattern[i] == "*":
                rx, i = rx + "[^/]*", i + 1
            elif pattern[i] == "?":
                rx, i = rx + "[^/]", i + 1
            elif pattern[i] == "[":
                end = pattern.index("]", i)
                rx, i = rx + pattern[i:end + 1], end + 1
            else:
                rx, i = rx + re.escape(pattern[i]), i + 1
        out.append((negate, re.compile(rx)))
    return out


def ignored(path: str, patterns) -> bool:
    """Docker's rule: the last pattern matching the path or one of its parent folders decides."""
    parts = path.split("/")
    prefixes = ["/".join(parts[:k]) for k in range(1, len(parts) + 1)]
    out = False
    for negate, rx in patterns:
        if any(rx.fullmatch(p) for p in prefixes):
            out = not negate
    return out


def _tracked(folder: str) -> list[str]:
    """The files git tracks under ``folder`` (what the image is built from); outside a git
    checkout, every file but caches and the operating system's clutter (Finder's .DS_Store)."""
    try:
        out = subprocess.run(["git", "ls-files", "-z", "--", folder], cwd=ROOT, capture_output=True, check=True)
        files = [f for f in out.stdout.decode().split("\0") if f]
        if files:
            return files
    except (OSError, subprocess.CalledProcessError):
        pass
    return [p.relative_to(ROOT).as_posix() for p in (ROOT / folder).rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and ".egg-info" not in str(p)
            and p.name not in (".DS_Store", "Thumbs.db")]


def test_dockerignore_keeps_data_runs_notebooks_and_secrets_out():
    patterns = _ignore_patterns()
    out = ["data/MetroPT3(AirCompressor).csv", "data/processed_v4/grid_10s.csv.gz", "data/catalogue.jsonl",
           "runs/metropt3_apu/quarantine.jsonl", "runs/pdm.db", ".env", ".env.local", "mosquitto/config/passwd",
           "mosquitto/certs/ca.key", "mosquitto/certs/server.crt", "notebooks/metropt3_eda.ipynb",
           "eval/out/freeze/pca/scores.npz", "services/opcua_north/server_key.pem", "server_cert.der",
           "lib/pdm_common/__pycache__/store.cpython-311.pyc", ".git/config", "mlruns/0/meta.yaml",
           "lib/pdm_common.egg-info/PKG-INFO", "compose.override.yaml", ".venv/bin/python"]
    assert [p for p in out if not ignored(p, patterns)] == []
    kept = ["requirements.txt", "services/dashboard/requirements.txt", "lib/pyproject.toml", ".env.example",
            "docker/Dockerfile", ".github/workflows/ci.yml", "tools/workorder_sim.py", "db/init/001_schema.sql"]
    for folder in ("lib", "services", "profiles", "eval/frozen", "corpus", "skills", "checks", "schemas", "ops"):
        kept += _tracked(folder)
    kept.append("eval/config.yaml")
    assert [p for p in kept if ignored(p, patterns)] == []


# --- CI and the local check -------------------------------------------------------------------------------

def test_ci_runs_the_scan_the_tests_and_the_harness_on_310_and_311():
    workflow = yaml.safe_load(CI.read_text())
    job = workflow["jobs"]["test"]
    assert job["strategy"]["matrix"]["python-version"] == ["3.10", "3.11"]
    steps = job["steps"]
    runs = [s.get("run", "") for s in steps]
    order = ["python tools/secret_scan.py", "pip install -r requirements.txt", "pip install -e lib", "pytest -q",
             "python eval/evaluate.py"]
    assert [r for r in runs if r] == order
    setup = [s for s in steps if str(s.get("uses", "")).startswith("actions/setup-python")]
    assert setup and setup[0]["with"]["python-version"] == "${{ matrix.python-version }}"
    for step in steps:
        text = " ".join(str(v) for v in step.values()).lower()
        assert "docker" not in text and "compose" not in text and "anthropic" not in text, step
    assert "secrets." not in CI.read_text()


def test_verify_local_is_an_executable_bash_script():
    assert VERIFY.is_file() and os.access(VERIFY, os.X_OK)
    text = VERIFY.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "docker compose config -q" in text and "pytest" in text
    if shutil.which("bash"):
        proc = subprocess.run(["bash", "-n", str(VERIFY)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


# --- Compose's own parser, where the docker CLI is installed --------------------------------------------

def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _compose_available(), reason="needs the docker CLI with the compose plugin (no daemon)")
def test_docker_compose_config_accepts_both_files(tmp_path):
    values = {name: "change-me" for name in REQUIRED}
    values.update({f"PDM_MQTT_PASSWORD_{env_key(a)}": "change-me"
                   for a in set(BROKER_ACCOUNTS.values()) | {"healthcheck"}})
    env_file, empty = tmp_path / "test.env", tmp_path / "empty.env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in sorted(values.items())))
    empty.write_text("")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PDM_", "COMPOSE_", "GRAFANA_", "PG_"))}

    def config(env_path, *files):
        args = ["docker", "compose", "--project-directory", str(ROOT), "--env-file", str(env_path)]
        for f in files:
            args += ["-f", str(f)]
        return subprocess.run(args + ["--profile", "replay", "--profile", "scada", "config", "-q"],
                              capture_output=True, text=True, timeout=120, env=env)

    for files in ((COMPOSE,), (COMPOSE, SECURE)):
        proc = config(env_file, *files)
        assert proc.returncode == 0, proc.stderr
    missing = config(empty, COMPOSE)               # a fresh clone with no .env stops at once, naming what is missing
    named = re.findall(r"required variable (\w+) is missing", missing.stderr)
    assert missing.returncode != 0 and named and set(named) <= set(REQUIRED)   # some versions name only the first
