"""The operator dashboard (T18, T19, T34; HIL-01 to HIL-04, HIL-07 to HIL-09, NFR-09, INT-04).

services/dashboard against the real API in process (starlette's TestClient) over a store with real
incidents and briefs, built as tests/test_study.py builds it: a sufficient, an insufficient and a
budget-exhausted brief with study briefs beside them, and one more sufficient brief to decide on.

* api_client.py against every endpoint the dashboard uses, and its errors;
* views.py and charts.py on the API's JSON;
* app.py run under a stand-in streamlit module (below) that has the signatures of the Streamlit calls
  the page makes, records what it draws, plays back typed values and clicks, runs button callbacks
  and reruns as Streamlit does, and refuses what Streamlit refuses (a duplicate widget key, a widget's
  state changed after it was drawn, a value that is not one of the options).

Streamlit and Plotly are not installed here: this checks the logic and the page wiring, not the page
in a browser.
"""
import contextlib
import json
import random
import re
import runpy
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_study import AS, ENV, Clock, build_world, clone, study_client, walk  # noqa: E402

sys.path.insert(0, str(ROOT / "services" / "dashboard"))
import api_client  # noqa: E402
import auth  # noqa: E402
import charts  # noqa: E402
import views  # noqa: E402

APP = ROOT / "services" / "dashboard" / "app.py"
ARM_WORDS = re.compile(r"\b(raw|full|retrieval|arms?|agent brief)\b|retrieval.only", re.I)


@pytest.fixture(scope="module")
def world():
    return build_world([("sufficient", True), ("insufficient", True), ("budget_exhausted", True),
                        ("sufficient", False)])


@pytest.fixture
def env(world):
    """A fresh copy of the world, the API over it (with the study routes) and the incidents by role."""
    store = clone(world.store)
    tokens = auth.load_tokens(ENV)
    clock = Clock()
    client = study_client(store, rng=random.Random(3), clock=clock, tokens=tokens)
    ids = [iid for iid, _, _ in world.incidents]
    return SimpleNamespace(store=store, client=client, tokens=tokens, clock=clock, asset=world.asset,
                           sufficient=ids[0], insufficient=ids[1], budget=ids[2], decide=ids[3])


def api(env, who="rosh"):
    return api_client.ApiClient("http://testserver", AS[who], client=env.client)


def brief_of(env, iid):
    return api(env).incident(iid)["brief"]


# --- a stand-in for streamlit -----------------------------------------------------------------------------

class Rerun(Exception):
    """st.rerun(), or a button callback: the script runs again from the top."""


class StreamlitAPIException(Exception):
    pass


class SessionState(dict):
    """st.session_state: refuses to change a widget's value once the widget is drawn, except in a callback."""

    def __init__(self, fake):
        super().__init__()
        self._fake = fake

    def __setitem__(self, key, value):
        if key in self._fake.live and not self._fake.in_callback:
            raise StreamlitAPIException(f"st.session_state.{key} cannot be modified after the widget with key "
                                        f"{key} is instantiated")
        super().__setitem__(key, value)

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


