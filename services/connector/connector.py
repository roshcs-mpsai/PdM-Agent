"""Connector -- send approved work orders to the maintenance system's REST endpoint (T22; INT-04 to INT-06, HIL-06).

Every few seconds it takes the work_orders rows that are due (status queued,
or failed and waiting to retry, with next_attempt_at reached), oldest first.
For each row it:

1. checks the approval and the text (pdm_common.workorders.send_problems): a
   decision row exists, approves (approve or edit), is for the row's incident,
   the incident is approved (HIL-06), and the stored body still carries the
   text the reviewer approved, by hash (INT-06); and the order is for the
   incident's asset. Any problem blocks the row: status blocked, the reasons in
   last_error, an error in the log (the alert) and
   pdm_work_orders_blocked_total{reason}. Nothing is sent;
2. claims the row by compare-and-set on its status and next_attempt_at, moving
   next_attempt_at a lease ahead: a second connector that read the same row
   finds it changed and skips it, and if this one dies mid-request the row is
   due again when the lease ends;
3. POSTs the stored body with ``Authorization: Bearer <token>`` (INT-05) and
   ``Idempotency-Key: <decision id>`` (INT-04), then:

   * 2xx: sent, with the endpoint's reference; the incident moves approved ->
     sent (actor connector) in the same transaction. A 200 the endpoint marks
     as a replay is a request it had already received (a lost response): the
     same order, counted as a duplicate;
   * 409: blocked, conflict. The endpoint holds a different body for this
     approval; the connector never resends it on its own;
   * 401, 403, 422 and any other 4xx except 408 and 429: blocked,
     rejected_by_endpoint, with the status and the endpoint's error;
   * 5xx, 408, 429, a timeout or no connection: failed, retried after 5, 10,
     20 ... s (at most 300 s); blocked after 8 attempts.

The token comes from $PDM_WORKORDER_TOKEN; without it the connector does not
start. It never goes to the log, to last_error or to the report.

    PDM_WORKORDER_TOKEN=<token> python services/connector/connector.py [--store "$PDM_STORE"] \\
        [--endpoint http://localhost:8088/api/work-orders] [--interval 5] [--once]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import signal
import sys
import threading
from collections import Counter
from typing import Callable, Optional
from urllib.parse import urlsplit

import httpx

from pdm_common import incidents as lifecycle
from pdm_common import logs, metrics
from pdm_common.hashing import canonical_json
from pdm_common.timeutil import to_utc
from pdm_common.workorders import BLOCKED, DUE_STATUSES, FAILED, SENT, problem_reason, send_problems

log = logging.getLogger("connector")

ACTOR = "connector"
DEFAULT_ENDPOINT = "http://localhost:8088/api/work-orders"
TOKEN_VARS = ("PDM_WORKORDER_TOKEN", "PDM_TOKEN_CONNECTOR")    # the second is docs/SECURITY.md's name
TIMEOUT_S = 10.0
LEASE_S = 60.0                     # a claimed row is due again after this long, if its connector died
OUTCOMES = ("sent", "duplicate", "retry", "failed")
BLOCK_REASONS = ("no_approval", "tampered", "conflict", "rejected_by_endpoint")
RETRY_CODES = (408, 429)           # with every 5xx
RUN_KEYS = ("due", "sent", "duplicate", "retry", "failed", "blocked", "skipped", "error")
LOOPBACK = ("localhost", "127.0.0.1", "::1")


def default_client(timeout: float = TIMEOUT_S) -> httpx.Client:
    """The HTTP client used when none is given."""
    return httpx.Client(timeout=timeout)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _reference(response) -> Optional[str]:
    """The endpoint's reference from a JSON answer, if it gave one."""
    try:
        doc = response.json()
    except ValueError:
        return None
    ref = doc.get("reference") if isinstance(doc, dict) else None
    return None if ref is None else str(ref)


