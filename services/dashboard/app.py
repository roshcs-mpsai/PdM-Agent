"""PdM-Agent operator dashboard (T18, T19, T34; HIL-01 to HIL-04, HIL-07 to HIL-09, NFR-09, PR-16).

    pip install -e lib -r services/dashboard/requirements.txt
    PDM_API_URL=http://localhost:8000 streamlit run services/dashboard/app.py

Five pages, all reading and writing through the HTTP API (services/api) as the person signed in:
Assets, Asset detail, Review, Audit and Study (README.md says what each shows). This file is the
Streamlit wiring only: page content comes from views.py, figures from charts.py and every request
from api_client.py; tests/test_dashboard.py runs this file under a stand-in streamlit module
against the real API.

Sign-in: the person's own API token in a password field, checked with GET /me and kept in this
browser session's state only; it never goes into a log, a URL or the page. During a study session
the other pages are hidden, so a participant cannot open the live brief of the incident in front of
them.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
import api_client  # noqa: E402
import charts  # noqa: E402
import views  # noqa: E402

API_URL = os.environ.get("PDM_API_URL") or api_client.DEFAULT_URL
PAGES = ("Assets", "Asset detail", "Review", "Audit", "Study")
STATE = st.session_state


# --- session state and navigation -------------------------------------------------------------------

def client() -> api_client.ApiClient:
    return api_client.ApiClient(API_URL, STATE["auth"]["token"])


def flash(kind: str, text: str) -> None:
    """A message for the top of the next page run (kind: success, info, warning or error)."""
    STATE["flash"] = list(STATE.get("flash") or []) + [(kind, text)]


def show_flash() -> None:
    for kind, text in STATE.get("flash") or []:
        getattr(st, kind)(text)
    STATE["flash"] = []


def go(page: str, **selected) -> None:
    """Button callback: open ``page`` with an asset, incident or decision selected."""
    STATE["page"] = page
    for key, value in selected.items():
        STATE[key] = value


def sign_in() -> None:
    token = (STATE.get("token_input") or "").strip()
    STATE["token_input"] = ""
    if not token:
        flash("error", "Enter your API token to sign in.")
        return
    try:
        who = api_client.ApiClient(API_URL, token).me()
    except api_client.ApiError as exc:
        flash("error", f"Sign-in failed: {exc}")
        return
    STATE["auth"] = {"token": token, "name": who.get("name"), "role": who.get("role")}
    STATE["page"] = PAGES[0]


def sign_out() -> None:
    for key in list(STATE.keys()):
        del STATE[key]


def start_study() -> None:
    try:
        out = client().study_start(consent=bool(STATE.get("study_consent")))
    except api_client.ApiError as exc:
        flash("error", f"The study could not start: {exc}")
        return
    STATE["study_session"], STATE["study_item"], STATE["page"] = out["session_id"], None, "Study"


def resume_study(session_id: str) -> None:
    STATE["study_session"], STATE["study_item"], STATE["page"] = session_id, None, "Study"


def leave_study() -> None:
    for key in ("study_session", "study_item"):
        if key in STATE:
            del STATE[key]
    STATE["study_consent"] = False


def pick(label: str, options: list, state_key: str):
    """A selectbox whose choice lives in STATE[state_key], not in the widget: the list changes as
    incidents arrive, and Streamlit may then draw the widget anew at its first option."""
    if STATE.get(state_key) not in options:
        STATE[state_key] = options[0]
    choice = st.selectbox(label, options, index=options.index(STATE[state_key]), key=f"{state_key}_pick")
    STATE[state_key] = choice
    return choice


def table(rows: list, empty: str = "Nothing to show.") -> None:
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True)
    else:
        st.caption(empty)


def sidebar():
    """The sign-in form, or who is signed in and the page picker; returns the page or None."""
    st.sidebar.title("PdM-Agent")
    auth = STATE.get("auth")
    if not auth:
        st.sidebar.text_input("API token", type="password", key="token_input")
        st.sidebar.button("Sign in", key="sign_in", on_click=sign_in, type="primary")
        st.sidebar.caption(f"API: {API_URL}")
        return None
    st.sidebar.markdown(f"Signed in as **{views.md(auth['name'])}** ({views.md(auth['role'])})")
    st.sidebar.button("Sign out", key="sign_out", on_click=sign_out)
    pages = ("Study",) if STATE.get("study_session") else PAGES
    if STATE.get("page") not in pages:
        STATE["page"] = pages[0]
    page = st.sidebar.radio("Page", pages, key="page")
    if STATE.get("study_session"):
        st.sidebar.caption("Study mode: the other pages come back when you stop or finish the session.")
    return page


# --- shared pieces ---------------------------------------------------------------------------------

def show_citation(api, cite: dict) -> None:
    """A citation, expandable to its stored tool result (fetched when the page did not get it)."""
    with st.expander(cite["title"]):
        result = cite["result"]
        if result is None and api is not None:
            try:
                result = api.result(cite["id"])
            except api_client.ApiError as exc:
                st.error(str(exc))
                return
        st.json(result)


def show_brief(api, brief: dict) -> None:
    """A brief, read-only: banner, summary, hypotheses with their citations, prognosis, action, checks."""
    if brief["banner"]:
        st.warning(brief["banner"])
    st.markdown("**Summary**")
    st.markdown(views.md(brief["summary"]))
    st.markdown("**Hypotheses**")
    if not brief["hypotheses"]:
        st.caption("No hypothesis: this brief makes no claim.")
    for h in brief["hypotheses"]:
        st.markdown(f"**{views.md(h['title'])}**: {views.md(h['statement'])}")
        for side, claims in (("for", h["claims"]), ("against", h["against"])):
            for claim in claims:
                ids = ", ".join(c["id"] for c in claim["citations"])
                st.markdown(f"- {side}: {views.md(claim['text'])} (cites {views.md(ids)})")
                for cite in claim["citations"]:
                    show_citation(api, cite)
    prognosis = brief["prognosis"]
    ids = ", ".join(c["id"] for c in prognosis["citations"])
    st.markdown(f"**Prognosis**, cited from {views.md(ids)}")
    if prognosis["available"]:
        for col, h in zip(st.columns(len(views.HORIZONS)), views.HORIZONS):
            col.metric(f"P({h} h), 90 % interval", prognosis["cells"][h])
        if prognosis["made"]:
            st.caption(prognosis["made"])
        if prognosis["note"]:
            st.caption(f"Note: {prognosis['note']}.")
    else:
        st.caption(views.md(prognosis["text"]))
    for cite in prognosis["citations"]:
        show_citation(api, cite)
    action = brief["action"]
    st.markdown(f"**Recommended action**: {views.md(action['type'])}. {views.md(action['text'])}")
    for cite in action["citations"]:
        show_citation(api, cite)
    st.markdown("**Checks run**")
    table(brief["checks"], "No check was run.")


def show_work_orders(rows: list) -> None:
    for row in rows or ():
        kind, text = views.work_order_note(row)
        getattr(st, kind)(text)


# --- pages ----------------------------------------------------------------------------------------

def page_assets(api) -> None:
    st.title("Assets")
    st.caption("Highest P(72 h) first. Every probability is shown with its 90 % interval.")
    rows = views.asset_rows(api.assets()["assets"], api.incidents(open_only=True)["incidents"])
    if not rows:
        st.info("No asset has reported yet.")
    for row in rows:
        with st.container(border=True):
            dot, body, actions = st.columns([1, 14, 5])
            dot.markdown(views.dot_html(row["hex"]), unsafe_allow_html=True)
            body.markdown(views.status_line(row))
            body.markdown(views.probability_line(row))
            body.caption(views.activity_line(row))
            if row["skill_note"]:
                body.caption(f"Note: {row['skill_note']}.")
            actions.button("Asset detail", key=f"open_asset_{row['asset_id']}", on_click=go,
                           args=("Asset detail",), kwargs={"asset_id": row["asset_id"]})
            for iid in row["incidents"][:3]:
                actions.button(f"Review {iid}", key=f"open_incident_{iid}", on_click=go, args=("Review",),
                               kwargs={"incident_id": iid})
            if len(row["incidents"]) > 3:
                actions.caption(f"and {len(row['incidents']) - 3} more on the Review page")
    st.button("Refresh", key="refresh_assets")


def page_asset_detail(api) -> None:
    st.title("Asset detail")
    ids = [a["asset_id"] for a in api.assets()["assets"]]
    if not ids:
        st.info("No asset has reported yet.")
        return
    asset_id = pick("Asset", ids, "asset_id")
    span = st.radio("Span", list(views.SPANS), index=1, horizontal=True, key="span")
    detail = api.asset(asset_id)
    head = views.asset_header(detail)
    dot, body = st.columns([1, 19])
    dot.markdown(views.dot_html(head["hex"]), unsafe_allow_html=True)
    body.markdown(views.status_line(head))
    body.caption(f"{head['score']} · status since {head['status_at']} · last update {head['last_update']}")
    for col, h in zip(st.columns(len(views.HORIZONS)), views.HORIZONS):
        col.metric(f"P({h} h), 90 % interval", head[f"p{h}"])
    if head["skill_note"]:
        st.info(f"Note: {head['skill_note']}.")
    series = api.series(asset_id, *views.span_bounds(detail.get("last_update"), views.SPANS[span]))
    st.plotly_chart(charts.score_chart(series), key="chart_score")
    with st.expander("Table view: scores"):
        table(views.score_rows(series), "No scores in this span.")
    st.plotly_chart(charts.probability_chart(series), key="chart_probability")
    with st.expander("Table view: probabilities"):
        table(views.prognosis_rows(series), "No prognosis in this span.")
    st.plotly_chart(charts.attribution_chart(detail.get("latest_alert")), key="chart_attribution")
    with st.expander("Table view: attribution"):
        table(views.attribution_rows(detail.get("latest_alert")), "No alert yet.")
    st.subheader("Open incidents")
    if not detail.get("incidents"):
        st.caption("None.")
    for inc in detail.get("incidents") or ():
        st.button(f"Review {inc['incident_id']} ({inc['state'].replace('_', ' ')})",
                  key=f"detail_incident_{inc['incident_id']}", on_click=go, args=("Review",),
                  kwargs={"incident_id": inc["incident_id"]})


def decision_form(api, auth: dict, brief: dict) -> None:
    """The draft work order edited in place and approve, edit or reject with a reason (HIL-03)."""
    st.subheader("Decision")
    if auth["role"] != "reviewer":
        st.info(f"Only a reviewer can approve, edit or reject; you are signed in as {auth['role']}.")
        return
    bid, draft = brief["brief_id"], brief["work_order"]
    fields = views.work_order_fields(draft)
    title = st.text_input("Work order title", value=fields["title"], key=f"wo_title_{bid}")
    priority = st.selectbox("Priority", views.PRIORITIES, index=views.PRIORITIES.index(fields["priority"]),
                            key=f"wo_priority_{bid}")
    tasks = st.text_area("Tasks, one per line", value=fields["tasks"], key=f"wo_tasks_{bid}")
    safety = st.text_area("Safety notes, one per line", value=fields["safety_notes"], key=f"wo_safety_{bid}")
    edited = views.work_order_from_form(draft, title, priority, tasks, safety)
    changed = views.work_order_changed(draft, edited)
    if changed:
        st.caption("Your changes to the draft, sent with the decision as an edit:")
        st.code(views.work_order_diff(draft, edited), language="diff")
        for problem in views.work_order_problems(edited):
            st.warning(f"Work order: {problem}.")
    reason = st.text_area("Reason (every decision needs one)", key=f"reason_{bid}")
    approve_col, edit_col, reject_col = st.columns(3)
    action = None
    if approve_col.button("Approve the draft", key=f"approve_{bid}", type="primary", disabled=changed):
        action = "approve"
    if edit_col.button("Approve with my edits", key=f"edit_{bid}", disabled=not changed):
        action = "edit"
    if reject_col.button("Reject", key=f"reject_{bid}"):
        action = "reject"
    if action is None:
        return
    try:
        out = api.decide(bid, views.decision_body(action, reason, draft, edited if action == "edit" else None))
    except views.FormError as exc:
        st.error(str(exc))
        return
    except api_client.ApiError as exc:
        st.error(f"The decision was not recorded: {exc}")
        return
    flash("success", views.decision_done(out))
    st.rerun()


def page_review(api, auth: dict) -> None:
    st.title("Review")
    queue = st.radio("Queue", list(views.QUEUES), horizontal=True, key="queue")
    rows = views.incident_rows(api.incidents(**views.QUEUES[queue])["incidents"])
    table(rows, "No incident in this queue.")
    ids = [r["incident"] for r in rows]
    if STATE.get("incident_id") and STATE["incident_id"] not in ids:
        ids.insert(0, STATE["incident_id"])                  # the incident opened from elsewhere, or just decided
    if ids:
        detail = api.incident(pick("Incident", ids, "incident_id"))
        head = views.incident_header(detail)
        st.subheader(head["incident_id"])
        st.markdown(f"**{views.md(head['asset_id'])}** · opened {head['opened']} · {views.md(head['state_label'])}")
        if head["cause"]:
            st.caption(views.md(head["cause"]))
        brief = views.incident_brief(detail)
        if brief is None:
            st.info("The agent has not written a brief for this incident yet; it stays on the list as a raw alert.")
        else:
            show_brief(api, brief)
            st.markdown("**Draft work order**")
            for line in views.work_order_lines(brief["work_order"]):
                st.markdown(line)
            if head["state"] == "awaiting_review":
                decision_form(api, auth, brief)
        for d in views.decision_rows(detail.get("decisions")):
            st.markdown(f"**{views.md(d['decision'])}**: {d['action']} by {views.md(d['reviewer'])} at {d['at']}. "
                        f"Reason: {views.md(d['reason'])}")
            st.button("Audit this decision", key=f"audit_from_review_{d['decision']}", on_click=go,
                      args=("Audit",), kwargs={"decision_id": d["decision"]})
        show_work_orders(detail.get("work_orders"))
    st.subheader("Work orders")
    st.caption("Status as the connector last left it: queued, sent, failed (it retries) or blocked "
               "(it needs a person).")
    table(views.work_order_rows(api.work_orders()["work_orders"]), "No work order yet.")


def page_audit(api) -> None:
    st.title("Audit")
    st.caption("A past decision rebuilt from the store: the brief and evidence as decided on, whether they still "
               "hash to the record, the agent's run, and whether the incident's events replay to its state.")
    st.text_input("Decision id", key="decision_id")
    decided = [i for i in api.incidents()["incidents"] if i.get("decision")][:10]
    for inc in decided:
        d = inc["decision"]
        st.button(f"{d['decision_id']}: {d['action']} on {inc['incident_id']}", key=f"audit_{d['decision_id']}",
                  on_click=go, args=("Audit",), kwargs={"decision_id": d["decision_id"]})
    decision_id = (STATE.get("decision_id") or "").strip()
    if not decision_id:
        st.caption("Enter a decision id, or pick a recent decision above.")
        return
    view = views.audit_view(api.decision(decision_id))
    d = view["decision"]
    st.subheader(d["decision_id"])
    st.markdown(f"**{d['action']}** by **{views.md(d['reviewer'])}** at {d['at']}, on brief {views.md(d['brief_id'])} "
                f"of {views.md(d['incident_id'])}. Reason: {views.md(d['reason'])}")
    (st.success if view["evidence_ok"] else st.error)(view["evidence_text"])
    (st.success if view["events_ok"] else st.error)(view["events_text"])
    if view["work_order"]:
        st.markdown("**Approved work order**")
        for line in views.work_order_lines(view["work_order"]):
            st.markdown(line)
    if view["diff"]:
        st.code(view["diff"], language="diff")
    if view["work_order_row"]:
        show_work_orders([view["work_order_row"]])
    st.markdown("**Versions**")
    st.json(d["versions"])
    if view["brief"]:
        st.markdown("**The brief as decided on**")
        show_brief(None, view["brief"])
    if view["trace"]:
        t = view["trace"]
        st.markdown("**The agent's run**")
        st.caption(views.md(f"trace {t['trace_id']}, outcome {t['outcome']}, model {t['model']}, "
                            f"{t['started']} to {t['finished']}"))
        table(t["calls"], "No tool call.")
    st.markdown("**Incident events**")
    table(view["events"])


def consent_screen(api) -> None:
    st.subheader("Before you start")
    for paragraph in views.CONSENT_TEXT.split("\n\n"):
        st.markdown(paragraph)
    agree = st.checkbox("I have read this and agree to take part.", key="study_consent")
    st.button("Start the study", key="study_start", type="primary", disabled=not agree, on_click=start_study)
    unfinished = views.unfinished_sessions(api.study_sessions()["sessions"])
    if unfinished:
        st.markdown("**Or carry on a session you started**")
    for s in unfinished:
        st.button(s["label"], key=f"study_resume_{s['session_id']}", on_click=resume_study, args=(s["session_id"],))


def page_study(api, auth: dict) -> None:
    st.title("Study")
    if auth["role"] == "evaluator":
        results = api.study_results()
        st.caption(views.study_results_line(results))
        table(views.study_results_rows(results))
        return
    if auth["role"] != "reviewer":
        st.info("The study is for reviewers; its results are for evaluators.")
        return
    session_id = STATE.get("study_session")
    if not session_id:
        consent_screen(api)
        return
    if STATE.get("study_item") is None:
        STATE["study_item"] = api.study_next(session_id)
    view = views.study_item_view(STATE["study_item"])
    if view["done"]:
        st.success(views.THANK_YOU)
        st.button("Leave study mode", key="study_leave", on_click=leave_study)
        return
    n = view["order_no"]
    st.caption(view["progress"])
    st.subheader(view["label"])
    st.markdown(views.md(view["header"]))
    if view["kind"] == "alert":
        alert = view["alert"]
        st.markdown(f"**Alarm**: {views.md(alert['cause'])}")
        if alert["gate"]:
            st.caption(alert["gate"])
        st.plotly_chart(charts.trajectory_chart(alert), key="chart_trajectory")
        st.markdown("**Checks at the opening**")
        table(alert["checks"], "No check was recorded at the opening.")
    else:
        show_brief(None, view["brief"])
        st.markdown("**Draft work order**")
        for line in views.work_order_lines(view["brief"]["work_order"]):
            st.markdown(line)
    rating = st.radio("How useful is this for deciding what to do?", list(views.RATINGS), index=None,
                      format_func=views.RATINGS.get, horizontal=True, key=f"study_rating_{session_id}_{n}")
    decision = st.radio("What would you do?", list(views.STUDY_DECISIONS), index=None,
                        format_func=views.STUDY_DECISIONS.get, horizontal=True, key=f"study_decision_{session_id}_{n}")
    submit_col, stop_col = st.columns(2)
    submit = submit_col.button("Submit and go on", key=f"study_submit_{session_id}_{n}", type="primary",
                               disabled=rating is None or decision is None)
    stop_col.button("Stop for now", key="study_stop", on_click=leave_study)
    if not submit:
        return
    try:
        api.study_answer(session_id, views.study_answer_body(n, rating, decision))
    except (views.FormError, api_client.ApiError) as exc:
        st.error(f"The answer was not recorded: {exc}")
        return
    STATE["study_item"] = None
    st.rerun()


def main() -> None:
    st.set_page_config(page_title="PdM-Agent", layout="wide")
    page = sidebar()
    show_flash()
    if page is None:
        st.title("PdM-Agent")
        st.markdown(views.SIGN_IN_TEXT)
        return
    auth, api = STATE["auth"], client()
    try:
        if page == "Assets":
            page_assets(api)
        elif page == "Asset detail":
            page_asset_detail(api)
        elif page == "Review":
            page_review(api, auth)
        elif page == "Audit":
            page_audit(api)
        else:
            page_study(api, auth)
    except api_client.ApiError as exc:
        st.error(str(exc))
        if exc.status == 401:
            st.button("Sign in again", key="sign_in_again", on_click=sign_out)


main()
