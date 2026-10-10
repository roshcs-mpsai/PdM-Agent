"""Windows over the 10 s grid (T6; ING-11), plus the R0 minute buckets.

Two kinds of window live here:

* ``window_ends``, ``make_windows`` and ``add_folds`` -- the R1 windows: fixed
  length and stride from the profile (10 min, 1 min), each starting on a stride
  boundary, complete only when every grid point is observed or bridged, with
  the zone, regime, leave-one-event-out blocks, look-back flags and the
  fit / calibrate / check role the evaluation harness reads. Ported from
  notebooks/metropt3_eda_v4_clean.ipynb Sections 7.3 and 7.4.
* ``MinuteWindower`` -- the R0 walking skeleton's one-minute means, kept for
  the z-score baseline (services/detector/zscore.py).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd

from pdm_common.timeutil import parse_duration
from pdm_common.zones import ZoneScheme, expert_spans, failure_spans, paddings, regime_of, zone_scheme

EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def floor_time(moment: dt.datetime, length: dt.timedelta) -> dt.datetime:
    """Start of the epoch-aligned bucket of ``length`` holding ``moment``."""
    return EPOCH + ((moment - EPOCH) // length) * length


@dataclass
class Window:
    start: dt.datetime
    end: dt.datetime
    n: int                              # samples in the bucket
    means: dict[str, float]
    first_ts: dt.datetime
    last_ts: dt.datetime


@dataclass
class _Bucket:
    start: dt.datetime
    sums: dict[str, float]
    n: int = 0
    first_ts: dt.datetime | None = None
    last_ts: dt.datetime | None = None


class MinuteWindower:
    """Per-signal means over fixed buckets (default one minute).

    Samples must arrive in time order; the caller (ingest) quarantines any
    that do not. A bucket closes when the first sample of a later bucket
    arrives, or on ``flush()``.
    """

    def __init__(self, signals: list[str], length: dt.timedelta = dt.timedelta(minutes=1)):
        if not signals:
            raise ValueError("MinuteWindower needs at least one signal")
        self.signals = list(signals)
        self.length = length
        self._open: _Bucket | None = None

    @property
    def open_start(self) -> dt.datetime | None:
        return self._open.start if self._open else None

    def add(self, ts: dt.datetime, values: dict[str, float]) -> list[Window]:
        """Add one sample; return the window it closed, if any."""
        start = floor_time(ts, self.length)
        closed = []
        if self._open is not None and start != self._open.start:
            closed.append(self._close())
        if self._open is None:
            self._open = _Bucket(start=start, sums={s: 0.0 for s in self.signals}, first_ts=ts)
        bucket = self._open
        for s in self.signals:
            bucket.sums[s] += values[s]
        bucket.n += 1
        bucket.last_ts = ts
        return closed

    def flush(self) -> list[Window]:
        """Close the open bucket (end of stream or shutdown)."""
        return [self._close()] if self._open is not None else []

    def _close(self) -> Window:
        b, self._open = self._open, None
        return Window(
            start=b.start,
            end=b.start + self.length,
            n=b.n,
            means={s: b.sums[s] / b.n for s in self.signals},
            first_ts=b.first_ts,
            last_ts=b.last_ts,
        )


# --- R1 windows on the grid --------------------------------------------------------

def window_ends(index: pd.DatetimeIndex, win_bins: int, stride: pd.Timedelta) -> np.ndarray:
    """Grid positions of the last point of every window that starts on a
    stride boundary (epoch-aligned)."""
    last = np.arange(win_bins - 1, len(index))
    start_ns = index.asi8[last - (win_bins - 1)]
    unit = _ns_per(index)
    return last[(start_ns // unit) % int(stride.total_seconds()) == 0]


def _ns_per(index: pd.DatetimeIndex) -> int:
    """Units per second of the index's int64 representation."""
    unit = getattr(index, "unit", "ns")
    return {"s": 1, "ms": 10**3, "us": 10**6, "ns": 10**9}[unit]