class FakeStreamlit(types.ModuleType):
    """The calls app.py makes, with Streamlit's signatures. ``values``: what the person typed or chose,
    by widget key (used the next time the widget is drawn); ``clicks``: button keys pressed."""

    def __init__(self, values=None, clicks=()):
        super().__init__("streamlit")
        self.session_state = SessionState(self)
        self.values, self.clicks = dict(values or {}), list(clicks)
        self.sidebar = self
        self.in_callback = False
        self.log, self.input_types, self.runs, self.widget_keys = [], {}, 0, set()
        self.begin_run()

    def begin_run(self):
        self.runs += 1
        self.live, self.out, self.buttons, self.widgets, self.figures, self.tables = set(), [], {}, {}, {}, []

    def end_run(self):
        """After a run that finished, Streamlit forgets the state of every widget the run did not draw."""
        for key in [k for k in self.session_state if k in self.widget_keys and k not in self.live]:
            dict.__delitem__(self.session_state, key)

    # -- what the page draws
    def _put(self, kind, body):
        text = body if isinstance(body, str) else json.dumps(body, default=str)
        self.out.append((kind, text))
        self.log.append((kind, text))

    def text(self, skip=()):
        return "\n".join(body for kind, body in self.out if kind not in skip)

    def page_words(self):
        """What a person reads on the page: text, labels and captions, not the expandable evidence."""
        words = [body for kind, body in self.out if kind not in ("json", "dataframe")]
        words += [w["label"] for w in self.widgets.values()] + [str(o) for w in self.widgets.values()
                                                                for o in w["labels"]]
        return "\n".join(words)

    def everything(self):
        return json.dumps([self.log, self.input_types])

    def set_page_config(self, page_title=None, page_icon=None, layout="centered", initial_sidebar_state="auto",
                        menu_items=None):
        self.page_config = {"page_title": page_title, "layout": layout}

    def title(self, body, anchor=None, *, help=None):
        self._put("title", body)

    def subheader(self, body, anchor=None, *, help=None, divider=False):
        self._put("subheader", body)

    def markdown(self, body, unsafe_allow_html=False, *, help=None):
        if unsafe_allow_html:          # raw HTML only for the marker dot, with a colour from views.MARKER_HEX
            colours = "|".join(views.MARKER_HEX.values())
            assert re.fullmatch(rf"<span style='color:({colours});font-size:1\.6rem;line-height:1'>&#9679;</span>",
                                body), body
        self._put("markdown", body)

    def caption(self, body, unsafe_allow_html=False, *, help=None):
        self._put("caption", body)

    def code(self, body, language="python", *, line_numbers=False, wrap_lines=False):
        self._put("code", body)

    def json(self, body, *, expanded=True):
        self._put("json", body)

    def info(self, body, *, icon=None):
        self._put("info", body)

    def success(self, body, *, icon=None):
        self._put("success", body)

    def warning(self, body, *, icon=None):
        self._put("warning", body)

    def error(self, body, *, icon=None):
        self._put("error", body)

    def metric(self, label, value, delta=None, delta_color="normal", help=None, label_visibility="visible"):
        self._put("metric", f"{label}: {value}")

    def dataframe(self, data=None, width=None, height=None, *, use_container_width=False, hide_index=None,
                  column_order=None, column_config=None, key=None, on_select="ignore", selection_mode="multi-row"):
        records = data.to_dict("records")
        self.tables.append(records)
        self._put("dataframe", records)

    def plotly_chart(self, figure_or_data, use_container_width=False, *, theme="streamlit", key=None,
                     on_select="ignore", selection_mode=("points", "box", "lasso"), config=None):
        assert key and key not in self.figures, key
        json.dumps(figure_or_data, allow_nan=False)
        assert set(figure_or_data) == {"data", "layout"}
        assert all(t["type"] in ("scatter", "bar") for t in figure_or_data["data"])
        self.figures[key] = figure_or_data
        self._put("chart", key)

    # -- layout
    def columns(self, spec, *, gap="small", vertical_alignment="top"):
        return [self] * (spec if isinstance(spec, int) else len(spec))

    def container(self, *, height=None, border=None, key=None):
        return contextlib.nullcontext(self)

    def expander(self, label, expanded=False, *, icon=None):
        self._put("expander", label)
        return contextlib.nullcontext(self)

    # -- widgets
    def _widget(self, kind, label, key, default, options=None, format_func=str):
        assert key, f"{kind} {label!r} has no key"
        if key in self.live:
            raise StreamlitAPIException(f"DuplicateWidgetID: two widgets with key {key}")
        if key in self.values:
            value = self.values.pop(key)
        elif key in self.session_state:
            value = self.session_state[key]
        else:
            value = default
        if options is not None and value is not None and value not in options:
            raise StreamlitAPIException(f"{key}: {value!r} is not one of the options {options}")
        dict.__setitem__(self.session_state, key, value)
        self.live.add(key)
        self.widget_keys.add(key)
        self.widgets[key] = {"kind": kind, "label": label, "options": options,
                             "labels": [format_func(o) for o in options or ()]}
        return value

    def text_input(self, label, value="", max_chars=None, key=None, type="default", help=None, autocomplete=None,
                   on_change=None, args=None, kwargs=None, *, placeholder=None, disabled=False,
                   label_visibility="visible"):
        self.input_types[key] = type
        return self._widget("text_input", label, key, value)

    def text_area(self, label, value="", height=None, max_chars=None, key=None, help=None, on_change=None, args=None,
                  kwargs=None, *, placeholder=None, disabled=False, label_visibility="visible"):
        return self._widget("text_area", label, key, value)

    def selectbox(self, label, options, index=0, format_func=str, key=None, help=None, on_change=None, args=None,
                  kwargs=None, *, placeholder="Choose an option", disabled=False, label_visibility="visible"):
        options = list(options)
        return self._widget("selectbox", label, key, options[index] if options and index is not None else None,
                            options, format_func)

    def radio(self, label, options, index=0, format_func=str, key=None, help=None, on_change=None, args=None,
              kwargs=None, *, disabled=False, horizontal=False, captions=None, label_visibility="visible"):
        options = list(options)
        return self._widget("radio", label, key, options[index] if options and index is not None else None,
                            options, format_func)

    def checkbox(self, label, value=False, key=None, help=None, on_change=None, args=None, kwargs=None, *,
                 disabled=False, label_visibility="visible"):
        return self._widget("checkbox", label, key, value)

    def button(self, label, key=None, help=None, on_click=None, args=None, kwargs=None, *, type="secondary",
               icon=None, disabled=False, use_container_width=False):
        self._widget("button", label, key, False)
        self.buttons[key] = {"label": label, "disabled": disabled}
        if key not in self.clicks:
            return False
        assert not disabled, f"{key} was clicked but is disabled"
        self.clicks.remove(key)
        if on_click is None:
            return True
        self.in_callback = True
        try:
            on_click(*(args or ()), **(kwargs or {}))
        finally:
            self.in_callback = False
        raise Rerun()

    def rerun(self, *, scope="app"):
        raise Rerun()


@pytest.fixture
def page(env, monkeypatch):
    """run(fake): app.py under ``fake`` until it stops asking to run again, against the env's API."""
    monkeypatch.setenv("PDM_API_URL", "http://testserver")
    monkeypatch.setattr(api_client, "http_client", lambda timeout: env.client)

    def run(fake, max_runs=8):
        monkeypatch.setitem(sys.modules, "streamlit", fake)
        path = list(sys.path)
        try:
            for _ in range(max_runs):
                fake.begin_run()
                try:
                    runpy.run_path(str(APP), run_name="__main__")
                except Rerun:
                    continue
                fake.end_run()
                break
            else:
                raise AssertionError("the page kept asking to run again")
        finally:
            sys.path[:] = path
        assert not fake.clicks, f"clicked but never drawn: {fake.clicks}"
        return fake
    return run


def signed_in(page, who="rosh", **values):
    fake = page(FakeStreamlit(values={"token_input": AS[who], **values}, clicks=["sign_in"]))
    assert fake.session_state["auth"]["name"] == who
    return fake


def show(run, fake, clicks=(), **values):
    """Type or choose ``values``, press ``clicks``, and run the page again."""
    fake.values.update(values)
    fake.clicks = list(clicks)
    return run(fake)


# --- api_client.py ---------------------------------------------------------------------------------

