# Services

One directory per component; each becomes a container in `compose.yaml` as it
lands (build order and owners are in the construction plan).

| Directory | Component | Release |
|---|---|---|
| replayer | Deterministic stream replay to MQTT (1x–10x) | R0 |
| ingest | Schema validation, quarantine, windowing | R0 stub, R1 |
| detector | Common scoring interface: PCA, z-score, USAD, AE | R1 |
| prognostics | Discrete-time survival models | R1 |
| gate | Persistence gate | R2 |
| mcp_server | Six-tool MCP server + checks dispatch | R2 |
| agent | LangGraph loop, brief schema | R2 |
| dashboard | Streamlit operator dashboard + approval store | R0 stub, R2 |
| opcua_north | Read-only northbound OPC UA server: score, prognosis with bounds, NE 107, incident, versions | R2 |
| connector | REST work-order connector | R2 |
| drift | Per-signal drift checks against the healthy reference every 6 stream hours; candidate requests | R3 |
| south | Southbound adapters: Sparkplug B and OPC UA client to telemetry_json_v1, the simulated SCADA; read-only CIP client (G8 stretch) | R2; R4 stretch |

Rule: nothing in here names a MetroPT signal — asset facts come from
`profiles/` (enforced by `tests/test_profiles.py`).