class Connector:
    """The service minus its loop: due work_orders rows in, POSTs out, rows and incidents updated.

    ``client``: anything with httpx.Client's ``post`` (tests pass starlette's
    TestClient); ``clock``: returns the current UTC time."""

    def __init__(self, store, endpoint_url: str, token: str, *, client=None,
                 clock: Optional[Callable[[], dt.datetime]] = None, max_attempts: int = 8,
                 backoff_s: float = 5, backoff_max_s: float = 300):
        if not token:
            raise ValueError("the connector needs the endpoint's bearer token (INT-05)")
        if not isinstance(endpoint_url, str) or not endpoint_url.startswith(("http://", "https://")):
            raise ValueError(f"the endpoint must be an http or https URL, not {endpoint_url!r}")
        if max_attempts < 1 or backoff_s < 0 or backoff_max_s < 0:
            raise ValueError("max_attempts must be at least 1 and the backoffs at least 0")
        self.store = store
        self.endpoint_url = endpoint_url
        self._token = token
        self._own_client = client is None
        self.client = default_client() if client is None else client
        self.clock = clock or _utc_now
        self.max_attempts = int(max_attempts)
        self.backoff_s = backoff_s
        self.backoff_max_s = backoff_max_s
        self.stopping = threading.Event()
        self.counts: Counter = Counter({key: 0 for key in ("runs",) + RUN_KEYS})
        self.blocked_by: Counter = Counter()
        self.pending = 0
        self.m = metrics.service_metrics("connector")
        for outcome in OUTCOMES:
            self.m.work_order_posts_total.labels(outcome=outcome)
        for reason in BLOCK_REASONS:
            self.m.work_orders_blocked_total.labels(reason=reason)

    # -- the run ----------------------------------------------------------------------------------
    def run_once(self, now=None) -> dict:
        """Process every due row, oldest first. Returns this run's counts (due, then one count
        per outcome) and the number of rows still queued or failed (pending)."""
        now = self._now(now)
        rows = self.due(now)
        counts = Counter({key: 0 for key in RUN_KEYS})
        counts["due"] = len(rows)
        for row in rows:
            if self.stopping.is_set():
                break
            try:
                outcome = self.process(row, now)
            except Exception:                      # one bad row must not stop the others
                log.exception("work order %s: unexpected error; it is due again when its lease ends",
                              row.get("wo_id"), extra={"incident_id": row.get("incident_id")})
                outcome = "error"
            counts[outcome] += 1
        self.counts.update(counts)
        self.counts["runs"] += 1
        result = dict(counts)
        result["pending"] = self.update_pending()
        return result

    def due(self, now=None) -> list[dict]:
        """Rows queued or failed whose next_attempt_at has come (or is not set), oldest first."""
        from pdm_common.store import _to_db
        now = self._now(now)
        return self.store.query(
            "SELECT * FROM work_orders WHERE status IN (?, ?) AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
            "ORDER BY next_attempt_at IS NOT NULL, next_attempt_at, wo_id",
            [*DUE_STATUSES, _to_db("work_orders", "next_attempt_at", now, self.store.dialect)], table="work_orders")

    def process(self, row: dict, now=None) -> str:
        """One due row: block it, send it or schedule a retry. Returns sent, duplicate, retry,
        failed (gave up), blocked, or skipped (the row changed under this connector)."""
        now = self._now(now)
        incident = self._incident(row.get("incident_id"))
        problems = send_problems(row, self._decision(row.get("decision_id")),
                                 incident["state"] if incident else None)
        if incident is not None and row.get("asset_id") != incident.get("asset_id"):
            problems.append(f"tampered: the work order is for asset {row.get('asset_id')}, but incident "
                            f"{row.get('incident_id')} is on {incident.get('asset_id')}.")
        if problems:
            return self._block(row, problem_reason(problems[0]), "; ".join(problems), now)
        lease = now + dt.timedelta(seconds=LEASE_S)
        if not self._cas(row, {"next_attempt_at": lease, "updated_at": now}):
            return self._taken(row)
        claimed = dict(row, next_attempt_at=lease)
        attempts = int(row.get("attempts") or 0) + 1
        try:
            response = self.client.post(self.endpoint_url, content=canonical_json(row["body"]).encode("utf-8"),
                                        headers=self._headers(row["decision_id"]))
        except (httpx.HTTPError, OSError) as exc:          # timeout, refused connection, broken answer
            return self._retry(claimed, attempts, self._scrub(f"{type(exc).__name__}: {exc}"), now)
        code = response.status_code
        if 200 <= code < 300:
            return self._sent(claimed, attempts, response, now)
        error = self._describe(response)
        if code >= 500 or code in RETRY_CODES:
            return self._retry(claimed, attempts, error, now)
        self.m.work_order_posts_total.labels(outcome="failed").inc()
        return self._block(claimed, "conflict" if code == 409 else "rejected_by_endpoint", error, now,
                           attempts=attempts)

    # -- outcomes ---------------------------------------------------------------------------------
    def _sent(self, claimed: dict, attempts: int, response, now: dt.datetime) -> str:
        replayed = response.headers.get("idempotent-replayed", "").strip().lower() == "true"
        outcome = "duplicate" if replayed else "sent"
        self.m.work_order_posts_total.labels(outcome=outcome).inc()
        reference = _reference(response)
        wo_id, incident_id = claimed["wo_id"], claimed.get("incident_id")
        with self.store.transaction():
            if not self._cas(claimed, {"status": SENT, "attempts": attempts, "reference": reference, "sent_at": now,
                                       "next_attempt_at": now, "last_error": None, "updated_at": now}):
                return self._taken(claimed)
            incident = self._incident(incident_id)
            if incident is not None and incident["state"] == "approved":
                try:
                    lifecycle.transition(self.store, incident_id, "sent", ACTOR,
                                         f"work order {wo_id} sent, reference {reference or 'not given'}",
                                         at=now, expect="approved")
                except lifecycle.IllegalTransition as exc:
                    log.error("work order %s is sent but incident %s did not move to sent: %s", wo_id, incident_id,
                              exc, extra={"incident_id": incident_id, "wo_id": wo_id})
        log.info("work order %s sent: reference %s, attempt %d%s", wo_id, reference, attempts,
                 " (the endpoint already had it)" if replayed else "",
                 extra={"incident_id": incident_id, "wo_id": wo_id, "outcome": outcome})
        return outcome

    def _retry(self, claimed: dict, attempts: int, error: str, now: dt.datetime) -> str:
        wo_id, incident_id = claimed["wo_id"], claimed.get("incident_id")
        if attempts >= self.max_attempts:
            self.m.work_order_posts_total.labels(outcome="failed").inc()
            if not self._cas(claimed, {"status": BLOCKED, "attempts": attempts, "next_attempt_at": now,
                                       "updated_at": now,
                                       "last_error": f"gave up after {attempts} attempts; the last: {error}"}):
                return self._taken(claimed)
            log.error("work order %s blocked: no answer after %d attempts (%s)", wo_id, attempts, error,
                      extra={"incident_id": incident_id, "wo_id": wo_id, "outcome": "failed"})
            return "failed"
        delay = min(self.backoff_s * 2 ** min(attempts - 1, 60), self.backoff_max_s)
        self.m.work_order_posts_total.labels(outcome="retry").inc()
        if not self._cas(claimed, {"status": FAILED, "attempts": attempts, "last_error": error, "updated_at": now,
                                   "next_attempt_at": now + dt.timedelta(seconds=delay)}):
            return self._taken(claimed)
        log.warning("work order %s: attempt %d of %d failed (%s); next attempt in %g s", wo_id, attempts,
                    self.max_attempts, error, delay, extra={"incident_id": incident_id, "wo_id": wo_id})
        return "retry"

    def _block(self, row: dict, reason: str, error: str, now: dt.datetime, attempts: Optional[int] = None) -> str:
        values = {"status": BLOCKED, "last_error": error, "next_attempt_at": now, "updated_at": now}
        if attempts is not None:
            values["attempts"] = attempts
        if not self._cas(row, values):
            return self._taken(row)
        self.m.work_orders_blocked_total.labels(reason=reason).inc()
        self.blocked_by[reason] += 1
        log.error("work order %s blocked (%s): %s", row["wo_id"], reason, error,
                  extra={"incident_id": row.get("incident_id"), "wo_id": row["wo_id"], "reason": reason})
        return "blocked"

    def _taken(self, row: dict) -> str:
        log.info("work order %s changed while this connector handled it (another connector or a person); "
                 "left as it is", row["wo_id"], extra={"incident_id": row.get("incident_id"), "wo_id": row["wo_id"]})
        return "skipped"

    # -- the store --------------------------------------------------------------------------------
    def _cas(self, row: dict, values: dict) -> bool:
        """Compare and set: change the row only while it still has the status and next_attempt_at
        this connector read. False when someone else changed it first."""
        from pdm_common.store import _to_db
        dialect = self.store.dialect
        sets = ", ".join(f"{column} = ?" for column in values)
        params = [_to_db("work_orders", c, v, dialect) for c, v in values.items()] + [row["wo_id"], row["status"]]
        if row.get("next_attempt_at") is None:
            same_time = "next_attempt_at IS NULL"
        else:
            same_time = "next_attempt_at = ?"
            params.append(_to_db("work_orders", "next_attempt_at", row["next_attempt_at"], dialect))
        changed = self.store.execute(f"UPDATE work_orders SET {sets} WHERE wo_id = ? AND status = ? AND {same_time}",
                                     params)
        return changed == 1

    def _decision(self, decision_id) -> Optional[dict]:
        if not decision_id:
            return None
        rows = self.store.select("decisions", {"decision_id": decision_id})
        return rows[0] if rows else None

    def _incident(self, incident_id) -> Optional[dict]:
        if not incident_id:
            return None
        return self.store.one("SELECT incident_id, asset_id, state FROM incidents WHERE incident_id = ?",
                              [incident_id])

    def update_pending(self) -> int:
        """Set pdm_work_orders_pending to the rows queued or failed; returns the number."""
        row = self.store.one("SELECT COUNT(*) AS n FROM work_orders WHERE status IN (?, ?)", list(DUE_STATUSES))
        self.pending = int(row["n"])
        self.m.work_orders_pending.set(self.pending)
        return self.pending

    # -- helpers ----------------------------------------------------------------------------------
    def _now(self, now=None) -> dt.datetime:
        return to_utc(now if now is not None else self.clock())

    def _headers(self, decision_id: str) -> dict:
        return {"Authorization": f"Bearer {self._token}", "Idempotency-Key": decision_id,
                "Content-Type": "application/json"}

    def _describe(self, response) -> str:
        """'HTTP 422 Unprocessable Entity: <the endpoint's answer, shortened>', without the token."""
        text = " ".join((response.text or "").split())
        if len(text) > 300:
            text = text[:300] + " ..."
        head = f"HTTP {response.status_code} {getattr(response, 'reason_phrase', '') or ''}".strip()
        return self._scrub(f"{head}: {text}" if text else head)

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "[token]")

    def stop(self) -> None:
        """Finish the row in hand and take no more (SIGINT, SIGTERM)."""
        self.stopping.set()

    def close(self) -> None:
        if self._own_client:
            self.client.close()

    def report(self) -> dict:
        return dict(self.counts, endpoint=self.endpoint_url, pending=self.pending,
                    blocked_by=dict(sorted(self.blocked_by.items())))


