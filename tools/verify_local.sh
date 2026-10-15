#!/usr/bin/env bash
# Check the whole stack on a laptop with Docker (build plan T35; docs/RUNBOOK.md).
#
#   tools/verify_local.sh               # the tests, compose config, the stack up and healthy, the F4
#                                       # replay, then an incident and a brief through the API
#   tools/verify_local.sh --journey-a   # also approve the brief and follow it to the work-order
#                                       # simulator and to NE 107 over OPC UA, as SCADA reads it
#
# Options:
#   --skip-tests       do not run pytest first
#   --config-only      stop after `docker compose config -q`
#   --no-replay        bring the stack up and check its health, no replay
#   --journey-a        approve the newest brief with a reviewer token from .env, then check the order
#                      at the simulator and NE 107 on the OPC UA server. This writes a real decision
#                      into the store: use a test stack (`docker compose down -v` empties one).
#   --check-networks   core and north have no route off the host, egress reaches the model API, and
#                      the dashboard cannot reach the store
#   --live-agent       diagnose with the model (ANTHROPIC_API_KEY in .env); default: the dry-run script
#   --from ISO --to ISO --speed N
#                      the replay; default the F4 lead-up of README "Run the stream" at 1000x
#   --timeout SECONDS  how long each wait may take (default 600)
#   --down             `docker compose down` at the end; the volumes stay
#
# Needs docker with Compose 2.24.4 or later, python3 with requirements.txt installed (pytest, and
# the API calls), a filled-in .env, and the dataset under data/ for the replay. Tokens are read
# from .env by Python and never put in this shell's environment.
# Exit status: 0 when every check that ran passed, 1 otherwise.
set -o pipefail

cd "$(dirname "$0")/.." || exit 1
PYTHON="${PYTHON:-python3}"
DATA="data/MetroPT3(AirCompressor).csv"
FROM="2020-07-14T00:00:00"
TO="2020-07-15T20:00:00"
SPEED="1000"
TIMEOUT="600"
RUN_TESTS=1 CONFIG_ONLY=0 REPLAY=1 JOURNEY=0 NETWORKS=0 LIVE=0 DOWN=0

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --skip-tests) RUN_TESTS=0 ;;
    --config-only) CONFIG_ONLY=1 ;;
    --no-replay) REPLAY=0 ;;
    --journey-a) JOURNEY=1 ;;
    --check-networks) NETWORKS=1 ;;
    --live-agent) LIVE=1 ;;
    --down) DOWN=1 ;;
    --from) FROM="$2"; shift ;;
    --to) TO="$2"; shift ;;
    --speed) SPEED="$2"; shift ;;
    --timeout) TIMEOUT="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "verify_local: unknown option $1 (--help lists them)" >&2; exit 2 ;;
  esac
  shift
done

