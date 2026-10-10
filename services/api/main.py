"""The HTTP API: the one way into the store for the dashboard and the agent (T20; HIL-01 to HIL-08,
INT-05, FRD section 9).

    python services/api/main.py [--store "$PDM_STORE"] [--host 0.0.0.0 --port 8000]

Starlette and uvicorn (FastAPI, which the FRD names, is not installable in the build
environment; the routes are the FRD's). Tokens and roles: services/api/auth.py.

| Method and path | Roles | |
|---|---|---|
| GET /health | none | liveness (the metrics are on port 9107, on the core network only) |
| GET /me | any | who the token belongs to (the dashboard's sign-in) |
| GET /assets | people | HIL-01: status, P(24/72/168 h) with bounds, open incidents; by P(72 h) |
| GET /assets/{id} | people | HIL-02: the summary, the latest alert's attribution, open incidents |
| GET /assets/{id}/series | people | scores against the threshold, prognoses with bounds (?from=&to=) |
| GET /incidents | people | the queue (?state=&asset_id=&open=1) |
| GET /incidents/{id} | people | HIL-03: the incident, the current brief with every cited result, history |
| GET /results/{id} | people | one stored tool result |
| GET /work-orders | people | INT-04: work orders with status, attempts and last error (?status=) |
| POST /briefs/{id}/decisions | reviewer | approve, edit or reject, with a reason (HIL-05) |
| GET /decisions/{id} | people | HIL-08: the decision rebuilt from the store, integrity checked |
| GET /traces/{id} | engineer, operator, evaluator | the agent's full trace (AGT-11) |
| POST /incidents/{id}/claim, /release | agent | the agent's claim and hand-back (INC-06) |
| POST /incidents/{id}/briefs | agent | a brief: validated, stored, the incident moved (BRF-02) |
| POST /agent/runs/{trace}/checkpoint, /trace | agent | the run's checkpoint and its trace |
| GET /agent/runs/unfinished, /agent/incidents/{id}/outages | agent | what the agent resumes and retries |

"People" is reviewer, engineer, operator and evaluator. No route creates a work order except a
decision (HIL-06).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import Response  # noqa: E402
from starlette.routing import Match, Route  # noqa: E402

import auth  # noqa: E402
import decisions  # noqa: E402
import queries  # noqa: E402
from pdm_common import logs, metrics  # noqa: E402
from pdm_common import diagnosis as agent_side  # noqa: E402
from pdm_common.diagnosis import accept_brief  # noqa: E402
from pdm_common.hashing import canonical_json  # noqa: E402

log = logging.getLogger("api")
PEOPLE = auth.PEOPLE
AUDITORS = ("engineer", "operator", "evaluator")
BODY_MAX = 2_000_000


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def _json(doc, status: int = 200) -> Response:
    return Response(canonical_json(doc), status_code=status, media_type="application/json")


def skill_reader(root: Path = ROOT) -> Callable[[str], Optional[dict]]:
    """The frozen survival model's Brier skill against a constant hazard, per asset, shown next to
    every prognosis (it is below the 0.15 bar)."""
    cache: dict = {}

    def skill(asset: str) -> Optional[dict]:
        if "value" not in cache:
            cache["value"] = None
            try:
                from pdm_common.frozen import load_frozen
                frozen = load_frozen(root / "eval" / "config.yaml", root)
                metrics_ = (frozen.survival_bundle or {}).get("metrics") or {}
                by_h = {k[len("skill_"):]: v for k, v in metrics_.items() if k.startswith("skill_")}
                mean = sum(by_h.values()) / len(by_h) if by_h else None
                cache["value"] = (frozen.config.get("asset"),
                                  {"brier_skill_by_horizon": by_h, "mean": mean, "bar": 0.15,
                                   "meets_bar": bool(mean is not None and mean >= 0.15)})
            except Exception as exc:              # no freeze in this checkout: no skill shown
                log.warning("no survival skill to show: %s", exc)
        value = cache["value"]
        return value[1] if value and value[0] == asset else None
    return skill


def create_app(store=None, tokens: Optional[dict] = None, *, skill_for=None, extra_routes=()) -> Starlette:
    """The API over ``store`` (default: $PDM_STORE). ``tokens``: {token: Principal}
    (default: from the environment; the API refuses to start without valid ones)."""
    if store is None:
        from pdm_common.store import open_store
        store = open_store()
    tokens = auth.load_tokens() if tokens is None else tokens
    skill_for = skill_for or skill_reader()
    m = metrics.service_metrics("api")

    def who(request: Request, roles) -> auth.Principal:
        principal = auth.authenticate(request.headers.get("authorization"), tokens)
        if principal is None:
            raise ApiError(401, "authentication required")
        if principal.role not in roles:
            raise ApiError(403, f"the {principal.role} role may not use this endpoint")
        return principal

    async def body(request: Request) -> dict:
        raw = await request.body()
        if len(raw) > BODY_MAX:
            raise ApiError(413, "the body is too large")
        try:
            import json
            doc = json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(422, "the body is not JSON")
        if not isinstance(doc, dict):
            raise ApiError(422, "the body is not a JSON object")
        return doc

    def route(path: str, methods: list[str], roles):
        def wrap(fn):
            async def endpoint(request: Request) -> Response:
                principal = who(request, roles) if roles else None
                return await fn(request, principal)
            return Route(path, endpoint, methods=methods, name=fn.__name__)
        return wrap

    # -- open --------------------------------------------------------------------------------------
    async def health(request: Request) -> Response:
        try:
            store.one("SELECT 1 AS ok")
            return _json({"status": "ok", "service": "api"})
        except Exception as exc:
            return _json({"status": "unavailable", "error": type(exc).__name__}, 503)

    # -- people ------------------------------------------------------------------------------------
    @route("/me", ["GET"], auth.ROLES)
    async def me(request, principal):
        return _json({"role": principal.role, "name": principal.name})

    @route("/assets", ["GET"], PEOPLE)
    async def assets(request, principal):
        return _json({"assets": queries.assets(store, skill_for)})

    @route("/assets/{asset_id}", ["GET"], PEOPLE)
    async def asset(request, principal):
        aid = request.path_params["asset_id"]
        doc = queries.asset_detail(store, aid, skill_for(aid))
        if doc is None:
            raise ApiError(404, f"no asset {aid}")
        return _json(doc)

    @route("/assets/{asset_id}/series", ["GET"], PEOPLE)
    async def asset_series(request, principal):
        q = request.query_params
        try:
            doc = queries.series(store, request.path_params["asset_id"], q.get("from"), q.get("to"))
        except ValueError as exc:
            raise ApiError(422, str(exc))
        return _json(doc)

    @route("/incidents", ["GET"], PEOPLE + ("agent",))
    async def incident_list(request, principal):
        q = request.query_params
        return _json({"incidents": queries.incidents(store, state=q.get("state"), asset_id=q.get("asset_id"),
                                                     open_only=q.get("open") in ("1", "true"))})

    @route("/incidents/{incident_id}", ["GET"], PEOPLE)
    async def incident(request, principal):
        doc = queries.incident_detail(store, request.path_params["incident_id"])
        if doc is None:
            raise ApiError(404, "no such incident")
        return _json(doc)

    @route("/results/{result_id}", ["GET"], PEOPLE)
    async def result(request, principal):
        rid = request.path_params["result_id"]
        found = queries.results(store, [rid])
        if rid not in found:
            raise ApiError(404, f"no result {rid}")
        return _json(found[rid])

    @route("/work-orders", ["GET"], PEOPLE)
    async def work_order_list(request, principal):
        return _json({"work_orders": queries.work_orders(store, request.query_params.get("status"))})

    @route("/briefs/{brief_id}/decisions", ["POST"], ("reviewer",))
    async def decide(request, principal):
        doc = await body(request)
        try:
            out = decisions.decide(store, request.path_params["brief_id"], principal, str(doc.get("action")),
                                   str(doc.get("reason") or ""), doc.get("work_order"))
        except decisions.DecisionError as exc:
            raise ApiError(exc.status, str(exc))
        m.decisions_total.labels(action=out["decision"]["action"]).inc()
        log.info("decision %s on %s by %s: %s", out["decision"]["decision_id"], out["decision"]["brief_id"],
                 principal.name, out["decision"]["action"], extra={"incident_id": out["decision"]["incident_id"]})
        return _json(out, 201)

    @route("/decisions/{decision_id}", ["GET"], PEOPLE)
    async def decision_audit(request, principal):
        doc = decisions.audit(store, request.path_params["decision_id"])
        if doc is None:
            raise ApiError(404, "no such decision")
        return _json(doc)

    @route("/traces/{trace_id}", ["GET"], AUDITORS)
    async def trace(request, principal):
        row = store.one("SELECT * FROM agent_traces WHERE trace_id = ?", [request.path_params["trace_id"]],
                        table="agent_traces")
        if row is None:
            raise ApiError(404, "no such trace")
        return _json(row)

    # -- the agent ---------------------------------------------------------------------------------
    @route("/incidents/{incident_id}/claim", ["POST"], ("agent",))
    async def claim(request, principal):
        doc = await body(request)
        return _json({"claimed": agent_side.claim(store, request.path_params["incident_id"],
                                                  str(doc.get("trace_id") or ""))})

    @route("/incidents/{incident_id}/release", ["POST"], ("agent",))
    async def release(request, principal):
        doc = await body(request)
        return _json({"released": agent_side.release(store, request.path_params["incident_id"],
                                                     str(doc.get("trace_id") or ""), str(doc.get("cause") or ""))})

    @route("/incidents/{incident_id}/briefs", ["POST"], ("agent",))
    async def submit_brief(request, principal):
        doc = await body(request)
        brief = doc.get("brief")
        if not isinstance(brief, dict) or brief.get("incident_id") != request.path_params["incident_id"]:
            raise ApiError(422, "the body's brief is not for this incident")
        problems = accept_brief(store, brief, trace_id=str(doc.get("trace_id") or ""), arm=str(doc.get("arm")),
                                move=bool(doc.get("move", True)))
        if problems:
            m.brief_rejections_total.labels(stage="api").inc()
            return _json({"accepted": False, "problems": problems}, 422)
        return _json({"accepted": True, "brief_id": brief.get("brief_id"), "problems": []}, 201)

    @route("/agent/runs/{trace_id}/checkpoint", ["POST"], ("agent",))
    async def checkpoint(request, principal):
        doc = await body(request)
        state = doc.get("state")
        if not isinstance(state, dict) or state.get("trace_id") != request.path_params["trace_id"]:
            raise ApiError(422, "the checkpoint is not for this run")
        if not all(isinstance(state.get(k), str) for k in ("incident_id", "arm", "node")):
            raise ApiError(422, "a checkpoint names its incident, arm and step")
        if store.one("SELECT 1 AS ok FROM incidents WHERE incident_id = ?", [state["incident_id"]]) is None:
            raise ApiError(404, f"no incident {state['incident_id']}")
        agent_side.save_checkpoint(store, state)
        return _json({"saved": True})

    @route("/agent/runs/{trace_id}/trace", ["POST"], ("agent",))
    async def save_trace(request, principal):
        doc = await body(request)
        row = doc.get("trace")
        if not isinstance(row, dict) or row.get("trace_id") != request.path_params["trace_id"]:
            raise ApiError(422, "the trace is not for this run")
        if not all(isinstance(row.get(k), str) for k in ("incident_id", "arm", "outcome", "started_at")):
            raise ApiError(422, "a trace names its incident, arm, outcome and start")
        if store.one("SELECT 1 AS ok FROM agent_traces WHERE trace_id = ?", [row["trace_id"]]) is not None:
            raise ApiError(409, f"the trace of {row['trace_id']} is already stored")
        store.insert("agent_traces", row)
        return _json({"saved": True}, 201)

    @route("/agent/runs/unfinished", ["GET"], ("agent",))
    async def unfinished(request, principal):
        return _json({"runs": agent_side.unfinished(store, request.query_params.get("asset_id"))})

    @route("/agent/incidents/{incident_id}/outages", ["GET"], ("agent",))
    async def outages(request, principal):
        return _json({"runs": agent_side.outage_tries(store, request.path_params["incident_id"])})

    routes = [Route("/health", health), me, assets, asset, asset_series,
              incident_list, incident, result, work_order_list, decide, decision_audit, trace, claim, release,
              submit_brief, checkpoint, save_trace, unfinished, outages, *extra_routes]

    async def errors(request: Request, exc: ApiError) -> Response:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return Response(canonical_json({"error": exc.message, **exc.extra}), status_code=exc.status,
                        media_type="application/json", headers=headers)

    app = Starlette(routes=routes, exception_handlers={ApiError: errors})
    app.state.store = store
    app.state.tokens = tokens

    async def measured(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        template = next((r.path for r in app.routes if r.matches(scope)[0] == Match.FULL), "unmatched")
        status = [500]

        async def send_status(message):
            if message["type"] == "http.response.start":
                status[0] = message["status"]
            await send(message)
        started = time.perf_counter()
        try:
            await inner(scope, receive, send_status)
        finally:
            m.http_requests_total.labels(route=template, method=scope["method"], code=str(status[0])).inc()
            m.http_request_seconds.labels(route=template, method=scope["method"]).observe(
                time.perf_counter() - started)
    inner = app.build_middleware_stack()
    app.middleware_stack = measured
    return app


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--store", default=None, help="store URL (default $PDM_STORE or sqlite:///runs/pdm.db)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logs.setup("api")
    from pdm_common.store import open_store
    try:
        tokens = auth.load_tokens()
    except auth.AuthError as exc:
        raise SystemExit(f"api: {exc} (docs/SECURITY.md)")
    app = create_app(open_store(args.store), tokens)
    server = metrics.serve(metrics.REGISTRY, metrics.port_for("api"), os.environ.get("PDM_METRICS_HOST", "127.0.0.1"),
                           service="api")
    import uvicorn
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_config=None)
    finally:
        if server is not None:
            server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
