# Security: broker accounts, TLS, tokens and secrets

R3 hardening, task T2 (NFR-06, ESR; build guide R3.1 and R3.2). The R3 exit checklist asks
for two things this document covers: the broker refuses anonymous clients and enforces
ACLs, and every endpoint takes a token while secrets stay out of git. NFR-06 adds least
privilege and no write path to plant systems.

| Piece | File |
|---|---|
| Secure broker config (accounts, ACLs, TLS, persistence) | `mosquitto/config/mosquitto.secure.conf` |
| R0 development broker (anonymous, no TLS), unchanged | `mosquitto/config/mosquitto.conf` |
| Per-service topic rights | `mosquitto/config/acl` |
| Broker passwords and the password file | `tools/broker_users.py` |
| Local CA and broker certificate | `tools/make_certs.sh` |
| Client settings every service reads | `lib/pdm_common/broker.py`, used by `pdm_common.mqtt` |
| Secret rules and the scanner | `.gitleaks.toml`, `tools/secret_scan.py` |
| Tests | `tests/test_broker_security.py`, `tests/test_secrets.py` |

## Broker accounts and ACLs

`mosquitto.secure.conf` sets `allow_anonymous false`, a `password_file` and an `acl_file`.
Every service logs in with its own account, and the ACL gives that account exactly the
topics the service uses, nothing more. In the topics below `+` is one level, the asset id.

| Account | Program | Reads | Writes |
|---|---|---|---|
| `replayer` | services/replayer | | `pdm/+/telemetry` |
| `south` | services/south: Sparkplug B and OPC UA adapters | `spBv1.0/#` | `pdm/+/telemetry` |
| `scada_sim` | services/south/scada_sim.py (test bed only) | | `spBv1.0/#` |
| `ingest` | services/ingest | `pdm/+/telemetry` | `pdm/+/windows` |
| `detector` | services/detector | `pdm/+/windows` | `pdm/+/scores` |
| `prognostics` | services/prognostics | `pdm/+/windows`, `pdm/+/scores` | `pdm/+/prognosis` |
| `gate` | services/gate | `pdm/+/scores`, `pdm/+/windows`, `pdm/+/prognosis`, `pdm/_sys/drift` | `pdm/+/incidents`, `pdm/+/status` |
| `drift` | services/drift | `pdm/+/telemetry` | `pdm/_sys/drift` |
| `agent` | services/agent | `pdm/+/incidents` | |
| `opcua_north` | services/opcua_north | `pdm/+/scores`, `pdm/+/prognosis`, `pdm/+/status` | |
| `south-cip` | services/south/cip.py (runs as `south-cip`) | | `pdm/+/device` |
| `mcp_server` | services/mcp_server | `pdm/+/device` | |
| `dashboard` | services/dashboard (status view) | `pdm/+/status` | |
| `healthcheck` | the compose health check of the broker | `$SYS/broker/version` | |

How Mosquitto applies this: an account may publish only where a `write` line matches and
receives only messages on topics a `read` line matches; anything else is refused or
silently dropped. There are no rules for clients without an account (and they cannot
connect anyway). `tests/test_broker_security.py` evaluates the file for every account
against every kind of topic, read and write.

No write path to the plant: only `scada_sim`, the simulated plant, can publish under
`spBv1.0/`. The `south` account reads the Sparkplug namespace and writes telemetry, so a
command (NCMD, DCMD) or a STATE message from it is dropped by the broker. That is the second
line behind the adapters' own publish guard (ING-15). A real site has no `scada_sim` account.

The account names are the names the programs use: `run_service("ingest", ...)` logs in as
`ingest`, `run_service("south-cip", ...)` as `south-cip`. A test fails if a service started
through `run_service` has no account.

Known gap: client ids are not tied to accounts. Any account can connect with another
service's client id (`pdm-ingest`) and so disconnect it. Mosquitto's dynamic-security plugin
can bind client ids to accounts; that is not done here.

### How a service finds its account

`pdm_common.broker.client_options(service)` reads the environment:

| Variable | Meaning |
|---|---|
| `PDM_MQTT_HOST` | broker host; replaces the program's `--broker` when set |
| `PDM_MQTT_PORT` | broker port; replaces `--port` when set |
| `PDM_MQTT_USER_<SERVICE>`, `PDM_MQTT_PASSWORD_<SERVICE>` | that service's account |
| `PDM_MQTT_USER`, `PDM_MQTT_PASSWORD` | the account when the per-service pair is not set |
| `PDM_MQTT_TLS_CA` | the CA certificate; setting it turns TLS on, and the port becomes 8883 unless `PDM_MQTT_PORT` is set |

