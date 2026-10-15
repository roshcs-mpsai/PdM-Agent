"""The dashboard's client for the PdM-Agent HTTP API (T18, T19, T34; HIL-01 to HIL-04, HIL-07 to HIL-09).

The dashboard reads and writes only through the API (services/api), with the token of the person
signed in. This module has one method per endpoint the dashboard uses. The token goes in the
``Authorization: Bearer`` header of each request and nowhere else: not in a URL, a log line, an
error message or the client's repr.

A refused request raises ``ApiError`` with the HTTP status and the API's own message:

* 401: the token was not accepted (sign in again);
* 403: the signed-in role may not do this;
* other 4xx: the request was refused, e.g. a decision without a reason (422) or on an incident
  that has moved on (409);
* 5xx: the API failed;
* no answer at all (connection refused, timeout): status None.

``client`` takes any httpx-style client (``request(method, url, ...)``), such as starlette's
TestClient, so the tests run this module against the real API in process.

    api = ApiClient("http://localhost:8000", token)     # base URL default: $PDM_API_URL
    api.me()                                            # {"role": "reviewer", "name": "rosh"}
"""
from __future__ import annotations

import os
from typing import Optional
from urllib.parse import quote

import httpx

DEFAULT_URL = "http://localhost:8000"
DEFAULT_TIMEOUT_S = 10.0


class ApiError(Exception):
    """A request the API refused or did not answer; ``status`` is the HTTP status or None."""

    def __init__(self, status: Optional[int], message: str):
        self.status, self.message = status, message
        super().__init__(f"{message} (HTTP {status})" if status else message)


def http_client(timeout: float):
    """The HTTP client used when none is passed in."""
    return httpx.Client(timeout=timeout)


def _path(value: str) -> str:
    return quote(str(value), safe="")


def _refusal(status: int, text: str) -> str:
    if status == 401:
        return f"the API did not accept the token; sign in again ({text})"
    if status == 403:
        return f"not allowed for your role: {text}"
    if status >= 500:
        return f"the API failed: {text}"
    return text


class ApiClient:
    """The API at ``base_url`` (default $PDM_API_URL, then http://localhost:8000) as the holder of
    ``token``. ``timeout``: seconds per request (default $PDM_API_TIMEOUT, then 10) for the client
    this class makes; a client passed in keeps its own."""

    def __init__(self, base_url: Optional[str] = None, token: str = "", *, timeout: Optional[float] = None,
                 client=None):
        self.base_url = (base_url or os.environ.get("PDM_API_URL") or DEFAULT_URL).rstrip("/")
        self.timeout = float(timeout if timeout is not None else os.environ.get("PDM_API_TIMEOUT", DEFAULT_TIMEOUT_S))
        self._token = token or ""
        self._http = client if client is not None else http_client(self.timeout)

    def __repr__(self) -> str:
        return f"ApiClient({self.base_url!r})"

    def _call(self, method: str, path: str, *, params: Optional[dict] = None, body: Optional[dict] = None):
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            r = self._http.request(method, self.base_url + path, params=params or None, json=body, headers=headers)
        except httpx.TimeoutException:
            raise ApiError(None, f"the API at {self.base_url} did not answer within {self.timeout:g} s") from None
        except httpx.HTTPError as exc:
            raise ApiError(None, f"cannot reach the API at {self.base_url} ({type(exc).__name__})") from None
        try:
            doc = r.json()
        except ValueError:
            doc = None
        if r.status_code >= 400:
            text = doc.get("error") if isinstance(doc, dict) and doc.get("error") else (r.text or "").strip()[:300]
            raise ApiError(r.status_code, _refusal(r.status_code, text or "no message"))
        if not isinstance(doc, dict):
            raise ApiError(r.status_code, f"{method} {path} did not answer with a JSON object")
        return doc

    # -- sign-in and the read views ------------------------------------------------------------------
    def health(self) -> dict:
        return self._call("GET", "/health")

    def me(self) -> dict:
        """Who the token belongs to: {"role", "name"} (HIL-07)."""
        return self._call("GET", "/me")

    def assets(self) -> dict:
        """{"assets": [...]}: highest P(72 h) first (HIL-01)."""
        return self._call("GET", "/assets")

    def asset(self, asset_id: str) -> dict:
        return self._call("GET", f"/assets/{_path(asset_id)}")

    def series(self, asset_id: str, start: Optional[str] = None, end: Optional[str] = None) -> dict:
        """Scores against the threshold and prognoses with bounds; default the last 7 days of stream time."""
        return self._call("GET", f"/assets/{_path(asset_id)}/series", params={"from": start, "to": end})

    def incidents(self, *, state: Optional[str] = None, asset_id: Optional[str] = None,
                  open_only: bool = False) -> dict:
        return self._call("GET", "/incidents", params={"state": state, "asset_id": asset_id,
                                                       "open": "1" if open_only else None})

    def incident(self, incident_id: str) -> dict:
        return self._call("GET", f"/incidents/{_path(incident_id)}")

    def result(self, result_id: str) -> dict:
        return self._call("GET", f"/results/{_path(result_id)}")

    def work_orders(self, status: Optional[str] = None) -> dict:
        return self._call("GET", "/work-orders", params={"status": status})

    def decision(self, decision_id: str) -> dict:
        """The decision rebuilt from the store (HIL-08)."""
        return self._call("GET", f"/decisions/{_path(decision_id)}")

    def trace(self, trace_id: str) -> dict:
        return self._call("GET", f"/traces/{_path(trace_id)}")

    # -- the reviewer's decision ----------------------------------------------------------------------
    def decide(self, brief_id: str, body: dict) -> dict:
        """POST /briefs/{id}/decisions with {"action", "reason"[, "work_order"]} (views.decision_body)."""
        return self._call("POST", f"/briefs/{_path(brief_id)}/decisions", body=body)

    # -- the study (T34) -------------------------------------------------------------------------------
    def study_start(self, consent: bool) -> dict:
        return self._call("POST", "/study/sessions", body={"consent": consent})

    def study_sessions(self) -> dict:
        return self._call("GET", "/study/sessions")

    def study_next(self, session_id: str) -> dict:
        return self._call("GET", f"/study/sessions/{_path(session_id)}/next")

    def study_answer(self, session_id: str, body: dict) -> dict:
        """POST {"order_no", "rating", "decision"} (views.study_answer_body)."""
        return self._call("POST", f"/study/sessions/{_path(session_id)}/answers", body=body)

    def study_results(self) -> dict:
        return self._call("GET", "/study/results")