def test_the_client_reads_every_view_and_writes_a_decision(env):
    c = api(env)
    assert c.health()["status"] == "ok" and c.me() == {"role": "reviewer", "name": "rosh"}
    assets = c.assets()["assets"]
    assert [a["asset_id"] for a in assets] == [env.asset]
    detail = c.asset(env.asset)
    assert detail["latest_alert"]["attribution"] and detail["incidents"]
    full = c.series(env.asset)
    start, end = views.span_bounds(detail["last_update"], 1)
    part = c.series(env.asset, start, end)
    assert (part["from"], part["to"]) == (start, end) and full["scores"]["score"]
    queue = c.incidents(state="awaiting_review")["incidents"]
    assert {i["incident_id"] for i in queue} == {env.sufficient, env.insufficient, env.budget, env.decide}
    assert {i["incident_id"] for i in c.incidents(open_only=True, asset_id=env.asset)["incidents"]} == \
        {i["incident_id"] for i in queue}
    incident = c.incident(env.decide)
    rid = next(iter(incident["cited_results"]))
    assert c.result(rid)["result_id"] == rid and c.work_orders()["work_orders"] == []
    out = c.decide(incident["brief"]["brief_id"], {"action": "approve", "reason": "matches the evidence"})
    assert out["decision"]["reviewer"] == "rosh" and out["work_order"]["status"] == "queued"
    assert [w["wo_id"] for w in c.work_orders(status="queued")["work_orders"]] == [out["work_order"]["wo_id"]]
    assert c.decision(out["decision"]["decision_id"])["evidence_matches"] is True
    assert api(env, "engineer").trace(out["decision"]["trace_id"])["outcome"] == "sufficient"
    session = c.study_start(True)
    item = c.study_next(session["session_id"])
    assert item["label"] in "ABC" and not item["done"]
    receipt = c.study_answer(session["session_id"], {"order_no": item["order_no"], "rating": 4, "decision": "act"})
    assert receipt["answered"] == 1 and c.study_sessions()["sessions"][0]["answered"] == 1
    assert api(env, "evaluator").study_results()["answers"] == 1


def _mock(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_refusals_and_failures_raise_api_errors_that_never_carry_the_token(env):
    errors = []

    def caught(call, status):
        with pytest.raises(api_client.ApiError) as e:
            call()
        assert e.value.status == status, (status, str(e.value))
        errors.append(str(e.value))
        return str(e.value)

    stranger = api_client.ApiClient("http://testserver", "x" * 40, client=env.client)
    assert "sign in again" in caught(stranger.assets, 401) and "x" * 40 not in repr(stranger)
    bid = brief_of(env, env.decide)["brief_id"]
    assert "not allowed for your role" in caught(lambda: api(env, "engineer").decide(bid, {"action": "approve",
                                                                                          "reason": "x"}), 403)
    assert "every decision needs a reason" in caught(lambda: api(env).decide(bid, {"action": "approve"}), 422)
    assert "no such incident" in caught(lambda: api(env).incident("INC-nope"), 404)
    api(env).decide(bid, {"action": "reject", "reason": "a known restart"})
    assert "not awaiting review" in caught(lambda: api(env).decide(bid, {"action": "approve", "reason": "x"}), 409)

    seen = []

    def server(request):
        seen.append(request)
        if request.url.path == "/assets":
            return httpx.Response(500, json={"error": "the store is down"})
        if request.url.path == "/me":
            return httpx.Response(502, text="Bad Gateway")
        return httpx.Response(200, text="not json")
    broken = api_client.ApiClient("http://api.test", "secret-token-" + "y" * 30, client=_mock(server))
    assert caught(broken.assets, 500) == "the API failed: the store is down (HTTP 500)"
    assert "Bad Gateway" in caught(broken.me, 502)
    assert "did not answer with a JSON object" in caught(broken.health, 200)
    assert all(r.headers["authorization"] == "Bearer secret-token-" + "y" * 30 and "secret" not in str(r.url)
               for r in seen)

    def down(request):
        raise httpx.ConnectError("connection refused", request=request)

    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)
    assert "cannot reach the API at http://api.test" in caught(
        api_client.ApiClient("http://api.test", "secret-token-" + "y" * 30, client=_mock(down)).me, None)
    assert "did not answer within 2 s" in caught(
        api_client.ApiClient("http://api.test", "secret-token-" + "y" * 30, timeout=2, client=_mock(slow)).me, None)
    assert not [e for e in errors if "x" * 40 in e or "secret-token" in e]


def test_the_base_url_and_timeout_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("PDM_API_URL", "http://api:8000/")
    monkeypatch.setenv("PDM_API_TIMEOUT", "3.5")
    c = api_client.ApiClient(token="t", client=object())
    assert (c.base_url, c.timeout) == ("http://api:8000", 3.5)
    monkeypatch.delenv("PDM_API_URL")
    monkeypatch.delenv("PDM_API_TIMEOUT")
    c = api_client.ApiClient(token="t", client=object())
    assert (c.base_url, c.timeout) == (api_client.DEFAULT_URL, api_client.DEFAULT_TIMEOUT_S)


# --- views.py -------------------------------------------------------------------------------------

def test_no_probability_without_its_interval():
    assert views.fmt_p({"p": 0.123, "lower": 0.05, "upper": 0.314}) == "0.12 (0.05-0.31)"
    assert views.fmt_p({"p": 0.5, "lower": None, "upper": 0.9}) == "n/a (no interval)"
    assert views.fmt_p({"p": 0.5}) == "n/a (no interval)"
    assert views.fmt_p(None) == views.fmt_p({}) == "n/a"


