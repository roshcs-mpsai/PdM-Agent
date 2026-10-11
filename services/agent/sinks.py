"""Where the agent's records go (T16, T20; INC-05, INC-06, BRF-02, AGT-11).

The runner records five things: its claim on the incident (open -> diagnosing, once: INC-06),
the hand-back when the model is unreachable (diagnosing -> open), each brief it submits
(validated and stored by pdm_common.diagnosis.accept_brief, which also moves the incident to
awaiting_review), a checkpoint after every step, and the trace at the end.

* ``DirectSink`` writes them to the store: the single-process pipeline and the tests.
* ``ApiSink`` posts them to the HTTP API with the agent's token, so that in the deployed
  stack the agent writes to the database only through the API (FRD section 9). The API runs
  the same functions on its side.
"""
from __future__ import annotations

from typing import Optional

from pdm_common.diagnosis import (accept_brief, claim, outage_tries, release, save_checkpoint, stale_runs,
                                  unfinished)


class DirectSink:
    """The agent's records, straight into the store."""

    def __init__(self, store):
        self.store = store

    def claim(self, incident_id: str, trace_id: str) -> bool:
        return claim(self.store, incident_id, trace_id)

    def release(self, incident_id: str, trace_id: str, cause: str) -> bool:
        return release(self.store, incident_id, trace_id, cause)

    def submit(self, brief: dict, *, trace_id: str, arm: str, move: bool = True) -> list[str]:
        return accept_brief(self.store, brief, trace_id=trace_id, arm=arm, move=move)

    def checkpoint(self, state: dict) -> None:
        save_checkpoint(self.store, state)

    def trace(self, row: dict) -> None:
        self.store.insert("agent_traces", row)

    def unfinished(self, asset_id: Optional[str] = None) -> list[dict]:
        return unfinished(self.store, asset_id)

    def outage_tries(self, incident_id: str) -> list[dict]:
        return outage_tries(self.store, incident_id)

    def stale_runs(self, asset_id: str, older_than_s: float) -> list[dict]:
        return stale_runs(self.store, asset_id, older_than_s)

    def open_incidents(self, asset_id: str) -> list[str]:
        return [r["incident_id"] for r in self.store.query(
            "SELECT incident_id FROM incidents WHERE asset_id = ? AND state = ? ORDER BY opened_at",
            [asset_id, "open"])]


class SinkError(RuntimeError):
    """The API refused or could not be reached."""


class ApiSink:
    """The agent's records, posted to the HTTP API (services/api) with the agent's token.
    ``client``: an httpx.Client (tests pass starlette's TestClient)."""

    def __init__(self, base_url: str, token: str, *, client=None, timeout_s: float = 30.0):
        import httpx
        if not token:
            raise ValueError("the API needs the agent's token (PDM_TOKEN_AGENT)")
        self.base = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=timeout_s)
        self._headers = {"Authorization": f"Bearer {token}"}

    def _post(self, path: str, body: dict, ok=(200, 201)) -> dict:
        r = self.client.post(f"{self.base}{path}", json=body, headers=self._headers)
        if r.status_code not in ok and r.status_code != 422:
            raise SinkError(f"POST {path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json() if r.content else {}

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        r = self.client.get(f"{self.base}{path}", params=params or {}, headers=self._headers)
        if r.status_code != 200:
            raise SinkError(f"GET {path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def claim(self, incident_id: str, trace_id: str) -> bool:
        return bool(self._post(f"/incidents/{incident_id}/claim", {"trace_id": trace_id}).get("claimed"))

    def release(self, incident_id: str, trace_id: str, cause: str) -> bool:
        return bool(self._post(f"/incidents/{incident_id}/release", {"trace_id": trace_id, "cause": cause})
                    .get("released"))

    def submit(self, brief: dict, *, trace_id: str, arm: str, move: bool = True) -> list[str]:
        doc = self._post(f"/incidents/{brief.get('incident_id')}/briefs",
                         {"brief": brief, "trace_id": trace_id, "arm": arm, "move": move})
        return list(doc.get("problems") or [])

    def checkpoint(self, state: dict) -> None:
        self._post(f"/agent/runs/{state['trace_id']}/checkpoint", {"state": state})

    def trace(self, row: dict) -> None:
        self._post(f"/agent/runs/{row['trace_id']}/trace", {"trace": row})

    def unfinished(self, asset_id: Optional[str] = None) -> list[dict]:
        return self._get("/agent/runs/unfinished", {"asset_id": asset_id} if asset_id else None)["runs"]

    def outage_tries(self, incident_id: str) -> list[dict]:
        return self._get(f"/agent/incidents/{incident_id}/outages")["runs"]

    def stale_runs(self, asset_id: str, older_than_s: float) -> list[dict]:
        return self._get("/agent/runs/stale", {"asset_id": asset_id, "older_than_s": older_than_s})["runs"]

    def open_incidents(self, asset_id: str) -> list[str]:
        doc = self._get("/incidents", {"state": "open", "asset_id": asset_id})
        return [i["incident_id"] for i in doc["incidents"]]