`<SERVICE>` is the account name in capitals with `-` turned into `_`: `PDM_MQTT_USER_SOUTH_CIP`.
`run_service` and `read_retained` apply all of this (`pdm_common.mqtt._client`). With none
of the variables set nothing changes: the R0 broker on localhost:1883 without a login.

- From a shell, load `.env` (`set -a; . ./.env; set +a`) and start any service; each one
  picks its own account from the per-service variables.
- In a container, give the service only its own account through the generic pair:
  `PDM_MQTT_USER: ingest` and `PDM_MQTT_PASSWORD: ${PDM_MQTT_PASSWORD_INGEST:?...}`. Do not
  hand every container the whole `.env` with `env_file`.
- The status view (`read_retained`) has no service name, so it uses the generic pair:
  `PDM_MQTT_USER=dashboard PDM_MQTT_PASSWORD="$PDM_MQTT_PASSWORD_DASHBOARD" python services/dashboard/status_view.py`.

The environment wins over `--broker` and `--port` because the services default those
flags to localhost and 1883, and a default cannot be told apart from a value given on purpose.

## TLS

The secure config has two listeners:

- **8883, MQTT over TLS.** Everything outside the compose network uses it: other machines,
  the plant side, and programs run on the host itself. TLS 1.2 is the minimum. Clients
  check the broker's certificate against our CA and the host name against the certificate.
  There are no client certificates; clients authenticate with their account.
- **1883, plain MQTT, for the compose network only.** Inside the container Mosquitto
  listens on the loopback and on the container's network interface. compose.yaml publishes
  8883 and not 1883, so 1883 cannot be reached from the host or the LAN, and its traffic
  never leaves the Docker host. Accounts and ACLs apply on 1883 exactly as on 8883.

`tools/make_certs.sh` writes a local CA (`ca.key`, `ca.crt`) and the broker certificate
(`server.key`, `server.crt`) into `mosquitto/certs/`: P-256 ECDSA, SHA-256, the CA valid for
five years and the broker certificate for one. The broker certificate names `localhost`,
`127.0.0.1`, `::1` and `mosquitto` (the compose service name). A client on another machine
connects by the host's LAN name or address, which must be in the certificate too:
`tools/make_certs.sh DNS:pdm-laptop.local IP:192.168.1.20`.

A client trusts `ca.crt`:

```bash
export PDM_MQTT_HOST=localhost PDM_MQTT_TLS_CA=mosquitto/certs/ca.crt      # port 8883 follows
mosquitto_sub -h localhost -p 8883 --cafile mosquitto/certs/ca.crt -u dashboard -P "$PDM_MQTT_PASSWORD_DASHBOARD" -t 'pdm/+/status' -v
```

## Running the secure broker

```bash
python tools/broker_users.py          # passwords into .env (generated where missing), hashes into mosquitto/config/passwd
tools/make_certs.sh                   # mosquitto/certs/
python tools/broker_users.py --check  # every ACL account has a password entry that matches .env
docker compose up -d mosquitto
```

compose.yaml runs the R0 broker. `mosquitto/compose.secure.yaml` replaces it with this one
when both files are used: put `COMPOSE_FILE=compose.yaml:mosquitto/compose.secure.yaml` in
`.env` and every `docker compose` command reads both. The override publishes 8883 with TLS
on 127.0.0.1 (`PDM_BROKER_BIND` to open it), mounts `mosquitto.secure.conf`, the ACL,
`passwd` and the certificates read-only, keeps the broker's data on a volume, logs the
health check in over TLS, and gives each service its own broker account. Notes on it:

- Every mount is read-only. The image's entrypoint hands everything under `/mosquitto` to
  the mosquitto user when it starts, which would otherwise change the owner of these files
  on the host.
- `passwd` and the certificates use the long mount syntax so that compose stops with an
  error when they are missing. The short syntax would create an empty directory in their
  place and the broker would fail later with a less clear message.
- The broker reads `passwd` and the keys as root when it starts, so mode 0600 is fine.
  Recent Mosquitto versions warn when these files are not owned by the user the broker runs
  as; that warning is expected with bind mounts. Mosquitto 2.1 also warns that
  `password_file` and `acl_file` are deprecated (they are supported until 3.0).
- The health check logs in as `healthcheck` over TLS, so it also catches an expired or
  wrong certificate.
- To go back to the R0 broker (for example for the e2e tests), mount `mosquitto.conf`
  again and publish 1883.