RESULTS=""
FAILED=0
record() { RESULTS="${RESULTS}$1  $2
"; echo "$1  $2"; }
pass() { record PASS "$1"; }
fail() { record FAIL "$1"; FAILED=1; }
skip() { record SKIP "$1"; }
step() { printf '\n== %s\n' "$1"; }

finish() {
  if [ "$DOWN" = 1 ]; then
    step "docker compose down (the volumes stay)"
    docker compose down
  fi
  printf '\n== Summary\n%s' "$RESULTS"
  if [ "$FAILED" = 0 ]; then echo "every check that ran passed"; else echo "some checks failed: see above"; fi
  exit "$FAILED"
}

# The API side of the checks. Tokens come from the shell's environment or from .env, the way
# compose finds them; requests to 127.0.0.1 skip any proxy.
api() {
  "$PYTHON" - "$@" <<'PY'
import json, os, sys, time, urllib.error, urllib.request
from pathlib import Path

sys.path.insert(0, "tools")
from broker_users import read_env  # noqa: E402  (reads .env the way docker compose does)

_, FILE = read_env(Path(".env"))
API, SIM, PROM = "http://127.0.0.1:8000", "http://127.0.0.1:8088", "http://127.0.0.1:9090"
PEOPLE = ["PDM_TOKEN_OPERATOR", "PDM_TOKEN_ENGINEER", "PDM_TOKEN_EVALUATOR", "PDM_TOKEN_REVIEWER",
          "PDM_TOKEN_REVIEWER_ROSH", "PDM_TOKEN_REVIEWER_ASHOK", "PDM_TOKEN_REVIEWER_GAYATRI"]
REVIEWERS = [name for name in PEOPLE if name.startswith("PDM_TOKEN_REVIEWER")]
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def setting(name):
    return os.environ.get(name) or FILE.get(name) or ""


def token(names, what):
    for name in names:
        if setting(name):
            return setting(name)
    sys.exit(f"no {what} token: set one of {', '.join(names)} in .env")


def call(url, tok=None, body=None):
    headers = {"Content-Type": "application/json"}
    if tok:
        headers["Authorization"] = "Bearer " + tok
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="GET" if body is None else "POST")
    try:
        with OPENER.open(req, timeout=15) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        if exc.code in (401, 403) or body is not None:     # waiting will not change these
            sys.exit(f"{req.get_method()} {url}: HTTP {exc.code} {detail}")
        raise


def wait(what, probe, timeout):
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            found = probe()
            if found:
                return found
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
        time.sleep(5)
    sys.exit(f"no {what} after {timeout:.0f} s" + (f" (last error: {last})" if last else ""))


def incidents(tok):
    return call(API + "/incidents", tok)["incidents"]


step, timeout = sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 600
if step == "incident":
    tok = token(PEOPLE, "people (reviewer, engineer, operator or evaluator)")
    found = wait("incident", lambda: incidents(tok), timeout)
    print(f"{len(found)} incident(s); newest {found[0]['incident_id']}, {found[0]['state']}")
elif step == "brief":
    tok = token(PEOPLE, "people")
    found = wait("brief", lambda: [i for i in incidents(tok) if i["brief"]], timeout)
    i = found[0]
    print(f"brief {i['brief']['brief_id']} on {i['incident_id']}: {i['brief']['evidence_state']}, "
          f"incident {i['state']}")
elif step == "approve":
    tok, reviewer = token(PEOPLE, "people"), token(REVIEWERS, "reviewer")
    rows = incidents(tok)
    waiting = [i for i in rows if i["state"] == "awaiting_review" and i["brief"]]
    if waiting:
        inc = waiting[0]
        out = call(f"{API}/briefs/{inc['brief']['brief_id']}/decisions", reviewer,
                   {"action": "approve", "reason": "tools/verify_local.sh: Journey A on a test stack"})
        print(f"approved {inc['brief']['brief_id']}: decision {out['decision']['decision_id']} "
              f"by {out['decision']['reviewer']}")
    else:
        done = [i for i in rows if i["decision"] and i["decision"]["action"] in ("approve", "edit")]
        if not done:
            sys.exit("no brief awaiting review and none approved before")
        inc = done[0]
        print(f"{inc['incident_id']} was approved before ({inc['decision']['decision_id']}); following that one")
    sim_tok = token(["PDM_TOKEN_CONNECTOR"], "connector")
    orders = wait("order at the simulator",
                  lambda: [o for o in call(SIM + "/api/work-orders", sim_tok)["orders"]
                           if o["order"]["incident_id"] == inc["incident_id"]], timeout)
    sent = wait("work order sent", lambda: [w for w in call(API + "/work-orders", tok)["work_orders"]
                                            if w.get("incident_id") == inc["incident_id"]
                                            and w.get("status") == "sent"], timeout)
    print(f"work order {orders[0]['reference']} at the simulator; {len(sent)} sent per the API")
    print("ASSET " + inc["asset_id"])
elif step == "targets":
    expected_down = set()
    seen = {"down": set()}

    def all_up():
        targets = call(PROM + "/api/v1/targets")["data"]["activeTargets"]
        seen["down"] = {t["labels"]["job"] for t in targets if t["health"] != "up"}
        return targets if targets and not seen["down"] - expected_down else None

    try:
        targets = wait("scrape of every service", all_up, timeout)
    except SystemExit as exc:
        sys.exit(f"{exc}; down: {', '.join(sorted(seen['down'] - expected_down)) or 'no answer from Prometheus'}")
    up = sum(t["health"] == "up" for t in targets)
    print(f"{up} of {len(targets)} targets up; down: {', '.join(sorted(seen['down'])) or 'none'}")
else:
    sys.exit(f"unknown step {step}")
PY
}

# reach SERVICE URL: exit 0 when an HTTP answer (any status) comes back from inside SERVICE
reach() {
  docker compose exec -T "$1" python - "$2" <<'PY'
import sys, urllib.error, urllib.request
try:
    urllib.request.urlopen(sys.argv[1], timeout=8)
except urllib.error.HTTPError:
    pass
except Exception as exc:
    print(f"{type(exc).__name__}: {exc}")
    sys.exit(1)
PY
}

# --- 1. preconditions ---------------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 || { echo "verify_local: docker is not installed" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "verify_local: docker compose is not available" >&2; exit 1; }
if [ ! -f .env ]; then
  echo "verify_local: no .env. Copy .env.example to .env and fill it in (docs/RUNBOOK.md, first-time setup)." >&2
  exit 1
fi

# --- 2. the test suite --------------------------------------------------------------------------------
if [ "$RUN_TESTS" = 1 ]; then
  step "pytest -q"
  if "$PYTHON" -m pytest -q; then pass "test suite"; else fail "test suite"; fi
else
  skip "test suite (--skip-tests)"
fi

# --- 3. the compose file ------------------------------------------------------------------------------
step "docker compose config -q"
if docker compose config -q; then
  pass "compose configuration (with .env)"
else
  fail "compose configuration: fix .env or compose.yaml first"
  finish
fi
[ "$CONFIG_ONLY" = 1 ] && finish