def test_the_asset_rows_keep_the_api_order_with_markers_intervals_and_the_skill_note(env):
    env.store.insert("prognoses", {"asset_id": "zz_other", "ts": "2020-06-01T03:00:00Z", "model_run_id": "m",
                                   "probabilities": {h_: {"p": 0.9, "lower": 0.8, "upper": 0.95}
                                                     for h_ in ("24", "72", "168")}})
    c = api(env)
    assets = c.assets()["assets"]
    rows = views.asset_rows(assets, c.incidents(open_only=True)["incidents"])
    assert [r["asset_id"] for r in rows] == [a["asset_id"] for a in assets] == ["zz_other", env.asset]
    other, mine = rows
    assert (other["p72"], other["colour"], other["status"], other["skill_note"]) == ("0.90 (0.80-0.95)", "grey",
                                                                                   "no status yet", None)
    prognosis = assets[1]["prognosis"]
    for h_ in views.HORIZONS:
        assert mine[f"p{h_}"] == views.fmt_p(prognosis["horizons"][h_]) and "(" in mine[f"p{h_}"]
    assert mine["skill_note"] == views.SKILL_NOTE and mine["status"] == "maintenance required"
    assert (mine["colour"], mine["hex"]) == ("amber", views.MARKER_HEX["amber"])
    assert mine["incidents"] == [env.decide, env.budget, env.insufficient, env.sufficient]       # newest first
    assert mine["open_incidents"] == 4 and mine["last_update"].endswith("UTC")
    assert views.probability_line(mine).startswith(f"P(24 h) {mine['p24']} · P(72 h) {mine['p72']}")
    assert [views.ne107_marker(s)["colour"] for s in ("good", "failure", "function_check")] == ["green", "red", "amber"]


def test_markdown_from_the_api_shows_as_text():
    hostile = "![x](https://evil.test/?q=1) *bold* <b>tag</b> $x$ :red[alarm] # head"
    escaped = views.md(hostile)
    assert not re.search(r"(?<!\\)[\[\]()!*<>#$:]", escaped)
    assert re.sub(r"\\(.)", r"\1", escaped) == hostile


def test_a_brief_view_resolves_every_citation_and_reads_the_prognosis_from_the_context(env):
    detail = api(env).incident(env.decide)
    view = views.incident_brief(detail)
    cites = [c for hyp in view["hypotheses"] for claim in hyp["claims"] for c in claim["citations"]]
    cites += view["action"]["citations"] + view["prognosis"]["citations"]
    assert cites and all(c["result"] is not None and c["title"] == f"{c['id']} · {c['tool']}" for c in cites)
    assert view["banner"] is None and view["prognosis"]["available"]
    assert all("(" in view["prognosis"]["cells"][h_] for h_ in views.HORIZONS)
    assert view["prognosis"]["note"] == views.SKILL_NOTE and view["prognosis"]["made"].startswith("made ")
    assert view["checks"][0]["check"].startswith("C1 ") and {r["result"] for r in view["checks"]} <= {
        "pass", "fail", "inconclusive"}
    early = views.incident_brief(api(env).incident(env.sufficient))     # opened before the first prognosis
    assert early["prognosis"]["available"] is False and "no prognosis" in early["prognosis"]["text"]
    missing = views.citation("R-00000000-1", {})
    assert missing["result"] is None and missing["title"] == "R-00000000-1 · not loaded"


def test_insufficient_and_budget_exhausted_briefs_have_their_own_banner(env):
    c = api(env)
    assert views.incident_brief(c.incident(env.insufficient))["banner"] == views.BANNERS["insufficient"]
    assert views.incident_brief(c.incident(env.budget))["banner"] == views.BANNERS["budget_exhausted"]
    assert views.BANNERS["insufficient"] != views.BANNERS["budget_exhausted"]
    assert views.banner_text(None) is None and views.banner_text("sufficient") is None


def test_the_decision_body_needs_a_reason_and_an_edit_needs_a_change():
    draft = {"title": "Review the alarm", "asset": "a1", "priority": "low", "tasks": ["Review the data"],
             "safety_notes": ["No work on the equipment."], "status": "draft"}
    fields = views.work_order_fields(draft)
    with pytest.raises(views.FormError, match="reason"):
        views.decision_body("approve", "   ")
    with pytest.raises(views.FormError, match="approve, edit or reject"):
        views.decision_body("ship", "x")
    assert views.decision_body("approve", " ok ") == {"action": "approve", "reason": "ok"}
    assert views.decision_body("reject", "a restart") == {"action": "reject", "reason": "a restart"}
    same = views.work_order_from_form(draft, " " + fields["title"], fields["priority"], fields["tasks"] + "\n\n",
                                      fields["safety_notes"])
    assert not views.work_order_changed(draft, same)
    with pytest.raises(views.FormError, match="unchanged"):
        views.decision_body("edit", "x", draft, same)
    edited = views.work_order_from_form(draft, "Inspect the air path", "high", "Find the leak\nCheck the drain valve",
                                        fields["safety_notes"])
    assert views.decision_body("edit", "x", draft, edited) == {
        "action": "edit", "reason": "x",
        "work_order": {"title": "Inspect the air path", "asset": "a1", "priority": "high",
                       "tasks": ["Find the leak", "Check the drain valve"],
                       "safety_notes": ["No work on the equipment."]}}
    with pytest.raises(views.FormError, match="at least one task"):
        views.decision_body("edit", "x", draft, views.work_order_from_form(draft, "t", "low", "", "s"))
    with pytest.raises(views.FormError, match="longer than"):
        views.decision_body("reject", "x" * (views.REASON_MAX + 1))


def test_the_diff_preview_is_the_diff_the_api_stores(env):
    brief = brief_of(env, env.decide)
    draft = brief["document"]["work_order"]
    fields = views.work_order_fields(draft)
    edited = views.work_order_from_form(draft, fields["title"], "high", fields["tasks"] + "\nCheck the drain valve",
                                        fields["safety_notes"])
    preview = views.work_order_diff(draft, edited)
    out = api(env).decide(brief["brief_id"], views.decision_body("edit", "add the drain check", draft, edited))
    compact = preview.replace(" ", "")
    assert out["decision"]["diff"] == preview and '+"Checkthedrainvalve"' in compact and '+"priority":"high"' in compact
    assert out["work_order"]["body"]["tasks"][-1] == "Check the drain valve"


