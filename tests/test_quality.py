"""Freeze mask, 10 s grid and gap policy (R1a.4; ING-09, ING-10).

Synthetic tests pin each rule; with the MetroPT-3 CSV present, the full-file
tests check the counts verified in the EDA notebook (Sections 2.1, 2.2, 7.1).
Signal names come from the profile.
"""
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pdm_common.events import load_events
from pdm_common.profile import analog_names, digital_names, load_profile
from pdm_common.quality import (
    freeze_runs,
    frozen_rows,
    label_gaps,
    mask_grid_and_gaps,
    raw_gaps,
    to_grid,
    utc_index,
)
from pdm_common.timeutil import to_utc

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
DATA = ROOT / "data" / "MetroPT3(AirCompressor).csv"


@pytest.fixture(scope="module")
def profile():
    return load_profile(PROFILE_PATH)


def frame(profile, times, frozen_from=None):
    """Rows at ``times`` (seconds from 2020-03-21 00:00). Analog channel i
    ramps as i + t/100; digital channels alternate with the row number. From
    ``frozen_from`` on, the analog values stop changing."""
    analog, digital = analog_names(profile), digital_names(profile)
    t0 = dt.datetime(2020, 3, 21)
    rows = []
    for k, t in enumerate(times):
        t_eff = t if frozen_from is None or t < frozen_from else frozen_from
        rows.append({"timestamp": (t0 + dt.timedelta(seconds=t)).isoformat(sep=" "),
                     **{a: i + t_eff / 100 for i, a in enumerate(analog)},
                     **{d: float(k % 2) for d in digital}})
    return pd.DataFrame(rows)


def grid_of(profile, df, frozen=None):
    frozen = np.zeros(len(df), dtype=bool) if frozen is None else frozen
    return to_grid(df, frozen, analog_names(profile), digital_names(profile),
                   step=profile["sampling"]["resample_rule"],
                   max_bridge_s=profile["gap_policy"]["max_bridge_seconds"])


def at(grid, seconds):
    return grid.loc[pd.Timestamp("2020-03-21", tz="UTC") + pd.Timedelta(seconds=seconds)]


def test_a_20s_gap_is_bridged_and_a_40s_gap_is_not(profile):
    """The R1a.4 check. Steps of 10 s, then a 20 s, a 30 s and a 40 s gap."""
    times = [*range(0, 70, 10), *range(80, 160, 10), *range(180, 260, 10), *range(290, 360, 10)]
    grid = grid_of(profile, frame(profile, times))
    first = analog_names(profile)[0]

    # 20 s gap (60 -> 80): one empty point, bridged, interpolated in time
    assert at(grid, 70)["bridged"] and not at(grid, 70)["observed"]
    assert at(grid, 70)[first] == pytest.approx(0.70)
    # 30 s gap (150 -> 180): two empty points, bridged
    assert at(grid, 160)["bridged"] and at(grid, 170)["bridged"]
    # 40 s gap (250 -> 290): three empty points, left missing
    for s in (260, 270, 280):
        row = at(grid, s)
        assert not row["bridged"] and not row["observed"] and np.isnan(row[first])
    assert grid["bridged"].sum() == 3


def test_digital_channels_are_carried_forward_not_interpolated(profile):
    grid = grid_of(profile, frame(profile, [0, 10, 20, 40, 50]))
    digital = digital_names(profile)[0]
    assert at(grid, 30)["bridged"]
    assert at(grid, 30)[digital] == at(grid, 20)[digital]


def test_edge_runs_are_never_bridged(profile):
    """Masked rows at the start leave a short empty run at the edge: not bridged."""
    df = frame(profile, range(0, 100, 10))
    frozen = np.zeros(len(df), dtype=bool)
    frozen[:2] = True                                  # mask the first two rows
    grid = grid_of(profile, df, frozen)
    assert not grid["observed"].iloc[:2].any()
    assert not grid["bridged"].any()


def test_two_samples_in_one_bin_are_averaged(profile):
    grid = grid_of(profile, frame(profile, [0, 9, 20]))  # a 9 s step
    first = analog_names(profile)[0]
    assert at(grid, 0)[first] == pytest.approx((0.0 + 0.09) / 2)
    assert at(grid, 0)[digital_names(profile)[0]] == 1.0    # the last value in the bin


def test_freeze_runs_rules(profile):
    analog = analog_names(profile)
    rule = profile["freeze_rule"]
    kwargs = dict(min_minutes=30, max_step_s=rule["max_step_s"])
    # 35 min of identical analog values at 12 s steps: one run
    times = [*range(0, 600, 10), *range(600, 600 + 35 * 60, 12), *range(2700, 3300, 10)]
    runs = freeze_runs(frame(profile, times, frozen_from=600), analog, **kwargs)
    assert len(runs) == 1 and runs["minutes"].iloc[0] >= 30
    # 20 min frozen: too short
    times = [*range(0, 600, 10), *range(600, 600 + 20 * 60, 12), *range(1900, 2500, 10)]
    assert freeze_runs(frame(profile, times, frozen_from=600), analog, **kwargs).empty
    # 40 min constant but logged every 20 s: not the freeze signature
    times = [*range(0, 600, 10), *range(600, 600 + 40 * 60, 20), *range(3100, 3700, 10)]
    assert freeze_runs(frame(profile, times, frozen_from=600), analog, **kwargs).empty