# --- the service ---------------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--store", default=None, help="store URL (default $PDM_STORE or sqlite:///runs/pdm.db)")
    p.add_argument("--endpoint", default=os.environ.get("PDM_WORKORDER_URL") or DEFAULT_ENDPOINT,
                   help=f"the work-order endpoint (default $PDM_WORKORDER_URL or {DEFAULT_ENDPOINT})")
    p.add_argument("--interval", type=float, default=5.0, help="seconds between runs (default 5)")
    p.add_argument("--once", action="store_true", help="one run, print the report and exit")
    args = p.parse_args(argv)
    if args.interval <= 0:
        p.error("--interval must be more than 0 seconds")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    logs.setup("connector")
    token = next((os.environ[name] for name in TOKEN_VARS if os.environ.get(name)), None)
    if not token:
        raise SystemExit("connector: set PDM_WORKORDER_TOKEN to the work-order endpoint's bearer token; "
                         "the connector does not send without one (INT-05)")
    from pdm_common.store import open_store
    try:
        connector = Connector(open_store(args.store), args.endpoint, token)
    except ValueError as exc:
        raise SystemExit(f"connector: {exc}")
    split = urlsplit(args.endpoint)
    if split.scheme == "http" and split.hostname not in LOOPBACK:
        log.warning("the endpoint %s is plain HTTP: the bearer token crosses the network unencrypted", args.endpoint)

    stop = threading.Event()

    def on_signal(signum, frame):
        stop.set()
        connector.stop()

    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, on_signal)
        except ValueError:                     # not the main thread: no handlers, the caller stops us
            break
    healthy = [True]
    server = None if args.once else metrics.serve(
        metrics.REGISTRY, metrics.port_for("connector"), os.environ.get("PDM_METRICS_HOST", "127.0.0.1"),
        service="connector", health=lambda: healthy[0])
    log.info("sending due work orders to %s every %g s (at most %d attempts, backoff %g s to %g s)",
             args.endpoint, args.interval, connector.max_attempts, connector.backoff_s, connector.backoff_max_s)
    try:
        while True:
            try:
                connector.run_once()
                healthy[0] = True
            except Exception:                      # the store went away: report it, try again
                healthy[0] = False
                log.exception("run failed; trying again in %g s", args.interval)
            connector.m.set_up(healthy[0])
            if args.once or stop.wait(args.interval):
                break
    finally:
        for sig, handler in previous.items():
            if handler is not None:
                signal.signal(sig, handler)
        connector.close()
        if server is not None:
            server.close()
    print(json.dumps(connector.report(), sort_keys=True))
    return 0 if healthy[0] or not args.once else 1


if __name__ == "__main__":
    sys.exit(main())