def test_work_order_status_and_notes():
    rows = [{"wo_id": "WO-1", "status": "queued", "attempts": 0},
            {"wo_id": "WO-2", "status": "sent", "attempts": 1, "reference": "SAP-42"},
            {"wo_id": "WO-3", "status": "failed", "attempts": 2, "last_error": "HTTP 503"},
            {"wo_id": "WO-4", "status": "blocked", "attempts": 8, "last_error": "tampered: the hash differs"}]
    notes = [views.work_order_note(r) for r in rows]
    assert [kind for kind, _ in notes] == ["info", "success", "warning", "error"]
    assert "SAP-42" in notes[1][1] and "attempt 2: HTTP 503" in notes[2][1] and "tampered" in notes[3][1]
    table = views.work_order_rows(rows)
    assert [r["status"] for r in table] == [views.WORK_ORDER_STATUS[s] for s in ("queued", "sent", "failed", "blocked")]
    assert table[3]["last error"] == "tampered: the hash differs"


def test_the_audit_view_says_whether_the_record_still_holds(env):
    c = api(env)
    bid = brief_of(env, env.decide)["brief_id"]
    decision = c.decide(bid, {"action": "approve", "reason": "ok"})["decision"]
    view = views.audit_view(c.decision(decision["decision_id"]))
    assert view["evidence_ok"] and view["events_ok"] and "replay to its stored state, approved" in view["events_text"]
    assert [e["to"] for e in view["events"]] == ["open", "diagnosing", "awaiting_review", "approved"]
    assert [x["tool"] for x in view["trace"]["calls"]] == ["run_check", "attribute_anomaly", "submit_brief"]
    assert all(cite["result"] for h_ in view["brief"]["hypotheses"] for claim in h_["claims"]
               for cite in claim["citations"])
    env.store.insert("decisions", dict(decision, decision_id="D-000000000000", evidence_sha256="0" * 64))
    broken = views.audit_view(c.decision("D-000000000000"))
    assert not broken["evidence_ok"] and "no longer matches" in broken["evidence_text"]


def test_study_views_show_a_label_never_an_arm(env):
    sid, got = walk(env.client, "rosh", env.clock, env.store)
    kinds = set()
    for kind, item in got:
        if kind != "item":
            continue
        view = views.study_item_view(item)
        kinds.add(view["kind"])
        assert view["label"] == f"Condition {item['label']}" and view["progress"] == f"Item {item['order_no']} of 3"
        assert view["header"].startswith(f"{env.asset} · alarm opened 2020-06-01 ")
        shown = [view["label"], view["progress"], view["header"]]
        if view["kind"] == "alert":
            shown += [view["alert"]["cause"], view["alert"]["gate"]]
            assert view["alert"]["trajectory"] and view["alert"]["checks"][0]["check"].startswith("C1 ")
        else:
            b = view["brief"]
            shown += [b["summary"], b["banner"] or "", b["action"]["text"]] + [h_["title"] for h_ in b["hypotheses"]]
            assert b["brief_id"] is None and all(c["result"] for c in b["prognosis"]["citations"])
        assert not ARM_WORDS.search("\n".join(shown)), shown
    assert kinds == {"alert", "brief"}
    end = views.study_item_view(got[-2][1])
    assert end == {"done": True, "progress": "3 of 3 answered"}
    with pytest.raises(views.FormError):
        views.study_answer_body(1, None, "act")
    with pytest.raises(views.FormError):
        views.study_answer_body(1, 3, None)
    assert views.study_answer_body(2, 5, "dismiss") == {"order_no": 2, "rating": 5, "decision": "dismiss"}
    assert views.unfinished_sessions([{"session_id": "S-1", "started_at": "2026-10-12T09:00:00Z", "items": 3,
                                       "answered": 1, "done": False}, {"session_id": "S-2", "done": True}]) == [
        {"session_id": "S-1", "label": "Started 2026-10-12 09:00 UTC: 1 of 3 answered"}]
    rows = views.study_results_rows(api(env, "evaluator").study_results())
    assert [r["arm"] for r in rows] == ["raw alert", "retrieval-only brief", "agent brief"]
    assert [r["answers"] for r in rows] == [1, 1, 1]


def test_the_table_views_match_the_series(env):
    series = api(env).series(env.asset)
    scores = views.score_rows(series)
    assert len(scores) == len(series["scores"]["score"]) and scores[0]["window end"] == views.fmt_time(
        series["scores"]["window_end"][-1])
    probabilities = views.prognosis_rows(series)
    assert len(probabilities) == len(series["prognoses"]["ts"])
    assert all("(" in row[f"P({h_} h)"] for row in probabilities for h_ in views.HORIZONS)


# --- charts.py ------------------------------------------------------------------------------------

def test_the_score_chart_draws_the_score_against_its_threshold(env):
    series = api(env).series(env.asset)
    fig = charts.score_chart(series)
    json.dumps(fig, allow_nan=False)
    score, threshold = fig["data"]
    assert (score["name"], threshold["name"]) == ("score", "threshold")
    assert score["line"] == {"color": charts.SERIES, "width": 2}
    assert threshold["line"]["color"] == charts.REFERENCE and threshold["line"]["dash"] == "dash"
    assert score["x"] == series["scores"]["window_end"] and score["y"] == series["scores"]["score"]
    assert fig["layout"]["hovermode"] == "x unified"
    assert fig["layout"]["yaxis"]["type"] == ("log" if charts.log_scale(score["y"] + threshold["y"]) else "linear")
    empty = charts.score_chart({})
    assert empty["data"] == [] and "No scores" in empty["layout"]["annotations"][0]["text"]