def test_frozen_rows_are_masked_before_the_grid(profile):
    times = [*range(0, 600, 10), *range(600, 600 + 35 * 60, 12), *range(2700, 3300, 10)]
    df = frame(profile, times, frozen_from=600)
    runs = freeze_runs(df, analog_names(profile))
    frozen = frozen_rows(df, runs)
    grid = grid_of(profile, df, frozen)
    inside = grid.loc[pd.Timestamp("2020-03-21 00:12", tz="UTC"):pd.Timestamp("2020-03-21 00:40", tz="UTC")]
    assert not inside["observed"].any() and not inside["bridged"].any()


def test_gap_labels(profile):
    labels = profile["gap_policy"]["gap_labels"]
    t = lambda text: pd.Timestamp(text, tz="UTC")  # noqa: E731
    gaps = pd.DataFrame({
        "gap_start": [t("2020-03-21 23:10"), t("2020-03-22 13:00"), t("2020-03-23 12:00"),
                      t("2020-03-24 01:00"), t("2020-03-24 22:00"), t("2020-03-25 18:30")],
        "gap_end":   [t("2020-03-22 04:40"), t("2020-03-22 14:00"), t("2020-03-24 06:00"),
                      t("2020-03-24 07:00"), t("2020-03-24 23:00"), t("2020-03-26 01:00")],
    })
    gaps["seconds"] = (gaps["gap_end"] - gaps["gap_start"]).dt.total_seconds()
    assert list(label_gaps(gaps, labels)["label"]) == [
        "overnight_stop",   # 5.5 h, 23:10 -> 04:40
        "dropout",          # daytime hour
        "long_outage",      # 18 h
        "dropout",          # starts at night, ends after 06:00
        "dropout",          # at night but under 3 h
        "dropout",          # starts before 19:00
    ]


def test_the_clock_is_utc_across_the_dst_change():
    """ING-09: the file records straight through 01:00-02:00 UTC on 2020-03-29.
    Read as Lisbon local time those stamps do not exist, so the source is UTC."""
    stamps = pd.Series(pd.date_range("2020-03-29 00:50", "2020-03-29 02:10", freq="10s"))
    utc = utc_index(stamps, "UTC")
    steps = pd.Series(utc).diff().dt.total_seconds().iloc[1:]
    assert (steps == 10).all()                               # no jump, no hole
    with pytest.raises(Exception) as info:
        utc_index(stamps, "Europe/Lisbon")
    # pandas 2 raises pytz's NonExistentTimeError("2020-03-29 01:00:00"); pandas 3
    # a ValueError that says "nonexistent". Accept either wording.
    said = f"{type(info.value).__name__} {info.value}".lower()
    assert "nonexist" in said or "does not exist" in said


# --- the full file ---------------------------------------------------------

needs_data = pytest.mark.skipif(not DATA.exists(), reason="needs the MetroPT-3 CSV under data/")


@pytest.fixture(scope="module")
def full(profile):
    df = pd.read_csv(DATA, index_col=0)
    return df, *mask_grid_and_gaps(df, profile)


@needs_data
def test_freeze_runs_finds_exactly_the_ten_masked_runs(full):
    """The R1a.4 check: ten runs, 169.8 h, equal to events.yaml to the second."""
    df, runs, frozen, grid, gaps = full
    assert len(runs) == 10
    assert round(runs["minutes"].sum() / 60, 1) == 169.8
    assert int(frozen.sum()) == 50_677
    masks = load_events(PROFILE_PATH.parent / "events.yaml")["data_quality_masks"]
    found = [(to_utc(f.to_pydatetime()), to_utc(l.to_pydatetime()))
             for f, l in zip(runs["first"], runs["last"])]
    assert found == [(to_utc(m["start"]), to_utc(m["end"])) for m in masks]


@needs_data
def test_full_grid_counts(full):
    df, runs, frozen, grid, gaps = full
    assert len(grid) == 1_841_760
    assert int(grid["observed"].sum()) == 1_453_431
    assert int(grid["bridged"].sum()) == 86
    assert str(grid.index.tz) == "UTC"


@needs_data
def test_full_gap_labels(full):
    df, runs, frozen, grid, gaps = full
    assert len(gaps) == 331
    counts = gaps["label"].value_counts().to_dict()
    assert counts == {"dropout": 259, "overnight_stop": 57, "long_outage": 15}
    long_hours = gaps.loc[gaps["label"] == "long_outage", "seconds"].sum() / 3600
    assert round(long_hours, 1) == 339.6
    assert len(raw_gaps(df)) == 331            # default threshold = the profile's 50 s
