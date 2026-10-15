"""Timeline zones from events.yaml (T7; CFG-05).

Every grid point gets exactly one zone label, so "healthy" means one thing in
the batch harness, the streaming services and every notebook. The labels and
their precedence are the EDA v4 notebook's (Section 7.2), with every span read
from events.yaml rather than typed into code:

1. masked           -- a logger freeze (``data_quality_masks``);
2. positive         -- a repair-forcing report window with its guard band;
3. expert_reported  -- an expert-logged failure that forced no repair, with
                       ``expert_band``; grey for training, a failure for scoring;
4. grey             -- uncertain periods, grey periods, the gap edge after each
                       recording gap, and the rest of each healthy buffer;
5. negative         -- everything else: the only detector training data.

Spans are painted as written in events.yaml: report, uncertain, grey,
expert and mask spans include their end instant; buffers and gap edges are
half-open. An expert entry written to the minute covers that whole minute.

Ported from notebooks/metropt3_eda_v4_clean.ipynb Section 7.2 (zone_codes,
gap_edges); tests/test_zones.py and the data test in tests/test_features.py
check the port against data/processed_v4.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from pdm_common.timeutil import parse_duration, to_utc

ONE_MINUTE = pd.Timedelta(minutes=1)


@dataclass(frozen=True)
class ZoneScheme:
    """The detail labels in precedence order (a higher code wins) and the zone
    each detail label belongs to."""
    detail: tuple[str, ...]
    zone_of: tuple[str, ...]

    @property
    def code(self) -> dict[str, int]:
        return {name: i for i, name in enumerate(self.detail)}

    def episode_codes(self) -> tuple[int, int]:
        """Codes from the first uncertain tag to 'failure': what a negative
        window's look-back must not reach (EDA 7.3, after_uncertain)."""
        codes = self.code
        first = next((i for i, d in enumerate(self.detail) if d.startswith("uncertain:")),
                     codes["expert_reported"])
        return first, codes["failure"]


def uncertain_tags(events: dict) -> list[str]:
    """Uncertain-period tags in order of first appearance in events.yaml."""
    tags: list[str] = []
    for period in events.get("uncertain_periods") or []:
        if period["tag"] not in tags:
            tags.append(period["tag"])
    return tags


def zone_scheme(events: dict) -> ZoneScheme:
    """negative < buffer < gap_edge < grey_period < uncertain:* < expert_reported
    < failure < masked, the EDA v4 order."""
    detail = (["negative", "buffer", "gap_edge", "grey_period"]
              + [f"uncertain:{tag}" for tag in uncertain_tags(events)]
              + ["expert_reported", "failure", "masked"])
    zone_of = ["negative"] + ["grey"] * (len(detail) - 3) + ["positive", "masked"]
    return ZoneScheme(tuple(detail), tuple(zone_of))


def _t(value) -> pd.Timestamp:
    return pd.Timestamp(to_utc(value))


def failure_spans(events: dict) -> list[tuple[str, pd.Timestamp, pd.Timestamp, pd.Timestamp | None]]:
    """(id, start, end, repair) for each repair-forcing failure, as written."""
    out = []
    for f in events["failures"]:
        repair = f.get("maintenance")
        out.append((f["id"], _t(f["start"]), _t(f["end"]), None if repair is None else _t(repair)))
    return out


def expert_spans(events: dict) -> list[tuple[int, pd.Timestamp, pd.Timestamp]]:
    """(number, start, end) of every expert-reported failure, as written."""
    return [(int(e["n"]), _t(e["start"]), _t(e["end"])) for e in events.get("expert_reported") or []]


def paddings(events: dict) -> dict[str, pd.Timedelta]:
    td = lambda text: pd.Timedelta(parse_duration(text))   # noqa: E731
    out = {
        "guard_before": td(events["guard_band"]["before"]),
        "guard_after": td(events["guard_band"]["after"]),
        "buffer_before": td(events["healthy_buffer"]["before_failure"]),
        "buffer_after": td(events["healthy_buffer"]["after_repair"]),
        "edge_after_gaps_of": td(events["gap_edges"]["after_gaps_of"]),
        "edge_length": td(events["gap_edges"]["length"]),
    }
    band = events.get("expert_band") or {"before": "0s", "after": "0s"}
    out["expert_before"], out["expert_after"] = td(band["before"]), td(band["after"])
    return out