def test_a_gap_breaks_the_line_and_a_wide_range_turns_the_axis_logarithmic():
    times = [f"2020-06-01T00:0{m}:00Z" for m in range(5)] + ["2020-06-01T03:00:00Z", "2020-06-01T03:01:00Z"]
    fig = charts.score_chart({"scores": {"window_end": times, "score": [1, 2, float("nan"), 4, 5, 900, 6],
                                         "threshold": [3] * 7}})
    json.dumps(fig, allow_nan=False)
    y = fig["data"][0]["y"]
    assert len(y) == 8 and y[2] is None and y[5] is None and fig["data"][0]["x"][5] > times[4]
    assert fig["layout"]["yaxis"]["type"] == "log"
    assert charts.log_scale([1, 500]) and not charts.log_scale([1, 50]) and not charts.log_scale([0, 500])
    assert not charts.log_scale([])


def test_every_probability_line_has_its_interval_band(env):
    series = api(env).series(env.asset)
    fig = charts.probability_chart(series)
    json.dumps(fig, allow_nan=False)
    assert len(fig["data"]) == 9
    domains = []
    for i, h_ in enumerate(charts.HORIZONS):
        upper, lower, p = fig["data"][3 * i: 3 * i + 3]
        axis = "" if i == 0 else str(i + 1)
        assert upper["yaxis"] == lower["yaxis"] == p["yaxis"] == "y" + axis
        assert upper["line"]["width"] == 0 and lower["fill"] == "tonexty" and p["name"] == f"P({h_} h)"
        assert len(upper["y"]) == len(lower["y"]) == len(p["y"]) == len(p["x"]) == len(series["prognoses"]["ts"])
        assert all(lo <= mid <= hi for lo, mid, hi in zip(lower["y"], p["y"], upper["y"]) if mid is not None)
        layout_axis = fig["layout"]["yaxis" + axis]
        assert layout_axis["range"] == [0, 1] and layout_axis["title"]["text"] == f"P({h_} h)"
        domains.append(layout_axis["domain"])
        assert fig["layout"]["xaxis" + axis]["showticklabels"] == (i == 2)
    assert all(a[0] >= b[1] for a, b in zip(domains, domains[1:]))           # stacked, no overlap
    assert fig["layout"]["xaxis2"]["matches"] == fig["layout"]["xaxis3"]["matches"] == "x"
    assert charts.probability_chart({"prognoses": {"ts": []}})["data"] == []


def test_the_attribution_chart_puts_the_largest_share_first(env):
    alert = api(env).asset(env.asset)["latest_alert"]
    fig = charts.attribution_chart(alert)
    json.dumps(fig, allow_nan=False)
    bars = fig["data"][0]
    assert bars["type"] == "bar" and bars["orientation"] == "h"
    assert bars["y"] == [a["feature"] for a in alert["attribution"]] and bars["x"] == sorted(bars["x"], reverse=True)
    assert bars["text"][0] == f"{100 * alert['attribution'][0]['share']:.0f} %"
    assert fig["layout"]["yaxis"]["autorange"] == "reversed" and fig["layout"]["showlegend"] is False
    assert charts.attribution_chart(None)["data"] == []


def test_the_trajectory_chart_ends_at_the_opening():
    alert = {"trajectory": [{"minutes_before_opening": m, "score": 10 + m, "threshold": 5, "alert": True}
                            for m in (2.0, 1.0, 0.0)]}
    fig = charts.trajectory_chart(alert)
    assert fig["data"][0]["x"] == [-2.0, -1.0, 0.0] and fig["data"][1]["y"] == [5.0, 5.0, 5.0]
    assert charts.trajectory_chart({})["data"] == []


# --- app.py under the stand-in streamlit ------------------------------------------------------------------

def test_the_page_asks_for_a_token_and_calls_nothing_before(page, monkeypatch):
    monkeypatch.setattr(api_client, "http_client", lambda timeout: pytest.fail("no request before sign-in"))
    fake = page(FakeStreamlit())
    assert fake.input_types["token_input"] == "password" and set(fake.buttons) == {"sign_in"}
    assert views.SIGN_IN_TEXT in fake.text() and "auth" not in fake.session_state


def test_sign_in_and_the_asset_list(page, env):
    fake = signed_in(page)
    assert fake.session_state["auth"] == {"token": AS["rosh"], "name": "rosh", "role": "reviewer"}
    assert fake.session_state["page"] == "Assets"
    holding = [k for k, v in fake.session_state.items() if AS["rosh"] in json.dumps(v)]
    assert holding == ["auth"]                                    # the typed token is kept nowhere else
    rows = views.asset_rows(api(env).assets()["assets"], api(env).incidents(open_only=True)["incidents"])
    text = fake.text()
    assert views.status_line(rows[0]) in text and views.probability_line(rows[0]) in text
    assert f"Note: {views.SKILL_NOTE}." in text and "Signed in as **rosh** (reviewer)" in text
    assert {f"open_asset_{env.asset}", f"open_incident_{env.decide}", f"open_incident_{env.budget}",
            f"open_incident_{env.insufficient}"} <= set(fake.buttons)
    assert "and 1 more on the Review page" in text
    assert AS["rosh"] not in fake.everything()


def test_a_token_the_api_refuses_does_not_sign_in(page):
    fake = page(FakeStreamlit(values={"token_input": "x" * 40}, clicks=["sign_in"]))
    errors = [body for kind, body in fake.out if kind == "error"]
    assert "auth" not in fake.session_state and errors and errors[0].startswith("Sign-in failed")
    assert "HTTP 401" in errors[0] and "x" * 40 not in fake.everything()