# --- 4. the stack up, every health check passing ------------------------------------------------------
if [ "$LIVE" = 1 ]; then
  export PDM_AGENT_ARGS=""
  echo "agent: the model, with ANTHROPIC_API_KEY from .env"
else
  export PDM_AGENT_ARGS="--scripted services/agent/scripts/dry_run.json"
  echo "agent: the dry-run script (no model); --live-agent uses the model"
fi
step "docker compose up -d --build --wait"
if docker compose up -d --build --wait --wait-timeout "$TIMEOUT"; then
  pass "every service up and healthy"
else
  fail "the stack did not come up healthy"
  docker compose ps -a
  echo "logs: docker compose logs <service>; docs/RUNBOOK.md, where to look" >&2
  finish
fi
docker compose ps

step "observability"
if docker compose exec -T prometheus wget -q -O - http://otel-collector:13133/ >/dev/null; then
  pass "otel-collector answers on :13133 (it has no container health check)"
else
  fail "otel-collector health endpoint :13133"
fi
if api targets 120; then pass "Prometheus scrapes the services' /metrics"; else fail "Prometheus targets"; fi

# --- 5. network rules (FRD section 12) -------------------------------------------------------------------
if [ "$NETWORKS" = 1 ]; then
  step "networks"
  MODEL_API="https://api.anthropic.com/v1/models"
  if reach ingest "$MODEL_API" >/dev/null; then fail "core reaches the internet"; else pass "core has no route off the host"; fi
  if reach api "$MODEL_API" >/dev/null; then fail "north reaches the internet"; else pass "north has no route off the host"; fi
  if reach agent "$MODEL_API"; then pass "egress: the agent reaches the model API"; else fail "the agent cannot reach the model API"; fi
  if docker compose exec -T dashboard python -c "import socket; socket.create_connection(('postgres', 5432), 3)" \
      >/dev/null 2>&1; then
    fail "the dashboard reaches the store directly"
  else
    pass "the dashboard cannot reach the store (only the api)"
  fi
else
  skip "network rules (--check-networks)"
fi

# --- 6. replay -> incident -> brief -------------------------------------------------------------------------
if [ "$REPLAY" = 0 ]; then
  skip "replay, incident and brief (--no-replay)"
  finish
fi
if [ ! -f "$DATA" ]; then
  fail "the dataset is not at $DATA (docs/RUNBOOK.md, first-time setup)"
  finish
fi
step "replay $FROM to $TO at ${SPEED}x"
if docker compose run --rm replayer --speed "$SPEED" --from "$FROM" --to "$TO"; then
  pass "replay $FROM to $TO"
else
  fail "replay"
  finish
fi
step "incident and brief through the API"
if api incident "$TIMEOUT"; then
  pass "an incident through GET /incidents"
else
  fail "no incident: docker compose logs gate detector ingest; a replay from 2020-07-07 gives a full look-back"
  finish
fi
if api brief "$TIMEOUT"; then
  pass "a brief on the incident"
else
  fail "no brief: docker compose logs agent mcp_server api"
fi

# --- 7. Journey A: approval -> work order -> NE 107 over OPC UA ---------------------------------------------------
if [ "$JOURNEY" = 1 ]; then
  step "Journey A: approval, work order, NE 107"
  OUT="$(api approve "$TIMEOUT")"
  STATUS=$?
  echo "$OUT" | grep -v '^ASSET '
  ASSET="$(echo "$OUT" | sed -n 's/^ASSET //p')"
  if [ "$STATUS" = 0 ] && [ -n "$ASSET" ]; then
    pass "approved brief -> work order at the simulator, sent"
    # a client on the north network, where SCADA connects; the simulator's container has asyncua
    if docker compose exec -T workorder-sim python - "$ASSET" <<'PY'
import asyncio, sys
from asyncua import Client

STATES = ("good", "maintenance_required", "out_of_specification", "function_check", "failure")


async def read(asset):
    async with Client("opc.tcp://opcua_north:4840/pdm/", timeout=10) as client:
        idx = await client.get_namespace_index("urn:pdm-agent")
        out = {}
        for name in ("NE107", "NE107Code", "IncidentId", "IncidentState"):
            out[name] = await client.get_node(f"ns={idx};s={asset}.{name}").read_value()
        return out


try:
    values = asyncio.run(read(sys.argv[1]))
except Exception as exc:
    sys.exit(f"OPC UA read failed: {type(exc).__name__}: {exc}")
print("  ".join(f"{k}={v}" for k, v in values.items()))
if values["NE107"] == "function_check":
    print("function_check: no data for 5 min of wall time since the replay ended (ne107.no_data_after)")
sys.exit(0 if values["NE107"] in STATES else 1)
PY
    then
      pass "NE 107 read over OPC UA from the north network"
    else
      fail "NE 107 over OPC UA: docker compose logs opcua_north"
    fi
  else
    fail "approval or work order: docker compose logs api connector workorder-sim"
  fi
else
  skip "Journey A approval, work order and OPC UA (--journey-a)"
fi

finish
