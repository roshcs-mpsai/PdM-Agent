"""Plotly figures for the dashboard, as plain dicts (T19; HIL-02, NFR-09).

st.plotly_chart takes a {"data": [...], "layout": {...}} dict, so the figures are built here and
tested without plotly. Each one takes the API's JSON:

* ``score_chart``: the anomaly score against its threshold over time (GET /assets/{id}/series);
* ``probability_chart``: P(failure within 24, 72 and 168 h) from the same series, one panel per
  horizon on a shared time axis, each probability drawn with its 90 % interval as a band; there is
  no probability line without its band (NFR-09);
* ``attribution_chart``: each feature's share of the latest alert's score (GET /assets/{id});
* ``trajectory_chart``: the scores up to an incident's opening (a study item's raw alert).

Colours: one series hue (the first slot of the validated categorical palette) and a muted ink for
the threshold, which is a reference, not a series. Backgrounds, grid and fonts are left to
Streamlit's theme, so the figures follow its light and dark modes. A gap in the data longer than
five median steps breaks the line instead of drawing a straight line across it. A score axis turns
logarithmic when the plotted values span more than a factor of 100.
"""
from __future__ import annotations

import math
import statistics
from typing import Optional

from pdm_common.timeutil import iso_utc, to_utc

SERIES = "#2a78d6"
BAND = "rgba(42, 120, 214, 0.14)"
REFERENCE = "#898781"
HORIZONS = ("24", "72", "168")
GAP_STEPS = 5
LOG_SPAN = 100


def _clean(values) -> list:
    """Numbers as floats, missing or non-finite values as None (Plotly breaks the line there)."""
    out = []
    for v in values or ():
        out.append(float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                   else None)
    return out


def _time(value):
    try:
        return to_utc(value) if value else None
    except (TypeError, ValueError):
        return None


def break_gaps(x: list, *columns: list) -> tuple[list, list]:
    """(x, columns) with an empty point in every time gap longer than GAP_STEPS median steps."""
    times = [_time(t) for t in x]
    steps = [(b - a).total_seconds() for a, b in zip(times, times[1:]) if a and b and b > a]
    columns = [list(c) + [None] * (len(x) - len(c)) for c in columns]
    if not steps:
        return list(x), columns
    limit = GAP_STEPS * statistics.median(steps)
    out_x, out = [], [[] for _ in columns]
    for i, t in enumerate(x):
        if i and times[i] and times[i - 1] and (times[i] - times[i - 1]).total_seconds() > limit:
            out_x.append(iso_utc(times[i - 1] + (times[i] - times[i - 1]) / 2))
            for col in out:
                col.append(None)
        out_x.append(t)
        for col, values in zip(out, columns):
            col.append(values[i])
    return out_x, out


def log_scale(values) -> bool:
    """A log axis when every value is positive and they span more than LOG_SPAN times."""
    found = [v for v in _clean(values) if v is not None]
    return bool(found) and min(found) > 0 and max(found) / min(found) > LOG_SPAN


def _layout(title: str, height: int, **extra) -> dict:
    layout = {"title": {"text": title, "x": 0, "xanchor": "left"}, "height": height,
              "margin": {"l": 64, "r": 24, "t": 56, "b": 48},
              "paper_bgcolor": "rgba(0,0,0,0)", "plot_bgcolor": "rgba(0,0,0,0)",
              "legend": {"orientation": "h", "x": 1, "xanchor": "right", "y": 1.02, "yanchor": "bottom"}}
    layout.update(extra)
    return layout


def _empty(title: str, message: str) -> dict:
    return {"data": [], "layout": _layout(title, 220, xaxis={"visible": False}, yaxis={"visible": False},
                                          annotations=[{"text": message, "showarrow": False, "x": 0.5, "y": 0.5,
                                                        "xref": "paper", "yref": "paper"}])}


def _score_traces(x: list, score: list, threshold: list) -> list[dict]:
    return [{"type": "scatter", "mode": "lines", "name": "score", "x": x, "y": score,
             "line": {"color": SERIES, "width": 2}, "hovertemplate": "%{y:.3g}<extra>score</extra>"},
            {"type": "scatter", "mode": "lines", "name": "threshold", "x": x, "y": threshold,
             "line": {"color": REFERENCE, "width": 1.5, "dash": "dash", "shape": "hv"},
             "hovertemplate": "%{y:.3g}<extra>threshold</extra>"}]


def _score_axis(values) -> dict:
    log = log_scale(values)
    return {"title": {"text": "score (log scale)" if log else "score"}, "type": "log" if log else "linear"}


