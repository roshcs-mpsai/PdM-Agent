"""Simulated work-order system -- the downstream REST endpoint for the connector (PR-19 test bed, T31).

Stands in for the plant's maintenance system so the REST connector
(services/connector; INT-04 to INT-06) can be built and tested against a fixed
contract. It needs only Starlette and uvicorn, not pdm_common, because it
plays an outside system.

    POST /api/work-orders   Authorization: Bearer <token>
                            Idempotency-Key: <approval_id>
                            JSON body with at least approval_id, wo_id, incident_id,
                            asset_id, title, priority, tasks (list of strings),
                            safety_notes (list of strings), text_sha256 (64 lowercase
                            hex digits), approved_by, approved_at (ISO 8601 with an
                            offset or Z); other fields are kept as sent
      201  first request for the key:   {"reference": "WO-SIM-0001", "received_at": "<UTC ISO>"}
      200  same key, same body:         the same reference and received_at
                                        (header Idempotent-Replayed: true)
      409  same key, different body
      401  missing or wrong token
      422  no Idempotency-Key, a missing or mistyped field, a body that is not
           a JSON object, or an Idempotency-Key other than the body's approval_id
      503  the first N POSTs when failure injection is on: nothing is stored
    GET  /api/work-orders   (token needed) {"orders": [...]} in the order received
    GET  /health            open: {"status": "ok"}

"Same body" means the same JSON content: key order and whitespace do not
matter. Checks run in this order: injected failure, token, Idempotency-Key,
body, key against approval_id, idempotency.

Configuration, by argument to ``create_app`` or from the environment:
WORKORDER_SIM_TOKEN (default "sim-token"), WORKORDER_SIM_FAIL_FIRST (default
0) and WORKORDER_SIM_STORE (a JSON file the orders are kept in and reloaded
from; default: memory only). Tests use ``create_app`` with
starlette.testclient.TestClient, no server needed. A lost response is
simulated by sending the same order twice: the second gets 200 and the same
reference, and only one order is listed.

    python tools/workorder_sim.py --port 8088                  # or:
    uvicorn --app-dir tools --factory workorder_sim:create_app --port 8088
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
from pathlib import Path
from typing import Callable, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

log = logging.getLogger("workorder_sim")

STRING_FIELDS = ("approval_id", "wo_id", "incident_id", "asset_id", "title", "priority", "approved_by")
LIST_FIELDS = ("tasks", "safety_notes")
REQUIRED = STRING_FIELDS + LIST_FIELDS + ("text_sha256", "approved_at")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _utc_iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(body: dict) -> str:
    """The body's content hash: sorted keys, no whitespace."""
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def problems(body) -> list[str]:
    """Why a body is not a work order; [] when it is one."""
    if not isinstance(body, dict):
        return ["the body must be a JSON object"]
    found = [f"{name}: missing" for name in REQUIRED if name not in body]
    for name in STRING_FIELDS:
        if name in body and (not isinstance(body[name], str) or not body[name].strip()):
            found.append(f"{name}: must be a non-empty string")
    for name in LIST_FIELDS:
        if name in body and (not isinstance(body[name], list) or not all(isinstance(x, str) for x in body[name])):
            found.append(f"{name}: must be a list of strings")
    if "text_sha256" in body and not (isinstance(body["text_sha256"], str) and SHA256_HEX.match(body["text_sha256"])):
        found.append("text_sha256: must be 64 lowercase hex digits (SHA-256 of the approved text)")
    if "approved_at" in body:
        stamp = body["approved_at"]
        try:
            moment = dt.datetime.fromisoformat(stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp)
            if moment.tzinfo is None:
                found.append("approved_at: needs an offset or Z (UTC)")
        except (TypeError, ValueError, AttributeError):
            found.append("approved_at: must be an ISO 8601 time")
    return found