def test_one_click_from_the_list_to_the_asset_and_its_charts(page, env):
    fake = show(page, signed_in(page), clicks=[f"open_asset_{env.asset}"])
    assert fake.session_state["page"] == "Asset detail" and fake.session_state["asset_id"] == env.asset
    assert set(fake.figures) == {"chart_score", "chart_probability", "chart_attribution"}
    assert len(fake.figures["chart_probability"]["data"]) == 9
    assert fake.figures["chart_attribution"]["data"][0]["type"] == "bar"
    prognosis = api(env).asset(env.asset)["prognosis"]["horizons"]
    assert f"P(72 h), 90 % interval: {views.fmt_p(prognosis['72'])}" in fake.text()
    assert len(fake.tables) == 3 and f"Review {env.decide} (awaiting review)" in [
        b["label"] for b in fake.buttons.values()]
    fake = show(page, fake, span="1 day")
    assert fake.figures["chart_score"]["data"][0]["x"]


def test_one_click_from_the_list_to_the_incident_and_its_brief(page, env):
    fake = show(page, signed_in(page), clicks=[f"open_incident_{env.insufficient}"])
    assert fake.session_state["page"] == "Review" and fake.session_state["incident_id"] == env.insufficient
    assert views.BANNERS["insufficient"] in [body for kind, body in fake.out if kind == "warning"]
    detail = api(env).incident(env.insufficient)
    expanders = [body for kind, body in fake.out if kind == "expander"]
    for rid, result in detail["cited_results"].items():
        assert f"{rid} · {result['tool']}" in expanders
        assert json.dumps(result) in [body for kind, body in fake.out if kind == "json"]
    assert f"approve_{detail['brief']['brief_id']}" in fake.buttons


def test_each_evidence_state_is_marked_on_the_review_screen(page, env):
    fake = show(page, signed_in(page), page="Review")
    for iid, banner in ((env.sufficient, None), (env.insufficient, views.BANNERS["insufficient"]),
                        (env.budget, views.BANNERS["budget_exhausted"])):
        fake = show(page, fake, incident_id_pick=iid)
        warnings = [body for kind, body in fake.out if kind == "warning"]
        assert warnings == ([banner] if banner else []), iid


def test_approve_from_the_review_screen(page, env):
    fake = show(page, signed_in(page), page="Review", incident_id_pick=env.decide)
    bid = brief_of(env, env.decide)["brief_id"]
    assert not fake.buttons[f"approve_{bid}"]["disabled"] and fake.buttons[f"edit_{bid}"]["disabled"]
    fake = show(page, fake, clicks=[f"approve_{bid}"], **{f"reason_{bid}": "matches the leak signature"})
    rows = env.store.select("decisions", {"incident_id": env.decide})
    assert [(r["action"], r["reviewer"], r["reason"]) for r in rows] == [
        ("approve", "rosh", "matches the leak signature")]
    wo = env.store.select("work_orders", {"incident_id": env.decide})[0]
    successes = [body for kind, body in fake.out if kind == "success"]
    assert successes == [f"Recorded {rows[0]['decision_id']}: approve by rosh. Work order {wo['wo_id']} is "
                         "queued, waiting for its first send."]
    assert f"Work order {wo['wo_id']} is queued, waiting for its first send." in fake.text()
    assert f"approve_{bid}" not in fake.buttons and f"audit_from_review_{rows[0]['decision_id']}" in fake.buttons
    assert any(r.get("work order") == wo["wo_id"] for table in fake.tables for r in table)


def test_a_decision_without_a_reason_is_not_sent(page, env):
    fake = show(page, signed_in(page), page="Review", incident_id_pick=env.decide)
    bid = brief_of(env, env.decide)["brief_id"]
    fake = show(page, fake, clicks=[f"reject_{bid}"])
    assert "Give a reason: every decision needs one." in [body for kind, body in fake.out if kind == "error"]
    assert env.store.count("decisions") == 0
    assert api(env).incident(env.decide)["incident"]["state"] == "awaiting_review"


def test_the_work_order_is_edited_in_place_and_sent_with_the_diff_shown(page, env):
    fake = show(page, signed_in(page), page="Review", incident_id_pick=env.decide)
    brief = brief_of(env, env.decide)
    bid, draft = brief["brief_id"], brief["document"]["work_order"]
    fields = views.work_order_fields(draft)
    tasks = fields["tasks"] + "\nCheck the drain valve"
    fake = show(page, fake, **{f"wo_tasks_{bid}": tasks})
    preview = [body for kind, body in fake.out if kind == "code"]
    edited = views.work_order_from_form(draft, fields["title"], fields["priority"], tasks, fields["safety_notes"])
    expected = views.work_order_diff(draft, edited)
    assert preview == [expected]
    assert fake.buttons[f"approve_{bid}"]["disabled"] and not fake.buttons[f"edit_{bid}"]["disabled"]
    fake = show(page, fake, clicks=[f"edit_{bid}"], **{f"reason_{bid}": "add the drain check"})
    decision = env.store.select("decisions", {"incident_id": env.decide})[0]
    assert decision["action"] == "edit" and decision["diff"] == expected and decision["reviewer"] == "rosh"
    body = env.store.select("work_orders", {"incident_id": env.decide})[0]["body"]
    assert body["tasks"][-1] == "Check the drain valve"


def test_an_engineer_reads_the_brief_but_cannot_decide(page, env):
    fake = show(page, signed_in(page, "engineer"), page="Review", incident_id_pick=env.decide)
    bid = brief_of(env, env.decide)["brief_id"]
    assert "Only a reviewer can approve, edit or reject; you are signed in as engineer." in fake.text()
    assert not {f"approve_{bid}", f"edit_{bid}", f"reject_{bid}"} & set(fake.buttons)


