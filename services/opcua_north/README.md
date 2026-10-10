# OPC UA north

Read-only OPC UA server for SCADA: each asset's anomaly score, prognosis and
NE 107 status (T21; INT-01 to INT-03).

```bash
pip install 'asyncua>=1.1'

# demo: no security, anonymous sessions (still read-only)
python services/opcua_north/server.py --profile profiles/metropt3_apu/asset_profile.yaml --anonymous

# secure endpoint: Basic256Sha256 SignAndEncrypt and one username
PDM_OPCUA_USER=scada PDM_OPCUA_PASSWORD=... python services/opcua_north/server.py \
    --profile profiles/metropt3_apu/asset_profile.yaml --cert server_cert.der --key server_key.pem
```

Reads `topics.scores`, `topics.prognosis` (retained) and `topics.status`
(retained) of each profile and publishes nothing back. Endpoint
`opc.tcp://0.0.0.0:4840/pdm/` (`--endpoint`), namespace `urn:pdm-agent`.
`--profile` repeats, one folder per asset. Ctrl-C prints the counts per asset:
scores, prognoses and statuses applied, payloads for other assets, malformed
payloads by reason, NE 107 changes.

## Address space

`Objects/PdM/<asset>/`, one folder per asset, node ids `ns=2;s=<asset>.<Name>`.
Index 2 is `urn:pdm-agent`; clients should look the index up by the URI rather
than hard-code it.

| Name | Type | From | FRD §9 name |
|---|---|---|---|
| Score | Double | score_v1 `score` | Health/AnomalyScore |
| Threshold | Double | score_v1 `threshold` | Health/Threshold |
| Alert | Boolean | score_v1 `alert` | Health/AlertActive |
| P24, P72, P168 | Double | prognosis_v1 `horizons.<h>.p` | Prognosis/P24h, P72h, P168h |
| P24Lower ... P168Upper | Double | prognosis_v1 `horizons.<h>.lower`, `.upper` | Prognosis/P24hLower ... P168hUpper |
| SkillMeetsBar | Boolean | prognosis_v1 `skill.meets_bar` | (added) |
| NE107 | String | status_v1 `ne107` | Status/NE107 |
| NE107Code | Int32 | status_v1 `ne107`, table below | (added) |
| Causes | String | status_v1 `causes`, joined by `; ` | (added) |
| IncidentId | String | status_v1 `incident_id`, empty when none | (added) |
| IncidentState | String | status_v1 `incident_state`, empty when none | Status/IncidentState |
| DetectorRun | String | score_v1 `detector_run_id` | Versions/DetectorRun |
| ModelRun | String | prognosis_v1 `model_run_id` | Versions/SurvivalRun |
| ProfileSha256 | String | `profile_sha256` of the latest payload that carries it | Versions/ProfileHash |
| LastUpdate | DateTime | score_v1 `window_end` | Status/LastUpdate |

The horizons come from the profile's `prognosis.horizons` (24, 72 and 168 h,
the default when a profile declares none). Every value comes from a payload;
the server computes nothing. A payload is applied only if it is valid against
its schema; one for another asset is ignored and counted.

FRD §9 puts these nodes in Health/, Prognosis/, Status/ and Versions/
subfolders. We kept one flat folder per asset, so every node id is
`<asset>.<Name>`, and added NE107Code, Causes, IncidentId and SkillMeetsBar.
The FRD table needs updating to match, or the folders change before anyone
builds SCADA screens on them.

**Timestamps.** The source timestamp is the payload's stream time (the window
end, the prognosis hour, the status time), so in a replay it is 2020. The
server timestamp is wall time. Until a payload sets it, a variable reads
`BadWaitingForInitialData`; the same goes for a horizon missing from the
latest prognosis. Prognosis and status are retained topics, so they are
there right after start-up; Score waits for the next window.

### NE107Code

| NE107 | NE107Code | NE 107 signal |
|---|---|---|
| good | 0 | none |
| maintenance_required | 1 | M |
| out_of_specification | 2 | S |
| function_check | 3 | C |
| failure | 4 | F |

Higher means more severe, in the order `pdm_common.status` applies when
several conditions hold. A SCADA alarm is then one comparison
(`NE107Code >= 3`) and never disagrees with the gate about which state wins.
tests/test_opcua_north.py checks the codes against that order. This is not
the numbering of `DeviceHealthEnumeration` in the OPC UA Devices companion
specification (OPC 10000-100), which as we read it is NORMAL 0, FAILURE 1,
CHECK_FUNCTION 2, OFF_SPEC 3, MAINTENANCE_REQUIRED 4; a client built for DI
DeviceHealth has to map the codes.