class OrderBook:
    """Received orders in arrival order, by idempotency key; optionally kept in a JSON file."""

    def __init__(self, path: Optional[str | Path] = None):
        self.path = Path(path) if path else None
        self.orders: list[dict] = []
        if self.path is not None and self.path.exists():
            self.orders = json.loads(self.path.read_text())["orders"]
        self.by_key = {o["approval_id"]: o for o in self.orders}

    def add(self, body: dict, received_at: str) -> dict:
        entry = {"reference": f"WO-SIM-{len(self.orders) + 1:04d}", "received_at": received_at,
                 "approval_id": body["approval_id"], "body_sha256": _digest(body), "order": body}
        self.orders.append(entry)
        self.by_key[entry["approval_id"]] = entry
        if self.path is not None:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps({"orders": self.orders}, indent=2, sort_keys=True) + "\n")
            tmp.replace(self.path)
        return entry


def create_app(token: Optional[str] = None, fail_first: Optional[int] = None, *,
               store_path: Optional[str | Path] = None,
               clock: Optional[Callable[[], dt.datetime]] = None) -> Starlette:
    """The simulator as a Starlette app. Arguments left as None come from the environment."""
    token = token if token is not None else os.environ.get("WORKORDER_SIM_TOKEN", "sim-token")
    fail_first = fail_first if fail_first is not None else int(os.environ.get("WORKORDER_SIM_FAIL_FIRST", "0"))
    store_path = store_path if store_path is not None else os.environ.get("WORKORDER_SIM_STORE") or None
    clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
    if not token:
        raise ValueError("the simulator needs a non-empty token")
    book = OrderBook(store_path)
    posts = [0]

    def authorized(request: Request) -> bool:
        scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and bool(credentials) and \
            hmac.compare_digest(credentials.strip().encode(), token.encode())

    def error(status: int, message: str, **extra) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        return JSONResponse({"error": message, **extra}, status_code=status, headers=headers)

    async def post_order(request: Request) -> JSONResponse:
        posts[0] += 1
        if posts[0] <= fail_first:
            log.info("POST %d of %d failing on purpose: 503", posts[0], fail_first)
            return error(503, f"injected failure {posts[0]} of {fail_first}; nothing was stored")
        if not authorized(request):
            return error(401, "missing or wrong bearer token")
        key = request.headers.get("idempotency-key", "").strip()
        if not key:
            return error(422, "the Idempotency-Key header is required (the approval_id)")
        try:
            body = json.loads(await request.body())
        except ValueError:
            return error(422, "the body is not JSON")
        found = problems(body)
        if found:
            return error(422, "not a work order", problems=found)
        if key != body["approval_id"]:
            return error(422, f"Idempotency-Key {key!r} is not the body's approval_id {body['approval_id']!r}")
        existing = book.by_key.get(key)
        if existing is not None:
            if existing["body_sha256"] != _digest(body):
                return error(409, f"approval {key} was already received with a different body",
                             reference=existing["reference"])
            return JSONResponse({"reference": existing["reference"], "received_at": existing["received_at"]},
                                status_code=200, headers={"Idempotent-Replayed": "true"})
        entry = book.add(body, _utc_iso(clock()))
        log.info("work order %s: %s (approval %s, incident %s, priority %s)", entry["reference"], body["title"],
                 key, body["incident_id"], body["priority"])
        return JSONResponse({"reference": entry["reference"], "received_at": entry["received_at"]}, status_code=201)

    async def work_orders(request: Request) -> JSONResponse:
        if request.method == "POST":
            return await post_order(request)
        if not authorized(request):
            return error(401, "missing or wrong bearer token")
        return JSONResponse({"orders": [{k: o[k] for k in ("reference", "received_at", "approval_id", "order")}
                                        for o in book.orders]})

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app = Starlette(routes=[Route("/api/work-orders", work_orders, methods=["GET", "POST"]),
                            Route("/health", health, methods=["GET"])])
    app.state.book = book
    return app


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8088)
    p.add_argument("--fail-first", type=int, default=None, help="answer the first N POSTs with 503")
    p.add_argument("--store", default=None, help="keep the orders in this JSON file")
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    import uvicorn

    uvicorn.run(create_app(fail_first=args.fail_first, store_path=args.store), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
