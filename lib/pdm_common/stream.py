"""Streaming windows: the batch pipeline on a rolling buffer (T3; ING-09 to ING-12).

The ingest service feeds accepted records in time order. ``StreamWindows``
returns each window once nothing that arrives later can change it, with the
same completeness and features the batch pipeline (tools/build_windows.py)
computes for the same records, because it runs the same functions:

1. the freeze mask and the 10 s grid with bridging (``pdm_common.quality``);
2. windows on stride boundaries, complete when every point is observed or
   bridged (``pdm_common.windows``);
3. the features declared in features.yaml (``pdm_common.features``).

Two of those steps read ahead, so a window waits for them:

* bridging reads up to ``gap_policy.max_bridge_seconds`` of later data, so a
  window is released about 30 s after its last point;
* a run of identical analog rows is a logger freeze once it has lasted
  ``freeze_rule.min_duration``; while such a run is open and shorter than
  that, windows reaching into it wait. A run that becomes a freeze is masked,
  and the windows over it are dropped as incomplete.

The buffer keeps the longest feature look-back (seven days for MetroPT-3) and
the anchors of the features that have none (``features.retention_anchors``):
the last motor stop and the last long gap. Beyond ``max_buffer`` it is cut
regardless, which only matters if the motor has not stopped for that long.

A value the caller excludes (an out-of-range reading, ING-07) is kept for the
freeze check but is missing on the grid, so an isolated bad reading is
bridged like a dropped sample and a persistent one makes windows incomplete.
tests/test_stream.py holds the windows to the batch pipeline.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from pdm_common.features import FeatureSpec, retention_anchors, window_features
from pdm_common.profile import analog_names, digital_names
from pdm_common.quality import freeze_runs, frozen_rows, to_grid
from pdm_common.timeutil import parse_duration


@dataclass
class StreamWindow:
    start: pd.Timestamp
    end: pd.Timestamp                    # start + window length: the window_end of the batch tables
    complete: bool
    n_observed: int
    n_bridged: int
    features: dict = field(default_factory=dict)   # name -> float or None; empty when incomplete

    def json_features(self) -> dict:
        return {k: (None if v is None or (isinstance(v, float) and not math.isfinite(v)) else float(v))
                for k, v in self.features.items()}


def _ceil(t: pd.Timestamp, unit: pd.Timedelta) -> pd.Timestamp:
    return t if t == t.floor(unit) else t.floor(unit) + unit


class StreamWindows:
    """Records in, finished windows out. One instance per asset and stream."""

    def __init__(self, profile: dict, spec: FeatureSpec, *, margin: str = "1h", max_buffer: str = "30d"):
        self.analog, self.digital = analog_names(profile), digital_names(profile)
        self.columns = self.analog + self.digital
        rule = profile["freeze_rule"]
        self.freeze_minutes = parse_duration(rule["min_duration"]).total_seconds() / 60
        self.freeze_step_s = float(rule["max_step_s"])
        self.step_text = profile["sampling"]["resample_rule"]
        self.step = pd.Timedelta(parse_duration(self.step_text))
        self.max_bridge_s = float(profile["gap_policy"]["max_bridge_seconds"])
        self.spec = spec
        self.win_bins = spec.win_bins
        self.stride = spec.stride
        self.length = spec.window_length
        self.margin = pd.Timedelta(parse_duration(margin))
        self.lookback = spec.max_lookback() + self.length + self.margin
        self.max_buffer = pd.Timedelta(parse_duration(max_buffer))
        self._ts: list[int] = []                         # ns since the epoch, UTC
        self._values: list[list[float]] = []
        self._excluded: list[tuple[int, ...]] = []
        self._next_start: pd.Timestamp | None = None
        self._run_first: pd.Timestamp | None = None     # first row of the open run of identical rows
        self.counts: Counter = Counter({"records": 0, "windows": 0, "complete": 0, "dropped": 0})

    # -- input ---------------------------------------------------------------------------
    def append(self, ts, values: dict, excluded=()) -> None:
        """Add one record (time order; the caller quarantines anything else) without
        computing. ``excluded``: signal names whose value is not used on the grid."""
        ts = pd.Timestamp(ts)
        ts = ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")
        row = [float(values[c]) for c in self.columns]
        if self._ts:
            if ts.value <= self._ts[-1]:
                raise ValueError(f"record at {ts} is not after {self._last}")
            prev = self._values[-1]
            same = ((ts.value - self._ts[-1]) / 1e9 <= self.freeze_step_s and
                    all(row[i] == prev[i] for i in range(len(self.analog))))
            if same:
                self._run_first = self._run_first or ts
            else:
                self._run_first = None
        else:
            self._next_start = _ceil(ts.floor(self.step), self.stride)
        self._ts.append(ts.value)
        self._values.append(row)
        self._excluded.append(tuple(self.columns.index(c) for c in excluded if c in self.columns))
        self.counts["records"] += 1

    def add(self, ts, values: dict, excluded=()) -> list[StreamWindow]:
        """Add one record; return the windows it finished (usually none, one a minute)."""
        self.append(ts, values, excluded)
        return self.compute() if self._ready_until() is not None else []

    # -- what is final -------------------------------------------------------------------
    def settled_point(self, final: bool = False) -> pd.Timestamp | None:
        """The last grid point no later record can change."""
        if not self._ts:
            return None
        last_point = self._last.floor(self.step)
        if final:
            return last_point
        settled = last_point - pd.Timedelta(seconds=self.max_bridge_s)
        if self._run_first is not None:
            run_minutes = (self._last - self._run_first).total_seconds() / 60
            if run_minutes < self.freeze_minutes:          # might still become a freeze
                settled = min(settled, self._run_first.floor(self.step) - self.step)
        return settled

    def _ready_until(self, final: bool = False) -> pd.Timestamp | None:
        """The last window start that can be emitted now, or None."""
        settled = self.settled_point(final)
        if settled is None or self._next_start is None:
            return None
        last_start = settled - (self.win_bins - 1) * self.step
        return last_start if last_start >= self._next_start else None

    # -- the batch pipeline on the buffer ----------------------------------------------------
    @property
    def _last(self) -> pd.Timestamp:
        return pd.Timestamp(self._ts[-1], tz="UTC")

    def _frame(self) -> pd.DataFrame:
        df = pd.DataFrame(np.array(self._values, dtype=float), columns=self.columns)
        df.insert(0, "timestamp", pd.DatetimeIndex(np.array(self._ts, dtype="int64"), tz="UTC"))
        return df

    def grid(self) -> pd.DataFrame:
        """The masked, bridged grid of the buffer, as the batch pipeline builds it."""
        df = self._frame()
        runs = freeze_runs(df, self.analog, min_minutes=self.freeze_minutes, max_step_s=self.freeze_step_s)
        frozen = frozen_rows(df, runs)
        if any(self._excluded):
            values = df[self.columns].to_numpy(copy=True)
            for i, cols in enumerate(self._excluded):
                if cols:
                    values[i, list(cols)] = np.nan
            df[self.columns] = values
        return to_grid(df, frozen, self.analog, self.digital, step=self.step_text,
                       max_bridge_s=self.max_bridge_s, source_tz="UTC")

    def compute(self, final: bool = False) -> list[StreamWindow]:
        """Emit every window that is final now (``final``: the stream has ended)."""
        last_start = self._ready_until(final)
        if last_start is None:
            return []
        starts = pd.date_range(self._next_start, last_start, freq=self.stride)
        grid = self.grid()
        origin = grid.index[0]
        ends = ((starts - origin) // self.step).to_numpy().astype(int) + self.win_bins - 1
        inside = ends >= self.win_bins - 1          # a window starting before the buffer has no data there
        safe = np.where(inside, ends, self.win_bins - 1)
        valid = (grid["observed"] | grid["bridged"]).to_numpy()
        bridged = grid["bridged"].to_numpy()
        observed = grid["observed"].to_numpy()
        csum = lambda a: np.r_[0, np.cumsum(a.astype(int))]   # noqa: E731
        cv, cb, co = csum(valid), csum(bridged), csum(observed)
        n_valid = cv[safe + 1] - cv[safe + 1 - self.win_bins]
        complete = inside & (n_valid == self.win_bins)
        feats = window_features(grid, ends[complete], self.spec) if complete.any() else None
        out, k = [], 0
        for i, start in enumerate(starts):
            e = safe[i]
            w = StreamWindow(start=start, end=start + self.length, complete=bool(complete[i]),
                             n_observed=int(co[e + 1] - co[e + 1 - self.win_bins]) if inside[i] else 0,
                             n_bridged=int(cb[e + 1] - cb[e + 1 - self.win_bins]) if inside[i] else 0)
            if complete[i]:
                w.features = feats.iloc[k].to_dict()
                k += 1
            out.append(w)
        self.counts["windows"] += len(out)
        self.counts["complete"] += int(complete.sum())
        self.counts["dropped"] += int((~complete).sum())
        self._next_start = starts[-1] + self.stride
        self._trim(grid)
        return out

    def flush(self) -> list[StreamWindow]:
        """End of stream: what is left is final (an open run shorter than a freeze is not one)."""
        return self.compute(final=True)

    def _trim(self, grid: pd.DataFrame) -> None:
        keep_from = self._next_start - self.lookback
        upto = int((self._next_start - grid.index[0]) // self.step) + self.win_bins - 1
        for anchor in retention_anchors(grid, self.spec, upto):
            keep_from = min(keep_from, anchor - self.margin)
        keep_from = max(keep_from, self._last - self.max_buffer)
        cut = int(np.searchsorted(np.array(self._ts, dtype="int64"), keep_from.value, side="left"))
        if cut > 0:
            del self._ts[:cut], self._values[:cut], self._excluded[:cut]

    @property
    def buffered(self) -> int:
        return len(self._ts)