def score_chart(series: dict) -> dict:
    """The anomaly score against its threshold, from GET /assets/{id}/series."""
    title = "Anomaly score against its threshold"
    s = (series or {}).get("scores") or {}
    x = list(s.get("window_end") or [])
    if not x:
        return _empty(title, "No scores in this span.")
    x, (score, threshold) = break_gaps(x, _clean(s.get("score")), _clean(s.get("threshold")))
    return {"data": _score_traces(x, score, threshold),
            "layout": _layout(title, 340, hovermode="x unified",
                              xaxis={"type": "date", "title": {"text": "window end (UTC)"}},
                              yaxis=_score_axis(score + threshold))}


def probability_chart(series: dict, horizons=HORIZONS) -> dict:
    """P(failure within each horizon) with its 90 % interval band, one panel per horizon."""
    title = "Failure probability with its 90 % interval (band)"
    p = (series or {}).get("prognoses") or {}
    ts = list(p.get("ts") or [])
    if not ts:
        return _empty(title, "No prognosis in this span.")
    n, gap = len(horizons), 0.1
    height = (1 - gap * (n - 1)) / n
    data, axes = [], {}
    for i, h in enumerate(horizons):
        suffix = "" if i == 0 else str(i + 1)
        col = p.get(h) or {}
        x, (lower, mid, upper) = break_gaps(ts, _clean(col.get("lower")), _clean(col.get("p")),
                                            _clean(col.get("upper")))
        ref = {"xaxis": "x" + suffix, "yaxis": "y" + suffix, "showlegend": False}
        data += [{"type": "scatter", "mode": "lines", "name": f"upper bound, {h} h", "x": x, "y": upper,
                  "line": {"color": SERIES, "width": 0}, "hovertemplate": "%{y:.2f}<extra>upper</extra>", **ref},
                 {"type": "scatter", "mode": "lines", "name": f"lower bound, {h} h", "x": x, "y": lower,
                  "line": {"color": SERIES, "width": 0}, "fill": "tonexty", "fillcolor": BAND,
                  "hovertemplate": "%{y:.2f}<extra>lower</extra>", **ref},
                 {"type": "scatter", "mode": "lines", "name": f"P({h} h)", "x": x, "y": mid,
                  "line": {"color": SERIES, "width": 2}, "hovertemplate": f"%{{y:.2f}}<extra>P({h} h)</extra>",
                  **ref}]
        top = 1 - i * (height + gap)
        axes["yaxis" + suffix] = {"domain": [max(0.0, top - height), top], "range": [0, 1], "tickformat": ".1f",
                                  "title": {"text": f"P({h} h)"}}
        axes["xaxis" + suffix] = dict({"anchor": "y" + suffix, "type": "date", "showticklabels": i == n - 1},
                                      **({"matches": "x"} if i else {}))
    return {"data": data, "layout": _layout(title, 150 * n + 90, hovermode="x unified", **axes)}


def attribution_chart(latest_alert: Optional[dict], top: int = 8) -> dict:
    """Each feature's share of the latest alert's score, largest first."""
    title = "What the latest alert's score is made of"
    rows = ((latest_alert or {}).get("attribution") or [])[:top]
    if not rows:
        return _empty(title, "No alert yet, so no attribution.")
    features = [str(r.get("feature")) for r in rows]
    shares = [v or 0.0 for v in _clean([r.get("share") for r in rows])]
    data = [{"type": "bar", "orientation": "h", "name": "share of the score", "x": shares, "y": features,
             "marker": {"color": SERIES}, "text": [f"{100 * s:.0f} %" for s in shares], "textposition": "outside",
             "cliponaxis": False, "hovertemplate": "%{x:.1%} of the score<extra>%{y}</extra>"}]
    return {"data": data,
            "layout": _layout(title, 96 + 30 * len(rows), hovermode="closest", showlegend=False, bargap=0.35,
                              xaxis={"title": {"text": "share of the score"}, "tickformat": ".0%",
                                     "range": [0, max(max(shares) * 1.2, 0.05)]},
                              yaxis={"autorange": "reversed", "automargin": True})}


def trajectory_chart(alert: Optional[dict]) -> dict:
    """The scores up to an incident's opening against the threshold (0 = the opening)."""
    title = "Scores up to the opening"
    points = (alert or {}).get("trajectory") or []
    if not points:
        return _empty(title, "No scores were recorded before the opening.")
    x = _clean([-(p.get("minutes_before_opening") or 0) for p in points])
    score = _clean([p.get("score") for p in points])
    threshold = _clean([p.get("threshold") for p in points])
    return {"data": _score_traces(x, score, threshold),
            "layout": _layout(title, 300, hovermode="x unified",
                              xaxis={"title": {"text": "minutes relative to the opening"}},
                              yaxis=_score_axis(score + threshold))}
