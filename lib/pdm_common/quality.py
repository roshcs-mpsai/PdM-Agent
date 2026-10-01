"""Freeze mask, 10 s grid and gap policy (R1a.4; ING-09, ING-10).

The order matters and is fixed here, so the batch harness and the streaming
ingest service do the same thing:

1. ``freeze_runs`` finds logger freezes in the raw rows: every analog
   channel unchanged row to row, every step short, for at least the minimum
   duration (profile ``freeze_rule``). The 2020 file has exactly ten.
2. ``to_grid`` drops the frozen rows, so a freeze becomes missing data
   rather than a flat line a resampler could carry forward, then puts the
   rest on an epoch-aligned grid in UTC (profile ``sampling.resample_rule``).
   Analog channels take the mean of each bin, digital channels the last
   value. An interior run of empty points is bridged only when
   (run + 1) x step <= ``gap_policy.max_bridge_seconds`` -- one or two points
   at 10 s and 30 s, which covers every raw gap of 30 s or less -- analog by
   time interpolation, digital by forward fill, and flagged ``bridged``.
   Runs at either edge of the data are never bridged.
3. ``label_gaps`` names each raw timestamp gap: a long outage, an overnight
   stop or a dropout (profile ``gap_policy.gap_labels``). The survival model
   censors at long outages only.

Ported from notebooks/metropt3_eda.ipynb Sections 2.1, 2.2 and 7.1, where
the counts were verified on the full file.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from pdm_common.profile import analog_names, digital_names
from pdm_common.timeutil import parse_duration


# --- 1. logger freezes ---------------------------------------------------------

def freeze_runs(df: pd.DataFrame, analog: list[str], min_minutes: float = 30,
                max_step_s: float = 15) -> pd.DataFrame:
    """Runs where every analog channel repeats exactly and every step is short.

    ``df`` has a ``timestamp`` column in time order. Returns one row per run
    with ``first`` and ``last`` timestamps, ``rows`` and ``minutes``.
    """
    ts = pd.to_datetime(df["timestamp"])
    step = ts.diff().dt.total_seconds()
    same = (df[analog].diff().abs().sum(axis=1) == 0) & (step <= max_step_s)
    run = (same != same.shift()).cumsum()
    runs = ts[same].groupby(run[same]).agg(["first", "last", "size"])
    runs = runs.rename(columns={"size": "rows"})
    runs["minutes"] = (runs["last"] - runs["first"]).dt.total_seconds() / 60
    return runs[runs["minutes"] >= min_minutes].reset_index(drop=True)


def frozen_rows(df: pd.DataFrame, runs: pd.DataFrame) -> np.ndarray:
    """Boolean per row: inside a freeze run, both ends included."""
    ts = pd.to_datetime(df["timestamp"])
    frozen = np.zeros(len(df), dtype=bool)
    for first, last in zip(runs["first"], runs["last"]):
        frozen |= ((ts >= first) & (ts <= last)).to_numpy()
    return frozen


# --- 2. the grid -----------------------------------------------------------

def utc_index(timestamps: pd.Series, source_tz: str = "UTC") -> pd.DatetimeIndex:
    """Timestamps as an aware UTC index; naive values are read in ``source_tz``."""
    ts = pd.DatetimeIndex(pd.to_datetime(timestamps))
    if ts.tz is None:
        ts = ts.tz_localize(source_tz)
    return ts.tz_convert("UTC")


def to_grid(df: pd.DataFrame, frozen: np.ndarray, analog: list[str], digital: list[str],
            step: str = "10s", max_bridge_s: float = 30, source_tz: str = "UTC") -> pd.DataFrame:
    """Masked rows on a regular UTC grid, with ``observed`` and ``bridged`` flags."""
    ts = utc_index(df["timestamp"], source_tz)
    live = df.loc[~frozen, analog + digital].set_axis(ts[~frozen])
    idx = pd.date_range(ts.min().floor(step), ts.max().floor(step), freq=step, name="timestamp")
    a = live[analog].resample(step, origin="epoch").mean().reindex(idx)
    d = live[digital].resample(step, origin="epoch").last().reindex(idx)

    observed = a.notna().all(axis=1).to_numpy()
    missing = ~observed
    run_id = np.cumsum(np.r_[True, missing[1:] != missing[:-1]])
    run_len = np.bincount(run_id)[run_id]
    at_edge = (run_id == run_id[0]) | (run_id == run_id[-1])
    step_s = pd.Timedelta(step).total_seconds()
    bridged = missing & ~at_edge & ((run_len + 1) * step_s <= max_bridge_s)

    values_a = np.where(bridged[:, None],
                        a.interpolate(method="time", limit_area="inside").to_numpy(), a.to_numpy())
    values_d = np.where(bridged[:, None], d.ffill().to_numpy(), d.to_numpy())
    grid = pd.DataFrame({c: values_a[:, j] for j, c in enumerate(analog)}, index=idx)
    for j, c in enumerate(digital):
        grid[c] = values_d[:, j]
    grid["observed"] = observed
    grid["bridged"] = bridged
    return grid


# --- 3. gap labels -----------------------------------------------------------

def _clock(text: str) -> dt.time:
    return dt.time.fromisoformat(text)


def raw_gaps(df: pd.DataFrame, min_gap_s: float = 50, source_tz: str = "UTC") -> pd.DataFrame:
    """Raw timestamp steps longer than ``min_gap_s``. A freeze keeps its
    timestamps, so it is not a gap."""
    ts = pd.Series(utc_index(df["timestamp"], source_tz))
    seconds = ts.diff().dt.total_seconds()
    gaps = pd.DataFrame({"gap_start": ts.shift(1), "gap_end": ts, "seconds": seconds})
    return gaps[gaps["seconds"] > min_gap_s].reset_index(drop=True)


def label_gaps(gaps: pd.DataFrame, labels: dict) -> pd.DataFrame:
    """Add a ``label``: long_outage, overnight_stop or dropout.

    ``labels`` is the profile's ``gap_policy.gap_labels``. A long outage lasts
    at least ``long_outage``. An overnight stop is shorter than that, at least
    ``overnight_stop.min_duration``, starts between ``starts_after`` and
    ``starts_before`` (wrapping midnight) and ends before ``ends_before``.
    Everything else is a dropout. Clock times are UTC.
    """
    night = labels["overnight_stop"]
    long_s = parse_duration(labels["long_outage"]).total_seconds()
    min_s = parse_duration(night["min_duration"]).total_seconds()
    after, before, ends = (_clock(night[k]) for k in ("starts_after", "starts_before", "ends_before"))

    start_clock = gaps["gap_start"].dt.time
    end_clock = gaps["gap_end"].dt.time
    if after > before:                     # the window wraps midnight
        starts_at_night = start_clock.map(lambda t: t >= after or t < before)
    else:
        starts_at_night = start_clock.map(lambda t: after <= t < before)
    overnight = ((gaps["seconds"] >= min_s) & (gaps["seconds"] < long_s)
                 & starts_at_night.astype(bool) & end_clock.map(lambda t: t < ends).astype(bool))
    label = np.where(gaps["seconds"] >= long_s, "long_outage",
                     np.where(overnight, "overnight_stop", "dropout"))
    return gaps.assign(label=label)


# --- profile-driven entry point ----------------------------------------------

def mask_grid_and_gaps(df: pd.DataFrame, profile: dict):
    """Freeze runs, grid and labelled gaps with every parameter from the profile."""
    analog, digital = analog_names(profile), digital_names(profile)
    rule = profile["freeze_rule"]
    runs = freeze_runs(df, analog,
                       min_minutes=parse_duration(rule["min_duration"]).total_seconds() / 60,
                       max_step_s=rule["max_step_s"])
    frozen = frozen_rows(df, runs)
    tz = profile.get("source_timezone", "UTC")
    policy = profile["gap_policy"]
    grid = to_grid(df, frozen, analog, digital, step=profile["sampling"]["resample_rule"],
                   max_bridge_s=policy["max_bridge_seconds"], source_tz=tz)
    labels = policy["gap_labels"]
    gaps = label_gaps(raw_gaps(df, parse_duration(labels["min_gap"]).total_seconds(), tz), labels)
    return runs, frozen, grid, gaps
