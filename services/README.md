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
| opcua_north | Northbound OPC UA server, NE 107 | R2 |
| connector | REST work-order connector | R2 |

Rule: nothing in here names a MetroPT signal — asset facts come from
`profiles/` (enforced by `tests/test_profiles.py`).
