"""How the agent reaches its tools (T16; AGT-01 to AGT-05).

Two connections with the same four operations: the tool specs of an arm, ``open`` (the run of
one trace on one incident), ``context`` (the stored incident_context result, the run's first)
and ``call`` (one tool call, returning the stored result, or ToolRefused).

* ``InProcessTools`` runs the tool server's code in this process (services/mcp_server/tools.py):
  the single-process pipeline, the tests, and a deployment without the MCP container.
* ``McpTools`` is an MCP client of services/mcp_server/server.py (streamable HTTP with the
  agent's bearer token, or an MCPServer object in process for tests). It reads the context
  as the server's resource and calls the six tools by name.

The tool specs the model sees come from tools.py either way, so the model never sees the
incident_id, trace_id and arm parameters the MCP transport adds.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
MCP_DIR = ROOT / "services" / "mcp_server"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))
_loaded = sys.modules.get("tools")
if _loaded is not None and Path(getattr(_loaded, "__file__", "") or "").resolve() != MCP_DIR / "tools.py":
    raise ImportError(f"another module named 'tools' is loaded ({_loaded!r}); the agent needs "
                      f"services/mcp_server/tools.py")
import tools as toolserver  # noqa: E402

ToolRefused = toolserver.ToolRefused
CONTEXT_TOOL = toolserver.CONTEXT_TOOL
log = logging.getLogger("agent")


class InProcessTools:
    """The tool server's code, in this process."""

    def __init__(self, server: "toolserver.ToolServer"):
        self.server = server

    def specs(self, arm: str) -> list[dict]:
        return toolserver.tool_specs(arm)

    def open(self, incident_id: str, trace_id: str, arm: str):
        return self.server.open_run(incident_id, trace_id, arm)

    def context(self, run) -> dict:
        return self.server.context(run)

    def call(self, run, tool: str, arguments: dict) -> dict:
        return self.server.call(run, tool, arguments)

    def close(self) -> None:
        pass


@dataclass
class RemoteRun:
    """A run held by the MCP server, as the client sees it."""
    incident: dict
    trace_id: str
    arm: str
    token: str
    tools: tuple
    results: dict = field(default_factory=dict)


class McpTools:
    """An MCP client of the tool server. ``url``: the streamable-HTTP endpoint (…/mcp); or
    ``server``: an MCPServer object, connected in process (tests)."""

    def __init__(self, url: Optional[str] = None, token: Optional[str] = None, *, server=None,
                 timeout_s: float = 60.0):
        if (url is None) == (server is None):
            raise ValueError("give the tool server's url or an MCPServer object, not both")
        self.url, self.server, self.timeout_s = url, server, timeout_s
        self._token = token
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="mcp-client", daemon=True)
        self._thread.start()
        self._requests: Optional[asyncio.Queue] = None
        self._ready = concurrent.futures.Future()
        self._session = asyncio.run_coroutine_threadsafe(self._serve(), self._loop)
        self._ready.result(timeout=timeout_s)          # raises if the server cannot be reached

    # -- one session, in its own task, serving requests from other threads ----------------------------
    def _client(self):
        from mcp.client.client import Client
        if self.server is not None:
            return Client(self.server)
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else None
        return Client(streamable_http_client(self.url, http_client=create_mcp_http_client(headers=headers)))

    async def _serve(self) -> None:
        self._requests = asyncio.Queue()
        try:
            async with self._client() as client:
                self._ready.set_result(True)
                while True:
                    fn, done = await self._requests.get()
                    if fn is None:
                        break
                    try:
                        done.set_result(await fn(client))
                    except Exception as exc:              # handed to the calling thread
                        done.set_exception(exc)
        except Exception as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
            raise

    def _ask(self, fn):
        done: concurrent.futures.Future = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(self._requests.put_nowait, (fn, done))
        return done.result(timeout=self.timeout_s)

    # -- the four operations -------------------------------------------------------------------------
    def specs(self, arm: str) -> list[dict]:
        return toolserver.tool_specs(arm)

    def open(self, incident_id: str, trace_id: str, arm: str) -> RemoteRun:
        uri = f"pdm://incidents/{incident_id}/runs/{trace_id}/{arm}/context"
        read = self._ask(lambda c: c.read_resource(uri))
        body = json.loads(read.contents[0].text)
        if "error" in body:
            raise ToolRefused(body["error"])
        run = RemoteRun({"incident_id": incident_id}, trace_id, arm, body["result_id"].split("-")[1],
                        tuple(body["tools"]))
        run.results[body["result_id"]] = self._result(run, body)
        return run

    def context(self, run: RemoteRun) -> dict:
        return run.results[f"R-{run.token}-1"]

    def call(self, run: RemoteRun, tool: str, arguments: dict) -> dict:
        if tool not in run.tools:
            log.warning("refused %s: not a tool of the %s arm", tool, run.arm,
                        extra={"incident_id": run.incident["incident_id"], "trace_id": run.trace_id})
            raise ToolRefused(f"{tool} is not a tool of this run; the tools are {', '.join(run.tools)}")
        args = dict(arguments or {}, incident_id=run.incident["incident_id"], trace_id=run.trace_id, arm=run.arm)
        answer = self._ask(lambda c: c.call_tool(tool, args))
        texts = [b.text for b in answer.content if getattr(b, "type", "") == "text"]
        try:
            body = json.loads(texts[0]) if texts else {}
        except ValueError:
            body = {"error": texts[0][:300]}
        if getattr(answer, "is_error", False) or "error" in body:
            raise ToolRefused(body.get("error") or (texts[0][:300] if texts else f"{tool} failed"))
        result = self._result(run, body)
        run.results[result["result_id"]] = result
        return result

    @staticmethod
    def _result(run: RemoteRun, body: dict) -> dict:
        return {"schema": "tool_result_v1", "result_id": body["result_id"], "incident_id": run.incident["incident_id"],
                "tool": body["tool"], "output": body["output"]}

    def close(self) -> None:
        if self._requests is not None and self._session is not None and not self._session.done():
            self._loop.call_soon_threadsafe(self._requests.put_nowait, (None, None))
            try:
                self._session.result(timeout=10)
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