Persistence is on (`/mosquitto/data`, saved every minute): retained status, prognosis and
drift messages survive a broker restart, and so do the sessions of clients that connect with
clean session off, with up to 10,000 queued QoS 1 messages each, kept for 7 days. The
services still connect with clean sessions (paho's default), so a restarted service does not
get what it missed; R3.6 (QoS 1 persistent sessions for the consumers) needs
`clean_session=False` in `pdm_common.mqtt._client` and stable client ids.

## Where secrets live

None of these are in git. `.env` is git-ignored; `mosquitto/config/passwd` and
`mosquitto/certs/` need their `.gitignore` lines (see the end of this document).

| Secret | Where | Written by |
|---|---|---|
| Broker passwords | `.env`: `PDM_MQTT_PASSWORD_<SERVICE>` | `tools/broker_users.py` (generated: 256 random bits) |
| Broker password hashes | `mosquitto/config/passwd` | `tools/broker_users.py` |
| CA and broker private keys | `mosquitto/certs/*.key` | `tools/make_certs.sh` |
| Database password | `.env`: `PG_PASSWORD` | by hand |
| HTTP API and connector tokens | `.env`: `PDM_TOKEN_<ROLE>` | by hand (below) |
| OPC UA logins | `.env`: `PDM_OPCUA_PASSWORD`, `PDM_SOUTH_OPCUA_PASSWORD` | by hand |
| Model API keys | `.env` or Colab secrets: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | by hand |

`.env` should be mode 0600 (`broker_users.py` sets it). `.env.example` is in git and holds
placeholders only.

`tools/broker_users.py` writes Mosquitto's own format, `<account>:$7$<iterations>$<salt>$<hash>`:
PBKDF2-HMAC-SHA512 over the password with a 12-byte random salt, 1000 iterations (the
default of current mosquitto_passwd; Mosquitto 2.0 used 101), salt and 64-byte hash in
standard base64. Mosquitto 2.1 writes 64-byte salts that a 2.0 broker rejects; 12-byte salts
work with both. The broker runs PBKDF2 once per connection on its single thread, which is why
the count stays modest; the generated passwords are random, so their strength does not depend
on it. A password set by hand needs 16 or more characters from `A-Z a-z 0-9 . _ ~ + / = -`, so
compose and the shell read `.env` the same way.

### Secret scanning

`.gitleaks.toml` holds this repository's rules: Mosquitto password hashes, Anthropic and
OpenAI keys, MLflow and Databricks tokens, bearer tokens, `PDM_*` passwords and tokens with a
value, PostgreSQL passwords other than the `change-me` placeholder, passwords inside
database and broker URLs, private keys, and secret-only files (`.env`, `mosquitto/config/passwd`,
`*.key`). Placeholders (`change-me`, `<token>`, `${VAR}`, compose's `${VAR:?message}`) and
`tests/fixtures/` are allowed.

- `python tools/secret_scan.py` applies those rules to every file git tracks, with no
  gitleaks binary; CI should run it (the step is at the end of this document). `--staged`
  checks what is about to be committed:
  `printf '#!/bin/sh\nexec python3 tools/secret_scan.py --staged\n' > .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit`.
- With gitleaks installed, `gitleaks git .` reads the same file, adds gitleaks' built-in
  rules and scans the history too.
- A false positive: put `gitleaks:allow` in a comment on that line.
- A real secret that reached a commit: rotate it first (anything pushed is public), then
  remove it.

## Tokens for the HTTP API and the connector

The connector (services/connector) presents `PDM_WORKORDER_TOKEN`, or
`PDM_TOKEN_CONNECTOR` when that is unset, to the work-order endpoint and does not
start without one. The HTTP API (services/api) works as follows:

- Every request carries `Authorization: Bearer <token>`. Tokens come from the environment,
  at least 32 random characters each, one per role or one per person:

  | Role | Variables | Presented by | Allowed (FRD section 9) |
  |---|---|---|---|
  | reviewer | `PDM_TOKEN_REVIEWER`, `PDM_TOKEN_REVIEWER_<NAME>` | the dashboard, for a reviewer | reads; `POST /briefs/{id}/decisions` |
  | engineer | `PDM_TOKEN_ENGINEER`, `PDM_TOKEN_ENGINEER_<NAME>` | the dashboard | reads, the audit view, traces |
  | operator | `PDM_TOKEN_OPERATOR`, `PDM_TOKEN_OPERATOR_<NAME>` | the dashboard and tools | reads, the audit view, traces |
  | evaluator | `PDM_TOKEN_EVALUATOR`, `PDM_TOKEN_EVALUATOR_<NAME>` | the study (T34) | reads, the audit view, traces |
  | agent | `PDM_TOKEN_AGENT` | services/agent; the MCP tool server checks it too | the agent's endpoints: claim, release, briefs, checkpoints, traces |
  | connector | `PDM_TOKEN_CONNECTOR` | services/connector, to the work-order endpoint | checked by the work-order endpoint; grants nothing on our API |

- A named token (`PDM_TOKEN_REVIEWER_ROSH`) is how a person signs in: the dashboard asks
  for the token, `GET /me` names its holder, and every decision records that name
  (HIL-07). A role token without a name records the role.
- The API compares the presented token with every known token using
  `hmac.compare_digest`. No token or an unknown one: 401 with `WWW-Authenticate: Bearer`. A
  known token on an endpoint its role may not use: 403.
- The API refuses to start when no token is set, when a token is shorter than 32
  characters, or when two variables hold the same token.
- Tokens never appear in logs, traces or error messages.
- `/health` and `/metrics` take no token; they are reachable from the core network only (FRD).
- Make a token with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- The work-order simulator checks `WORKORDER_SIM_TOKEN`; in the demo stack set it to the
  value of `PDM_TOKEN_CONNECTOR`.

## Rotation

| What | Steps |
|---|---|
| One broker password | Delete its `PDM_MQTT_PASSWORD_<SERVICE>` line from `.env`, run `python tools/broker_users.py` (a new password is generated), `docker compose restart mosquitto`, then restart that service with the new `.env`. |
| All broker passwords | Delete every `PDM_MQTT_PASSWORD_*` line, then the same steps for every service. |
| Broker certificate (yearly, or a new name) | `tools/make_certs.sh [extra names]`, then `docker compose restart mosquitto`. Same CA, so clients need nothing. |
| The CA | Delete `mosquitto/certs/ca.key` and `ca.crt`, run `tools/make_certs.sh`, restart the broker, and give the new `ca.crt` to every client (`PDM_MQTT_TLS_CA`). |
| An API or connector token | New value in `.env`, restart the API and whatever presents the token (dashboard, agent, connector; for the connector also the work-order endpoint). |
| `PG_PASSWORD` | `POSTGRES_PASSWORD` applies only when the volume is created. Change it inside the database (`docker compose exec postgres psql -U pdm -d pdm`, then `\password pdm`), then in `.env`, and restart the services. |

Restart the broker rather than sending it SIGHUP: after start-up it runs as the mosquitto
user and may not be able to read the 0600 files again, and Mosquitto 2.1 stops when a reload
fails.

## Checks on a laptop with Docker

```bash
set -a; . ./.env; set +a
TLS="-h localhost -p 8883 --cafile mosquitto/certs/ca.crt"

# no account: refused ("not authorised")
mosquitto_sub $TLS -t '#' -C 1 -W 3

# ingest cannot publish scores; detector can. Watch as opcua_north, which reads scores:
mosquitto_sub $TLS -u opcua_north -P "$PDM_MQTT_PASSWORD_OPCUA_NORTH" -t 'pdm/+/scores' -v &
mosquitto_pub $TLS -u ingest -P "$PDM_MQTT_PASSWORD_INGEST" -t pdm/metropt3_apu/scores -m from-ingest      # not delivered
mosquitto_pub $TLS -u detector -P "$PDM_MQTT_PASSWORD_DETECTOR" -t pdm/metropt3_apu/scores -m from-detector  # delivered

# the south account cannot command the plant: watch as south itself
mosquitto_sub $TLS -u south -P "$PDM_MQTT_PASSWORD_SOUTH" -t 'spBv1.0/#' -v &
mosquitto_pub $TLS -u south -P "$PDM_MQTT_PASSWORD_SOUTH" -t spBv1.0/pdm_testbed/NCMD/scada_sim -m x       # not delivered

# 1883 is not reachable from the host
mosquitto_sub -h localhost -p 1883 -t '#' -C 1 -W 3                                                        # connection refused

# secrets
python tools/secret_scan.py
```

## What was not run here

The work was done where Docker, Mosquitto, paho-mqtt and gitleaks are not available.

- No broker has loaded `mosquitto.secure.conf`, the ACL or a generated `passwd`. The ACL
  was checked with an evaluator written from Mosquitto's documented rules, and the `$7$`
  entries were re-derived with hashlib, not compared with mosquitto_passwd output or
  accepted by a broker.
- The certificates were made with OpenSSL 3.0 and checked in a TLS handshake between
  Python's ssl client and server, not between paho and Mosquitto. LibreSSL (macOS) was not
  tried.
- `run_service` and `read_retained` ran against a fake paho module.
- gitleaks itself was not run; its built-in rules may flag things `tools/secret_scan.py`
  does not.
- The secure broker's compose override (mosquitto/compose.secure.yaml), its health check and
  the laptop checks have not been run.
- The lines this needs elsewhere are in place: `.gitignore` keeps `mosquitto/config/passwd`
  and `mosquitto/certs/` out of git, and CI runs `python tools/secret_scan.py` first.