def make_windows(grid: pd.DataFrame, ends: np.ndarray, events: dict, *, win_bins: int,
                 window_length: pd.Timedelta, lookback: pd.Timedelta, step: pd.Timedelta,
                 scheme: ZoneScheme | None = None) -> pd.DataFrame:
    """One row per window.

    ``grid`` carries ``observed``, ``bridged`` and ``zone_code``
    (``pdm_common.zones.label_grid``). A window is complete when all its points
    are observed or bridged, and takes the highest-precedence zone of its own
    points. A negative window whose look-back (``lookback`` before its start)
    reaches an uncertain period, an expert-reported episode or a report becomes
    grey, ``after_uncertain``: its trailing features still carry the episode.
    Adds the regime, ``aligned`` (starts on a window-length boundary: the
    non-overlapping subset) and ``group_id`` (the start hour).
    """
    scheme = scheme or zone_scheme(events)
    ends = np.asarray(ends, dtype=int)
    valid = (grid["observed"] | grid["bridged"]).astype("int16")
    n_valid = valid.rolling(win_bins).sum().to_numpy()[ends]
    code = grid["zone_code"]
    top = code.rolling(win_bins).max().to_numpy()[ends].astype(int)
    lo, hi = scheme.episode_codes()
    episode = code.between(lo, hi).astype("int8")
    reach = win_bins + int(lookback / step)
    touched = episode.rolling(reach, min_periods=1).max().to_numpy()[ends] > 0
    zone = np.array(scheme.zone_of, dtype=object)[top]
    detail = np.array(scheme.detail, dtype=object)[top]
    after = (zone == "negative") & touched
    zone[after], detail[after] = "grey", "after_uncertain"
    start = grid.index[ends - (win_bins - 1)]
    out = pd.DataFrame({"window_start": start, "window_end": start + window_length,
                        "complete": n_valid == win_bins, "zone": zone, "zone_detail": detail})
    out["regime"] = regime_of(out["window_start"], events)
    unit = _ns_per(pd.DatetimeIndex(start))
    seconds = pd.DatetimeIndex(start).asi8 // unit
    out["aligned"] = seconds % int(window_length.total_seconds()) == 0
    out["group_id"] = out["window_start"].dt.strftime("%Y-%m-%dT%H")
    return out


def add_folds(win: pd.DataFrame, events: dict, lookback_days: pd.Timedelta = pd.Timedelta(days=7)) -> dict:
    """Leave-one-event-out columns and the negative-window roles (EDA 7.4).

    Test block: the event's healthy buffer, from ``before_failure`` before the
    report to ``after_repair`` after the repair (after the report's end when no
    repair is listed). A block stops short of another event's positive zone and
    never holds another event's positive windows, so F3's block starts where
    F2's guard band ends (the asymmetric blocks of events.yaml). Adds
    ``event``, ``expert_failure``, ``block_<id>``, ``lookback_<id>``,
    ``neg_split`` and ``neg_role``; returns the block spans.
    """
    pad = paddings(events)
    failures = failure_spans(events)
    event = np.full(len(win), "none", dtype=object)
    positive = (win["zone"] == "positive").to_numpy()
    for fid, s, e, _ in failures:
        touches = (win["window_start"] <= e + pad["guard_after"]) & (win["window_end"] > s - pad["guard_before"])
        event[positive & touches.to_numpy()] = fid
    win["event"] = event
    expert = np.full(len(win), "", dtype=object)
    for n, s, e in expert_spans(events):
        hit = ((win["window_start"] < e + pd.Timedelta(minutes=1)) & (win["window_end"] > s)).to_numpy()
        expert[hit] = np.where(expert[hit] == "", str(n), expert[hit] + "," + str(n))
    win["expert_failure"] = np.where(expert == "", "none", expert)
    positive_zone = [(s - pad["guard_before"], e + pad["guard_after"]) for _, s, e, _ in failures]
    spans = {}
    for i, (fid, s, e, r) in enumerate(failures):
        a, b = s - pad["buffer_before"], (r if r is not None else e) + pad["buffer_after"]
        for j, (ps, pe) in enumerate(positive_zone):
            if j < i and pe > a:
                a = pe
            if j > i and ps < b:
                b = ps
        spans[fid] = (a, b)
        other = (win["event"] != "none") & (win["event"] != fid)
        win[f"block_{fid}"] = (win["window_start"] < b) & (win["window_end"] > a) & ~other
        reach = (win["window_start"] - lookback_days < b) & (win["window_end"] > a)
        win[f"lookback_{fid}"] = reach & ~win[f"block_{fid}"] & (win["zone"] == "negative")
    week = win["window_start"].dt.isocalendar().week.astype(int).to_numpy()
    negative = (win["zone"] == "negative").to_numpy()
    win["neg_split"] = np.where(~negative, "none", np.where(week % 2 == 0, "fit", "calibrate"))
    win["neg_role"] = np.select([~negative, week % 2 == 0, week % 4 == 1], ["none", "fit", "calibrate"], "check")
    return spans


def windowing_params(profile: dict, step: str | None = None) -> dict:
    """Window length, stride and grid step from the profile, as Timedeltas."""
    step = step or profile["sampling"]["resample_rule"]
    td = lambda text: pd.Timedelta(parse_duration(text))   # noqa: E731
    length, stride, grid_step = td(profile["windowing"]["length"]), td(profile["windowing"]["stride"]), td(step)
    return {"length": length, "stride": stride, "step": grid_step, "win_bins": int(length / grid_step)}
