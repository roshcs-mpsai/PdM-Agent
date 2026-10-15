"""As-of reads from the store (AGT-04): nothing recorded at or after a moment.

The checks of an incident, the agent's tools and the evaluation all read the
data an incident was opened on, never later data. These functions rebuild
that data from the store with the same pipeline ingest runs:

* ``telemetry_frame``: accepted records in [start, end), out-of-range values
  set missing as ingest leaves them off the grid;
* ``grid_asof``: those records through the freeze mask and the 10 s grid
  (pdm_common.quality), reaching back far enough that the last gap before the
  moment is whole;
* ``window_asof``: the features of the window that ended at the moment;
* ``evidence_asof``: the Evidence the checks of an incident read.
"""
from __future__ import annotations


import numpy as np
import pandas as pd

from pdm_common.checks import Evidence
from pdm_common.profile import analog_names, digital_names
from pdm_common.quality import mask_grid_and_gaps
from pdm_common.timeutil import iso_utc, parse_duration, to_utc


def _iso(t) -> str:
    return iso_utc(to_utc(pd.Timestamp(t).to_pydatetime()))


def telemetry_frame(store, profile: dict, asset: str, start, end) -> pd.DataFrame:
    """Records with start <= ts < end, oldest first, as the batch pipeline reads a CSV."""
    rows = store.between("telemetry", "ts", asset, _iso(start), _iso(end), end_inclusive=False)
    cols = analog_names(profile) + digital_names(profile)
    if not rows:
        return pd.DataFrame(columns=["timestamp", *cols])
    values = np.array([[r["signals"].get(c, np.nan) for c in cols] for r in rows], dtype=float)
    for i, r in enumerate(rows):
        for name in (r.get("out_of_range") or {}):
            if name in cols:
                values[i, cols.index(name)] = np.nan
    frame = pd.DataFrame(values, columns=cols)
    frame.insert(0, "timestamp", pd.DatetimeIndex([pd.Timestamp(r["ts"]) for r in rows]).tz_convert("UTC")
                 .tz_localize(None))
    return frame


def grid_asof(store, profile: dict, asset: str, moment, lookback: str = "7d") -> pd.DataFrame:
    """The 10 s grid of the ``lookback`` before ``moment``, plus the last record before
    that, so a gap reaching into the look-back is whole."""
    moment = pd.Timestamp(moment)
    start = moment - pd.Timedelta(parse_duration(lookback))
    before = store.latest("telemetry", "ts", asset, before=_iso(start - pd.Timedelta(microseconds=1)))
    first = pd.Timestamp(before["ts"]) if before else start
    frame = telemetry_frame(store, profile, asset, min(first, start), moment)
    if frame.empty:
        return pd.DataFrame()
    _, _, grid, _ = mask_grid_and_gaps(frame, dict(profile, source_timezone="UTC"))
    return grid


def window_asof(store, asset: str, moment) -> dict | None:
    """The features of the window that ended at ``moment`` (None if there is none)."""
    row = store.one("SELECT * FROM windows WHERE asset_id = ? AND window_end = ?",
                    [asset, store_time(store, "windows", "window_end", moment)], table="windows")
    return row["features"] if row else None


def store_time(store, table: str, column: str, moment):
    from pdm_common.store import _to_db
    return _to_db(table, column, _iso(moment), store.dialect)


def evidence_asof(store, profile: dict, events: dict, asset: str, opens, lookback: str = "7d") -> Evidence:
    grid = grid_asof(store, profile, asset, opens, lookback)
    window = window_asof(store, asset, opens) or {}
    if grid.empty:
        grid = pd.DataFrame(columns=analog_names(profile) + digital_names(profile) + ["observed", "bridged"],
                            index=pd.DatetimeIndex([], tz="UTC"))
    return Evidence(opens, grid, window, profile=profile, events=events)


