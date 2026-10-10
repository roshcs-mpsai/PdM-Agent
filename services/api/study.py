"""The blinded three-arm study (T34; HIL-09, PR-16, NFR-09, ESR-05): sessions for reviewers and the
results for evaluators, added to the API with ``create_app(..., extra_routes=routes(get_store, tokens))``.

Three arms per incident:

* ``raw``: the alert alone: the asset, the opening, the gate's cause, the scores of the 30 windows
  up to the opening against the threshold, and the checks the gate ran at the opening; no brief;
* ``retrieval_only``: the retrieval-only agent's brief (``agent.py --study INC-... --arm
  retrieval_only``), stored next to the live brief and never shown on the review screens;
* ``full``: the agent's live brief.

A session covers every incident with a valid brief in both agent arms. Consent comes first: without
``{"consent": true}`` nothing is stored. The incidents are shuffled (a new order per session) and
the arms dealt so each gets a third of them, the remainder going to arms drawn at random, in shuffled
order. Each arm is shown under a label, A, B or C, mapped at random per session. Items come one at a
time; a brief is shown without what would reveal its arm or its model (the brief id, the versions,
the trace), and the result ids it cites become E1, E2, ... in its text and evidence. The participant
rates each item from 1 (not useful) to 5 (very useful) and says what they would do (act, dismiss,
unsure). The seconds from the first showing of an item to its answer are measured here, not in the
browser; showing an item again (a page reload) does not restart the clock. An item shows the latest
valid brief of its arm at the time it is shown.

Participants are reviewers signed in with their own token (``PDM_TOKEN_REVIEWER_<NAME>``); the role
token, which names nobody, is refused. A participant is recorded as ``P-`` and the first 8 hex digits
of SHA-256(PDM_STUDY_SALT + name). Nothing a participant receives names a reviewer or an arm. The
evaluator role reads the results per true arm; nothing in them names a participant. Without
PDM_STUDY_SALT, or with one shorter than 16 characters, every study endpoint answers 503. Keep the salt
for the whole study: a new salt gives everyone a new pseudonym, and their open sessions are then
another participant's.

The label of each arm is not stored: it is HMAC-SHA256 under the salt of the session id and the arm,
so it holds for the whole session, differs between sessions, and only the salt's holder can recompute
it.

Rows in study_sessions (insert-only; every row of a session carries its session id and pseudonym):

| row | order_no | arm, incident_id | rating | seconds | decision | consent | at |
|---|---|---|---|---|---|---|---|
| consent | | | | | | true | consent given |
| item | 1 to n | the true arm, the incident | | | | | session start |
| shown | k | as the item | | | shown | | first shown |
| answer | k | as the item | 1 to 5 | since first shown | act, dismiss or unsure | | answered |

| Method and path | Roles | |
|---|---|---|
| POST /study/sessions | reviewer | `{"consent": true}`: a new session; 422 without consent, 409 if none is ready |
| GET /study/sessions | reviewer | the caller's sessions and how far each got, to carry on after a break |
| GET /study/sessions/{id}/next | reviewer | the next unanswered item under its label, or `"done": true` |
| POST /study/sessions/{id}/answers | reviewer | `{"order_no", "rating": 1-5, "decision"}`: act, dismiss or unsure |
| GET /study/results | evaluator | per arm: n, mean rating, median seconds, decision and rating counts |
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import statistics
from typing import Callable, Mapping, Optional

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

import auth
import queries
from pdm_common.brief import cited
from pdm_common.hashing import canonical_json
from pdm_common.store import _to_db
from pdm_common.timeutil import to_utc

ARMS = ("raw", "retrieval_only", "full")
LABELS = ("A", "B", "C")
DECISIONS = ("act", "dismiss", "unsure")
SHOWN = "shown"
SALT_MIN = 16
WINDOWS = 30                      # scores shown with a raw alert, as in the agent's incident_context
BODY_MAX = 10_000
HIDDEN_FIELDS = ("schema", "brief_id", "versions")
_RESULT_ID = re.compile(r"\bR-[0-9a-f]{8}-[0-9]+\b")
_RUN_IDS = re.compile(r"\b(?:B-[0-9a-f]{8}-[0-9]+|T-[0-9a-f]{16})\b")


class StudyError(Exception):
    """A study request that cannot be answered; ``status`` is the HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _true(store):
    return True if store.dialect == "postgres" else 1