def gap_edges(gaps: pd.DataFrame, events: dict, step: str = "10s") -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Grey spans after each recording gap of ``gap_edges.after_gaps_of`` or more.

    ``gaps`` holds raw-timestamp gaps (``gap_start``, ``gap_end``, ``seconds``
    as ``pdm_common.quality.raw_gaps`` returns them), so a freeze, which keeps
    its timestamps, is not a gap. Each edge runs from the grid point holding
    the first sample after the gap to ``length`` after that sample.
    """
    pad = paddings(events)
    stops = gaps[gaps["seconds"] >= pad["edge_after_gaps_of"].total_seconds()]
    ends = pd.DatetimeIndex(stops["gap_end"]).sort_values()
    if ends.tz is None:
        ends = ends.tz_localize("UTC")
    return [(t.floor(step), t + pad["edge_length"]) for t in ends]


def zone_codes(times: pd.DatetimeIndex, events: dict, edges, scheme: ZoneScheme | None = None) -> np.ndarray:
    """One code per timestamp (an index into ``scheme.detail``); where spans
    overlap, the higher code wins. ``times`` must be sorted and tz-aware."""
    scheme = scheme or zone_scheme(events)
    code_of = scheme.code
    pad = paddings(events)
    code = np.zeros(len(times), dtype=np.int8)

    def paint(start, end, c, closed=True):
        i0 = times.searchsorted(start)
        i1 = times.searchsorted(end, side="right" if closed else "left")
        code[i0:i1] = np.maximum(code[i0:i1], c)

    for _, s, e, r in failure_spans(events):
        paint(s - pad["buffer_before"], (r if r is not None else e) + pad["buffer_after"],
              code_of["buffer"], closed=False)
        paint(s - pad["guard_before"], e + pad["guard_after"], code_of["failure"])
    for s, e in edges:
        paint(s, e, code_of["gap_edge"], closed=False)
    for g in events.get("grey_periods") or []:
        paint(_t(g["start"]), _t(g["end"]), code_of["grey_period"])
    for u in events.get("uncertain_periods") or []:
        paint(_t(u["start"]), _t(u["end"]), code_of["uncertain:" + u["tag"]])
    for _, s, e in expert_spans(events):
        paint(s - pad["expert_before"], e + ONE_MINUTE + pad["expert_after"], code_of["expert_reported"])
    for m in events.get("data_quality_masks") or []:
        paint(_t(m["start"]), _t(m["end"]), code_of["masked"])
    return code


def label_grid(grid: pd.DataFrame, events: dict, edges, scheme: ZoneScheme | None = None) -> pd.DataFrame:
    """The grid with ``zone_code``, ``zone`` and ``zone_detail`` columns added."""
    scheme = scheme or zone_scheme(events)
    codes = zone_codes(grid.index, events, edges, scheme)
    out = grid.copy()
    out["zone_code"] = codes
    out["zone"] = pd.Categorical(np.array(scheme.zone_of)[codes],
                                 categories=["negative", "grey", "positive", "masked"])
    out["zone_detail"] = pd.Categorical(np.array(scheme.detail)[codes], categories=list(scheme.detail))
    return out


def regime_of(times, events: dict) -> np.ndarray:
    """The operating regime of each time (half-open regimes from events.yaml);
    times outside every regime get None."""
    regimes = events.get("regimes") or []
    names = np.array([r["name"] for r in regimes] + [None], dtype=object)
    starts = pd.DatetimeIndex([_t(r["start"]) for r in regimes])
    ends = pd.DatetimeIndex([_t(r["end"]) for r in regimes])
    t = pd.DatetimeIndex(times)
    if t.tz is None:
        t = t.tz_localize("UTC")
    k = starts.searchsorted(t, side="right") - 1
    inside = (k >= 0) & (t < ends[np.maximum(k, 0)])
    return np.where(inside, names[np.maximum(k, 0)], None)
