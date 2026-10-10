# Dashboard

The operator dashboard: five Streamlit pages that read and write only through the
HTTP API (services/api), as the person signed in (T18, T19, T34; HIL-01 to HIL-04,
HIL-07 to HIL-09, NFR-09, PR-16).

```bash
pip install -e lib -r services/dashboard/requirements.txt
PDM_API_URL=http://localhost:8000 streamlit run services/dashboard/app.py
```

`PDM_API_URL` (default `http://localhost:8000`) and `PDM_API_TIMEOUT` (seconds per
request, default 10). The API has to be running (services/api/README.md); for the
study page the API also needs `PDM_STUDY_SALT`.

A local run on a replayed store, with a scripted brief for each incident:

```bash
python eval/pipeline.py --data "data/MetroPT3(AirCompressor).csv" --from 2020-07-13 --to 2020-07-16 \
    --store sqlite:///runs/replay.db
python services/agent/agent.py --profile profiles/metropt3_apu/asset_profile.yaml --store sqlite:///runs/replay.db \
    --scripted services/agent/scripts/dry_run.json --once INC-...          # once per incident
export PDM_TOKEN_REVIEWER_ROSH=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
export PDM_TOKEN_EVALUATOR=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
export PDM_STUDY_SALT=$(python -c "import secrets; print(secrets.token_urlsafe(24))")
python services/api/main.py --store sqlite:///runs/replay.db &
streamlit run services/dashboard/app.py
```

The study page needs, for each incident, a retrieval-only brief next to the live one:
`agent.py ... --study INC-... --arm retrieval_only` with the model
(`ANTHROPIC_API_KEY`). Without a key, `--scripted services/agent/scripts/study_dry_run.json`
writes one (one `search_knowledge` call, then a brief that cites only the incident
context); `dry_run.json` is for the full arm only.

## Sign-in

Paste your own API token into the password field in the sidebar
(`PDM_TOKEN_REVIEWER_<NAME>` for a reviewer, docs/SECURITY.md). The dashboard asks
`GET /me` whose token it is and shows the name and role; the API records that name on
every decision (HIL-07). The token stays in the browser session's state and goes out
only in the `Authorization` header: never in a URL, a log line, an error message or
the page. Sign out clears the session. When the API stops accepting the token (401)
the page says so and offers to sign in again. Only a reviewer gets the decision
controls; the API refuses a decision from any other role anyway.

## Pages

**Assets** (HIL-01). One block per asset, highest P(72 h) first, in the order the
API gives: the NE 107 state in words beside its marker (green, amber or red; grey
before the first status) and its cause; P(24 h), P(72 h) and P(168 h), each written
with its 90 % interval as `0.12 (0.05-0.31)` (a probability without both bounds is
shown as `n/a (no interval)`, never alone); the note "the survival model does not
beat a constant hazard; read the probabilities as context" whenever the API says
the model misses its skill bar; the open incidents and the last update. One click
opens the asset, or one of its three newest open incidents on the review screen.

**Asset detail** (HIL-02). The same header, then three charts over 1, 7 or 30 days
of stream time: the anomaly score against its threshold; the probability of failure
within 24, 72 and 168 h, one panel per horizon, each line inside its interval band;
each feature's share of the latest alert's score. Each chart has a table view under
it. Then the asset's open incidents, one click each to review.

**Review** (HIL-03, HIL-04, INT-04). The queue (awaiting review, open with no brief
yet, not closed, all), and one incident: the gate's cause, a banner for an
insufficient brief and a different one for a budget-exhausted brief, the summary,
each hypothesis with its claims and counter-claims, every citation expandable to the
stored tool result, the prognosis read from the context result the brief cites, the
recommended action, the checks run and the draft work order. While the incident
awaits review, a reviewer edits the draft in place (the diff against the draft shows
as it is edited), writes the reason, and approves the draft, approves with the
edits, or rejects, on the same screen. Afterwards the screen shows the decision, a
link to its audit, and the work order's status as the connector left it: queued,
sent (with the reference), failed (with the last error; the connector retries) or
blocked (with the last error; a person has to look at it). The table of every work
order is at the bottom.

**Audit** (HIL-08). Any past decision, by id or from the recent ones, rebuilt from
the store: the action, reviewer, time and reason; whether the evidence the reviewer
saw still hashes to the recorded value; whether the incident's events replay to its
stored state; the approved work order and its diff; the versions; the brief with
its citations; the agent's tool calls and the incident's events.

**Study** (HIL-09). For a reviewer: the consent text first (what is recorded, the
pseudonym, stopping at any time), then the items one at a time, each under a letter
(A, B or C), with the 1 to 5 usefulness rating and act, dismiss or unsure, then a
thank-you. An item of the raw arm shows the alarm, its cause, the scores up to the
opening as a chart and the checks at the opening; an item of either agent arm shows
the brief as the review screen does, read-only. Nothing on the page names an arm:
the API strips the brief's id, versions and trace and relabels its citations E1,
E2, ... (services/api/study.py). While a session is open the sidebar offers only
this page, so the live review of the incident on screen is out of reach; "Stop for
now" leaves study mode and the session can be carried on later from the same page.
The time to answer is measured by the API. For an evaluator: the results by true
arm, in aggregate.

## Code

| File | |
|---|---|
| `app.py` | the Streamlit wiring, and nothing else |
| `views.py` | pure functions from the API's JSON to what each page shows: rows, labels, marker colours, probabilities with intervals, banners, citations, the decision body, the work-order diff, the study item |
| `charts.py` | Plotly figures as plain dicts, which `st.plotly_chart` takes |
| `api_client.py` | one method per endpoint; a refused request raises `ApiError` with the status and the API's message |
| `status_view.py` | the R0 status board, still the printed fallback: `python services/dashboard/status_view.py [--watch 2]` |

Text from the API reaches Markdown only through `views.md`, which escapes it: a
brief is written by a language model and may quote retrieved text, so a link or an
image in it shows as text and is never loaded. The only HTML on the page is the
marker dot, whose colour comes from a fixed table.

## Tests, and what was not run here

`tests/test_dashboard.py` runs `api_client.py` against the real API in process
(starlette's TestClient) over a store with real incidents and agent briefs, checks
`views.py` and `charts.py` on the API's JSON, and runs `app.py` under a stand-in
`streamlit` module. The stand-in has the signatures of the Streamlit calls the page
makes, plays back typed values and clicks, runs button callbacks and reruns as
Streamlit does, and refuses what Streamlit refuses: a duplicate widget key, a
widget's state changed after the widget is drawn, a value that is not one of the
options. The flows covered: sign-in and a refused token; asset list to asset detail
and to an incident in one click; the banner of each evidence state; approve; edit in
place with the diff shown and stored; a decision without a reason; an engineer
without decision controls; failed and blocked work orders; the audit of an intact
and of a broken record; a study session from consent to thank-you with no arm on
the page; stopping and carrying on; the evaluator's results; an expired token.

Streamlit and Plotly were not installed in the build environment. The logic and the
page wiring were run under the stand-in; the page itself has not been opened in a
browser, and the figures were checked as dicts, not drawn by Plotly. Before the demo,
run the dashboard against the API on a replayed store and click through each page.