def pseudonym(salt: str, name: str) -> str:
    """'P-' and the first 8 hex digits of SHA-256(salt + name)."""
    return "P-" + hashlib.sha256((salt + name).encode()).hexdigest()[:8]


def arm_labels(salt: str, session_id: str) -> dict:
    """{arm: label} of one session: A, B and C in the order of HMAC(salt, session id and arm)."""
    key = salt.encode()
    order = sorted(ARMS, key=lambda arm: hmac.new(key, f"{session_id}/{arm}".encode(), hashlib.sha256).hexdigest())
    return dict(zip(order, LABELS))


# --- choosing and dealing ---------------------------------------------------------------------------

def eligible(store) -> list[str]:
    """The incidents with a valid brief in both agent arms."""
    t = _true(store)
    rows = store.query("SELECT incident_id FROM briefs WHERE valid = ? AND arm = ? INTERSECT "
                       "SELECT incident_id FROM briefs WHERE valid = ? AND arm = ?", [t, "full", t, "retrieval_only"])
    return sorted(r["incident_id"] for r in rows)


def deal(incidents: list[str], rng) -> list[tuple[str, str]]:
    """(incident, arm) in the order shown: the incidents shuffled, each arm a third of them."""
    order = list(incidents)
    rng.shuffle(order)
    arms = [arm for arm in ARMS for _ in range(len(order) // 3)] + rng.sample(ARMS, len(order) % 3)
    rng.shuffle(arms)
    return list(zip(order, arms))


def start(store, participant: str, rng, at) -> dict:
    """A new session: the consent row, then one row per item. Returns its id and item count."""
    items = deal(eligible(store), rng)
    if not items:
        raise StudyError(409, "no incident has a valid brief in both agent arms yet; write the study briefs "
                              "first (agent.py --study INC-... --arm retrieval_only)")
    session_id = f"S-{secrets.token_hex(8)}"
    with store.transaction():
        store.insert("study_sessions", {"session_id": session_id, "reviewer": participant, "consent": True, "at": at})
        for n, (incident_id, arm) in enumerate(items, 1):
            store.insert("study_sessions", {"session_id": session_id, "reviewer": participant, "arm": arm,
                                            "incident_id": incident_id, "order_no": n, "at": at})
    return {"session_id": session_id, "items": len(items)}


# --- a session's rows -----------------------------------------------------------------------------

def _owned(store, session_id: str, participant: str) -> list[dict]:
    rows = store.select("study_sessions", {"session_id": session_id}, order="id")
    if not rows:
        raise StudyError(404, "no such study session")
    if any(r["reviewer"] != participant for r in rows):
        raise StudyError(403, "this study session belongs to another participant")
    return rows


def _items(rows: list[dict]) -> dict:
    """order_no -> {"item", "shown", "answer"}: the rows of each item (the first of each kind)."""
    out: dict = {}
    for r in rows:
        if r["order_no"] is None:
            continue
        slot = out.setdefault(r["order_no"], {"item": None, "shown": None, "answer": None})
        kind = "answer" if r["rating"] is not None else "shown" if r["decision"] == SHOWN else "item"
        slot[kind] = slot[kind] or r
    return dict(sorted(out.items()))


def sessions_of(store, participant: str) -> list[dict]:
    """The participant's sessions, oldest first, with how far each got."""
    by_session: dict = {}
    for r in store.select("study_sessions", {"reviewer": participant}, order="id"):
        by_session.setdefault(r["session_id"], []).append(r)
    out = []
    for session_id, rows in by_session.items():
        items = _items(rows)
        answered = sum(1 for slot in items.values() if slot["answer"])
        out.append({"session_id": session_id, "started_at": rows[0]["at"], "items": len(items),
                    "answered": answered, "done": answered == len(items)})
    return out


# --- what a participant sees --------------------------------------------------------------------

def _relabel(value, names: dict):
    """``value`` with every result id replaced by its label and every brief or trace id removed."""
    if isinstance(value, str):
        value = _RESULT_ID.sub(lambda m: names.get(m.group(0), "an uncited result"), value)
        return _RUN_IDS.sub("(withheld)", value)
    if isinstance(value, dict):
        return {k: _relabel(v, names) for k, v in value.items()}
    if isinstance(value, list):
        return [_relabel(v, names) for v in value]
    return value


def blind_brief(document: dict, results: dict) -> tuple[dict, dict]:
    """(brief, evidence): the brief without its id, versions and schema, its citations relabelled
    E1, E2, ... in the order the brief first cites them, and each cited result as {tool, arguments,
    output} under its label."""
    names = {rid: f"E{n}" for n, rid in enumerate(cited(document), 1)}
    brief = _relabel({k: v for k, v in document.items() if k not in HIDDEN_FIELDS}, names)
    evidence = {names[rid]: {"tool": r["tool"], "arguments": _relabel(r["arguments"], names),
                             "output": _relabel(r["output"], names)}
                for rid, r in results.items() if rid in names}
    return brief, dict(sorted(evidence.items(), key=lambda kv: int(kv[0][1:])))


def raw_alert(store, incident: dict) -> dict:
    """The alert alone: the gate's cause, the scores up to the opening and the checks at the opening."""
    opened = store.one("SELECT cause FROM incident_events WHERE incident_id = ? AND to_state = ? ORDER BY id LIMIT 1",
                       [incident["incident_id"], "open"])
    as_of = to_utc(incident["as_of"] or incident["opened_at"])
    scores = store.query("SELECT window_end, score, threshold, alert FROM scores WHERE asset_id = ? "
                         f"AND window_end <= ? ORDER BY window_end DESC LIMIT {WINDOWS}",
                         [incident["asset_id"], _to_db("scores", "window_end", as_of, store.dialect)], table="scores")
    gate = incident.get("gate") or {}
    checks = (incident.get("triage") or {}).get("checks") or []
    return {"cause": opened["cause"] if opened else None,
            "gate": {k: gate.get(k) for k in ("span", "min_fraction", "alerting", "present")},
            "trajectory": [{"minutes_before_opening": round((as_of - to_utc(s["window_end"])).total_seconds() / 60, 1),
                            "score": s["score"], "threshold": s["threshold"], "alert": s["alert"]}
                           for s in reversed(scores)],
            "checks": [{k: c.get(k) for k in ("id", "check", "kind", "result", "measured")} for c in checks]}


def latest_brief(store, incident_id: str, arm: str) -> Optional[dict]:
    return store.one("SELECT * FROM briefs WHERE incident_id = ? AND valid = ? AND arm = ? "
                     "ORDER BY created_at DESC LIMIT 1", [incident_id, _true(store), arm], table="briefs")


def arm_content(store, incident_id: str, arm: str) -> dict:
    """What an item shows for its arm, with the incident it is about (the same in every arm)."""
    inc = store.one("SELECT incident_id, asset_id, opened_at, as_of, gate, triage FROM incidents "
                    "WHERE incident_id = ?", [incident_id], table="incidents")
    if inc is None:
        raise StudyError(409, f"the incident of this item, {incident_id}, is not in the store")
    head = {"incident_id": inc["incident_id"], "asset_id": inc["asset_id"], "opened_at": inc["opened_at"]}
    if arm == "raw":
        return {"incident": head, "kind": "alert", "alert": raw_alert(store, inc)}
    row = latest_brief(store, incident_id, arm)
    if row is None:
        raise StudyError(409, f"the brief of this item is missing for {incident_id}")
    brief, evidence = blind_brief(row["document"], queries.results(store, cited(row["document"])))
    return {"incident": head, "kind": "brief", "brief": brief, "evidence": evidence,
            "banner": None if brief.get("evidence_state") == "sufficient" else brief.get("evidence_state")}


def next_item(store, session_id: str, participant: str, salt: str, at) -> dict:
    """The first unanswered item under its label, recorded as shown the first time; or done."""
    rows = _owned(store, session_id, participant)
    items = _items(rows)
    answered = sum(1 for slot in items.values() if slot["answer"])
    progress = {"session_id": session_id, "items": len(items), "answered": answered}
    pending = [n for n, slot in items.items() if slot["answer"] is None]
    if not pending:
        return dict(progress, done=True)
    n = pending[0]
    item = items[n]["item"]
    content = arm_content(store, item["incident_id"], item["arm"])
    if items[n]["shown"] is None:
        with store.transaction():
            fresh = _items(_owned(store, session_id, participant))[n]
            if fresh["shown"] is None:
                store.insert("study_sessions", {"session_id": session_id, "reviewer": participant, "arm": item["arm"],
                                                "incident_id": item["incident_id"], "order_no": n, "decision": SHOWN,
                                                "at": at})
    return dict(progress, done=False, order_no=n, label=arm_labels(salt, session_id)[item["arm"]], **content)


def _whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def answer(store, session_id: str, participant: str, body: dict, at) -> dict:
    """Store the answer to a shown item, with the seconds since it was first shown."""
    order_no, rating, decision = body.get("order_no"), body.get("rating"), body.get("decision")
    with store.transaction():
        items = _items(_owned(store, session_id, participant))
        if not _whole(order_no):
            raise StudyError(422, "order_no is the number of the item answered")
        if not _whole(rating) or not 1 <= rating <= 5:
            raise StudyError(422, "rating is a whole number from 1 (not useful) to 5 (very useful)")
        if decision not in DECISIONS:
            raise StudyError(422, f"decision is one of {', '.join(DECISIONS)}")
        slot = items.get(order_no)
        if slot is None or slot["item"] is None:
            raise StudyError(404, f"this session has no item {order_no}")
        if slot["answer"] is not None:
            raise StudyError(409, f"item {order_no} is already answered")
        if slot["shown"] is None:
            raise StudyError(409, f"item {order_no} has not been shown yet")
        seconds = max(0.0, round((to_utc(at) - to_utc(slot["shown"]["at"])).total_seconds(), 3))
        store.insert("study_sessions", {"session_id": session_id, "reviewer": participant, "arm": slot["item"]["arm"],
                                        "incident_id": slot["item"]["incident_id"], "order_no": order_no,
                                        "rating": rating, "seconds": seconds, "decision": decision, "at": at})
    answered = sum(1 for s in items.values() if s["answer"]) + 1
    return {"session_id": session_id, "order_no": order_no, "recorded": True, "items": len(items),
            "answered": answered, "done": answered == len(items)}


# --- results for the evaluators ------------------------------------------------------------------

def results(store) -> dict:
    """Per true arm: answers, mean rating, median seconds, decision and rating counts; also how many
    items were dealt and shown. Counts of sessions and participants; nothing that names anyone."""
    rows = store.select("study_sessions", order="id")
    arms = {arm: {"dealt": 0, "shown": 0, "answers": []} for arm in ARMS}
    for r in rows:
        if r["order_no"] is None or r["arm"] not in arms:
            continue
        if r["rating"] is not None:
            arms[r["arm"]]["answers"].append(r)
        elif r["decision"] == SHOWN:
            arms[r["arm"]]["shown"] += 1
        else:
            arms[r["arm"]]["dealt"] += 1
    out = {}
    for arm, a in arms.items():
        ratings = [r["rating"] for r in a["answers"]]
        seconds = [r["seconds"] for r in a["answers"] if r["seconds"] is not None]
        out[arm] = {"n": len(ratings), "dealt": a["dealt"], "shown": a["shown"],
                    "mean_rating": round(statistics.fmean(ratings), 3) if ratings else None,
                    "median_seconds": round(statistics.median(seconds), 1) if seconds else None,
                    "decisions": {d: sum(1 for r in a["answers"] if r["decision"] == d) for d in DECISIONS},
                    "ratings": {str(k): ratings.count(k) for k in range(1, 6)}}
    consented = [r for r in rows if r["consent"]]
    return {"sessions": len({r["session_id"] for r in consented}),
            "participants": len({r["reviewer"] for r in consented}),
            "answers": sum(v["n"] for v in out.values()), "arms": out}


# --- the routes ---------------------------------------------------------------------------------

def _json(doc, status: int = 200) -> Response:
    return Response(canonical_json(doc), status_code=status, media_type="application/json")


async def _body(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > BODY_MAX:
        raise StudyError(413, "the body is too large")
    try:
        doc = json.loads(raw or b"{}")
    except ValueError:
        raise StudyError(422, "the body is not JSON")
    if not isinstance(doc, dict):
        raise StudyError(422, "the body is not a JSON object")
    return doc


def routes(get_store: Callable, tokens: Mapping, *, env: Optional[Mapping] = None, rng=None,
           now: Optional[Callable] = None) -> list[Route]:
    """The study endpoints. ``get_store()`` gives the store, ``tokens`` is the API's {token: Principal};
    ``env`` (default os.environ, read on every request) holds PDM_STUDY_SALT; ``rng`` shuffles
    (default random.SystemRandom()); ``now`` is the clock (default UTC now)."""
    env = os.environ if env is None else env
    rng = rng or random.SystemRandom()
    now = now or (lambda: dt.datetime.now(dt.timezone.utc))

    def who(request: Request, role: str) -> auth.Principal:
        principal = auth.authenticate(request.headers.get("authorization"), tokens)
        if principal is None:
            raise StudyError(401, "authentication required")
        if principal.role != role:
            raise StudyError(403, f"the {principal.role} role may not use this endpoint")
        return principal

    def salt() -> str:
        value = env.get("PDM_STUDY_SALT") or ""
        if not value:
            raise StudyError(503, "the study is not set up: PDM_STUDY_SALT is unset")
        if len(value) < SALT_MIN:
            raise StudyError(503, f"the study is not set up: PDM_STUDY_SALT is shorter than {SALT_MIN} characters")
        return value

    def participant(request: Request) -> tuple[str, str]:
        principal = who(request, "reviewer")
        key = salt()
        if principal.name == principal.role:
            raise StudyError(403, "the study needs your own reviewer token (PDM_TOKEN_REVIEWER_<NAME>), "
                                  "not the role token")
        return pseudonym(key, principal.name), key

    def endpoint(fn):
        async def handle(request: Request) -> Response:
            try:
                return await fn(request)
            except StudyError as exc:
                headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
                return Response(canonical_json({"error": exc.message}), status_code=exc.status,
                                media_type="application/json", headers=headers)
        handle.__name__ = fn.__name__
        return handle

    @endpoint
    async def study_start(request):
        person, _ = participant(request)
        if (await _body(request)).get("consent") is not True:
            raise StudyError(422, "consent comes first: send {\"consent\": true} once the participant has agreed")
        return _json(start(get_store(), person, rng, now()), 201)

    @endpoint
    async def study_sessions(request):
        person, _ = participant(request)
        return _json({"sessions": sessions_of(get_store(), person)})

    @endpoint
    async def study_next(request):
        person, key = participant(request)
        return _json(next_item(get_store(), request.path_params["session_id"], person, key, now()))

    @endpoint
    async def study_answer(request):
        person, _ = participant(request)
        doc = await _body(request)
        return _json(answer(get_store(), request.path_params["session_id"], person, doc, now()), 201)

    @endpoint
    async def study_results(request):
        who(request, "evaluator")
        salt()
        return _json(results(get_store()))

    return [Route("/study/sessions", study_start, methods=["POST"], name="study_start"),
            Route("/study/sessions", study_sessions, methods=["GET"], name="study_sessions"),
            Route("/study/sessions/{session_id}/next", study_next, methods=["GET"], name="study_next"),
            Route("/study/sessions/{session_id}/answers", study_answer, methods=["POST"], name="study_answer"),
            Route("/study/results", study_results, methods=["GET"], name="study_results")]