def test_a_failed_or_blocked_work_order_is_shown_with_its_error(page, env):
    out = api(env).decide(brief_of(env, env.decide)["brief_id"], {"action": "approve", "reason": "ok"})
    wo_id = out["work_order"]["wo_id"]
    env.store.update("work_orders", {"wo_id": wo_id}, {"status": "failed", "attempts": 2,
                                                       "last_error": "HTTP 503 from the work-order endpoint"})
    fake = show(page, signed_in(page), page="Review", queue="not closed")          # approved: not awaiting review
    fake = show(page, fake, incident_id_pick=env.decide)
    assert (f"Work order {wo_id} failed on attempt 2: HTTP 503 from the work-order endpoint. The connector will try "
            "again.") in [body for kind, body in fake.out if kind == "warning"]
    assert any(r.get("status") == views.WORK_ORDER_STATUS["failed"] and r.get("last error", "").startswith("HTTP 503")
               for table in fake.tables for r in table)
    env.store.update("work_orders", {"wo_id": wo_id}, {"status": "blocked", "last_error": "tampered: the hash differs"})
    fake = page(fake)
    errors = [body for kind, body in fake.out if kind == "error"]
    assert any(body.startswith(f"Work order {wo_id} is blocked: tampered") for body in errors)


def test_the_audit_page_rebuilds_a_decision_and_flags_a_broken_record(page, env):
    fake = show(page, signed_in(page), page="Review", incident_id_pick=env.decide)
    bid = brief_of(env, env.decide)["brief_id"]
    fake = show(page, fake, clicks=[f"approve_{bid}"], **{f"reason_{bid}": "ok"})
    decision = env.store.select("decisions", {"incident_id": env.decide})[0]
    fake = show(page, fake, clicks=[f"audit_from_review_{decision['decision_id']}"])
    assert fake.session_state["page"] == "Audit" and fake.session_state["decision_id"] == decision["decision_id"]
    successes = [body for kind, body in fake.out if kind == "success"]
    assert any("still hashes to the recorded SHA-256" in s for s in successes)
    assert "The incident's 4 events replay to its stored state, approved." in successes
    env.store.insert("decisions", dict(decision, decision_id="D-000000000000", evidence_sha256="0" * 64))
    fake = show(page, fake, decision_id="D-000000000000")
    assert any("no longer matches its record" in body for kind, body in fake.out if kind == "error")


def test_the_study_page_asks_consent_shows_blind_items_and_thanks(page, env):
    fake = show(page, signed_in(page), page="Study")
    text = fake.text()
    assert all(paragraph in text for paragraph in views.CONSENT_TEXT.split("\n\n"))
    assert fake.buttons["study_start"]["disabled"] and env.store.count("study_sessions") == 0
    fake = show(page, fake, clicks=["study_start"], study_consent=True)
    sid = fake.session_state["study_session"]
    assert fake.widgets["page"]["options"] == ["Study"]                 # study mode: the other pages are hidden
    hidden = [b["brief_id"] for b in env.store.select("briefs")] + [b["trace_id"] for b in env.store.select("briefs")]
    kinds = set()
    for n in (1, 2, 3):
        assert f"Item {n} of 3" in fake.text() and any(re.fullmatch(r"Condition [ABC]", body)
                                                     for kind, body in fake.out if kind == "subheader")
        words = fake.page_words()
        assert not ARM_WORDS.search(words), ARM_WORDS.search(words)
        everything = fake.text()
        assert "scripted" not in everything and not [x for x in hidden if x in everything]
        kinds.add("alert" if "chart_trajectory" in fake.figures else "brief")
        assert fake.buttons[f"study_submit_{sid}_{n}"]["disabled"]
        fake = show(page, fake, clicks=[f"study_submit_{sid}_{n}"],
                    **{f"study_rating_{sid}_{n}": 4, f"study_decision_{sid}_{n}": "act"})
    assert kinds == {"alert", "brief"} and views.THANK_YOU in fake.text()
    answers = [r for r in env.store.select("study_sessions", {"session_id": sid}) if r["rating"] is not None]
    assert [(r["order_no"], r["rating"], r["decision"]) for r in answers] == [(n, 4, "act") for n in (1, 2, 3)]
    fake = show(page, fake, clicks=["study_leave"])
    assert fake.widgets["page"]["options"] == list(("Assets", "Asset detail", "Review", "Audit", "Study"))


def test_a_participant_can_stop_and_carry_on(page, env):
    fake = show(page, signed_in(page), page="Study")
    fake = show(page, fake, clicks=["study_start"], study_consent=True)
    sid = fake.session_state["study_session"]
    fake = show(page, fake, clicks=[f"study_submit_{sid}_1"], **{f"study_rating_{sid}_1": 2,
                                                                 f"study_decision_{sid}_1": "dismiss"})
    fake = show(page, fake, clicks=["study_stop"])
    assert "study_session" not in fake.session_state and not fake.session_state["study_consent"]
    assert fake.buttons["study_start"]["disabled"]                       # a new session asks for consent again
    fake = show(page, fake, clicks=[f"study_resume_{sid}"])
    assert "Item 2 of 3" in fake.text()


def test_the_evaluator_reads_the_results_by_arm(page, env):
    walk(env.client, "ashok", env.clock, env.store)
    fake = show(page, signed_in(page, "evaluator"), page="Study")
    rows = fake.tables[0]
    assert [r["arm"] for r in rows] == ["raw alert", "retrieval-only brief", "agent brief"]
    assert [r["answers"] for r in rows] == [1, 1, 1] and "1 session, 1 participant, 3 answers" in fake.text()


def test_an_expired_token_offers_to_sign_in_again(page, env):
    fake = signed_in(page)
    del env.tokens[AS["rosh"]]
    fake = page(fake)
    assert any("did not accept the token" in body for kind, body in fake.out if kind == "error")
    fake = show(page, fake, clicks=["sign_in_again"])
    assert "auth" not in fake.session_state and set(fake.buttons) == {"sign_in"}