## Browsing and subscribing from SCADA

1. Connect to `opc.tcp://<host>:4840/pdm/` using the host's name or address.
   The server listens on 0.0.0.0 and some clients copy that address from the
   endpoint description, which they cannot reach. In the demo, choose security
   None and an anonymous login.
2. Read the namespace array, find `urn:pdm-agent`, and browse
   Objects → PdM → `<asset>`.
3. Subscribe to the variables you need, typically NE107Code, Score and P72.
   Values change on every score (one per window), every status (one per
   score) and every prognosis (one per stream hour).

In Node-RED (node-red-contrib-opcua, the simulated SCADA of R2b.6) this is an
OPC UA client node in SUBSCRIBE mode with a node id such as
`ns=2;s=metropt3_apu.NE107Code` as its topic. We have not tried that flow yet.

## Read-only guarantees (INT-02)

1. Every PdM variable has AccessLevel and UserAccessLevel set to CurrentRead
   only and both write masks set to 0.
2. The server adds no methods.
3. No session gets the Admin role. asyncua lets an Admin write whatever the
   access levels say, and its default user manager makes any login named
   `admin` an Admin. The server turns that off (`allow_remote_admin(False)`)
   and installs its own user manager (`ReadOnlyUsers`): anonymous sessions in
   the demo, one username otherwise, both plain users.
4. A permission ruleset (`ReadOnlyRuleset`) passes only the session, browse,
   read and subscription requests in `ALLOWED_REQUESTS`, for every session.
   Write, HistoryUpdate, Call (the standard server methods such as
   GetMonitoredItems included), AddNodes, DeleteNodes, AddReferences,
   DeleteReferences, RegisterServer and the query services get
   `BadUserAccessDenied` before they reach the address space, and the refusal
   is logged.
5. At start-up the server reads those four attributes back from every PdM
   variable and walks the PdM folders for methods. It refuses to start if
   anything is writable or callable, or if the installed asyncua has no
   permission rulesets.

The negative tests are in tests/test_opcua_north.py. The model refuses every
write and call, and that test runs anywhere. With asyncua installed, a
client's `write_value` on Score is refused, and so are Call, AddNodes and
DeleteNodes, while browse, read and subscribe work.

One consequence: a client that calls GetMonitoredItems or ResendData to
recover after a reconnect is refused, and has to recreate its monitored items
instead.

## Security

- `--anonymous`: security policy None, anonymous sessions. For the demo only;
  FRD §9 has anonymous access off outside development. Still read-only.
- Otherwise: only Basic256Sha256 with SignAndEncrypt, using the certificate
  and key given (`--cert`, `--key`), and the one login in `PDM_OPCUA_USER` /
  `PDM_OPCUA_PASSWORD`. This is the FRD's proposed setting. Without all four
  the server will not start.

## Threads

asyncua runs on its own asyncio loop in a background thread (`ServerThread`).
MQTT (`run_service`) keeps the main thread, because it installs the SIGINT and
SIGTERM handlers and Python allows that only in the main thread. paho calls
the handlers on its network thread, and `NorthService` updates the model
there. The model passes each update to the server's loop with
`call_soon_threadsafe`, and a single task writes the updates in arrival
order, so asyncua objects are only used from their own loop. On Ctrl-C,
`run_service` returns, the server stops and the counts print.

## Not verified here

asyncua could not be installed where this was written, so:

- The asyncua binding has not run against asyncua.
  `test_the_asyncua_server_serves_read_only_nodes` is skipped here. It has to
  pass on a laptop (`pip install 'asyncua>=1.1'`, then
  `pytest tests/test_opcua_north.py`) before the demo relies on the server.
  The binding was run against a stand-in that records calls. That checks the
  threading, the update order, the start-up self-check and the shutdown, not
  asyncua's API.
- These parts of asyncua's API are used as we know them from asyncua 1.x:
  `DataValue(StatusCode_=...)`, `set_security_policy(..., permission_ruleset=)`,
  `Server(user_manager=)` and `iserver.user_manager`. If the laptop test fails,
  look at these first.
- The secure endpoint (certificate, key, username login) has never been run,
  and no test covers it.
- No SCADA client (Node-RED, Ignition) has browsed the server yet. That is the
  R2b.6 check.
- The DI numbering quoted above is from our reading of OPC 10000-100 and was
  not checked against the specification here.
