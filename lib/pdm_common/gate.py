"""The persistence gate as pure functions (T12a; INC-01 to INC-04).

The gate turns per-window alerts into incidents, the unit the planner sees and
the unit the false-alarm budget counts (INC-04). A window *passes* when at
least ``min_fraction`` of the windows ending in the last ``span`` alert and at
least ``min_present`` of them exist. Passes group into incidents: a pass more
than ``hold_off`` after the previous one opens a new incident, and an incident
stays open until ``hold_off`` after its last pass.

These are the notebook's functions (detector_selection Section 2,
``gate_passes`` and ``incidents``) with every parameter explicit instead of
read from notebook globals, so the evaluation harness, SPOT's calibration and
the gate service count alarms the same way. Times are int64 nanoseconds of the
window ends, sorted.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from pdm_common.timeutil import parse_duration

HOUR_NS = 3_600 * 10**9


@dataclass(frozen=True)
class GateParams:
    span_ns: int
    min_fraction: float
    hold_off_ns: int
    min_present: int

    def as_dict(self) -> dict:
        return {"span": str(pd.Timedelta(self.span_ns)), "min_fraction": self.min_fraction,
                "hold_off": str(pd.Timedelta(self.hold_off_ns)), "min_present": self.min_present}


def gate_params(profile: dict) -> GateParams:
    """The gate from the profile's ``gate`` and ``windowing.stride``; the gate
    also needs half of the windows in its span to exist."""
    span = parse_duration(profile["gate"]["span"])
    stride = parse_duration(profile["windowing"]["stride"])
    hold_off = parse_duration(profile["gate"]["hold_off"])
    return GateParams(span_ns=int(span.total_seconds() * 10**9),
                      min_fraction=float(profile["gate"]["min_fraction"]),
                      hold_off_ns=int(hold_off.total_seconds() * 10**9),
                      min_present=int(0.5 * (span / stride)))


def ns(times) -> np.ndarray:
    """Times as int64 nanoseconds since the epoch (UTC), whatever resolution or
    timezone pandas used."""
    arr = np.asarray(times)
    if arr.dtype.kind == "M":
        return arr.astype("datetime64[ns]").astype("int64")
    t = pd.DatetimeIndex(times)
    if t.tz is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t.to_numpy().astype("datetime64[ns]").astype("int64")


def gate_passes(end_ns, alerts, span_ns: int, min_fraction: float, min_present: int) -> np.ndarray:
    """Per window (sorted by end): do enough of the windows ending in the last span alert?"""
    end_ns = np.asarray(end_ns, dtype=np.int64)
    lo = np.searchsorted(end_ns, end_ns - span_ns, side="right")
    idx = np.arange(len(end_ns))
    present = idx + 1 - lo
    csum = np.concatenate([[0], np.cumsum(np.asarray(alerts), dtype=np.int64)])
    alerting = csum[idx + 1] - csum[lo]
    return (present >= min_present) & (alerting >= min_fraction * present)


def incidents(end_ns, passes, hold_off_ns: int):
    """Gate passes grouped into incidents: (opens, last passes, closes) in ns."""
    t = np.asarray(end_ns, dtype=np.int64)[np.asarray(passes, dtype=bool)]
    if len(t) == 0:
        empty = np.empty(0, np.int64)
        return empty, empty, empty
    new = np.concatenate([[True], np.diff(t) > hold_off_ns])
    first = np.flatnonzero(new)
    last = np.concatenate([first[1:] - 1, [len(t) - 1]])
    return t[first], t[last], t[last] + hold_off_ns


def gate(end_ns, alerts, params: GateParams):
    """Alerts to incidents in one call: (opens, last passes, closes) in ns."""
    passes = gate_passes(end_ns, alerts, params.span_ns, params.min_fraction, params.min_present)
    return incidents(end_ns, passes, params.hold_off_ns)


def lead_at_report(opens, closes, start_ns: int, end_ns: int) -> float:
    """Hours from the warning to the report start; NaN when the report passes
    with no incident. The warning is the incident already open when the report
    starts, or else the first one to open during the report (a negative lead)."""
    opens, closes = np.asarray(opens), np.asarray(closes)
    open_at_start = (opens <= start_ns) & (closes > start_ns)
    if open_at_start.any():
        return (start_ns - opens[open_at_start].min()) / HOUR_NS
    during = opens[(opens > start_ns) & (opens <= end_ns)]
    return (start_ns - during.min()) / HOUR_NS if len(during) else float("nan")
