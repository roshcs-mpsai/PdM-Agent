"""Config invariants: the facts the rest of the system depends on.

These are the checks that would have caught the 1 Hz / 15.2 M errors.
"""
import datetime as dt
import re
from pathlib import Path

import pandas as pd
import yaml

PROFILE_DIR = Path(__file__).resolve().parents[1] / "profiles" / "metropt3_apu"

DAY = pd.Timedelta(days=1)
EPS = pd.Timedelta(seconds=1)   # spans written with a time include their end instant


def _load(name):
    with open(PROFILE_DIR / name) as f:
        return yaml.safe_load(f)


def _t(value):
    return pd.Timestamp(str(value))


def _span(entry):
    """events.yaml time conventions -> half-open [start, end).

    A date-only end covers that whole day; an end written with a time is
    included, so it is widened by one second (the logger never steps < 8 s).
    """
    end = entry["end"]
    if isinstance(end, dt.date) and not isinstance(end, dt.datetime):
        return _t(entry["start"]), _t(end) + DAY
    return _t(entry["start"]), _t(end) + EPS


def _overlaps(a, b):
    return a[0] < b[1] and b[0] < a[1]


def _dur(text):
    """'7d', '48h', '30min', '10s' -> Timedelta (the units events.yaml uses)."""
    number, unit = re.fullmatch(r"(\d+)(s|min|h|d)", text).groups()
    seconds = {"s": 1, "min": 60, "h": 3600, "d": 86400}[unit]
    return pd.Timedelta(seconds=int(number) * seconds)


def test_asset_profile_invariants():
    p = _load("asset_profile.yaml")
    assert p["asset"]["records"] == 1_516_948
    assert p["sampling"]["step_seconds"] == 10
    assert p["sampling"]["rate_hz"] == 0.1
    assert len(p["signals"]["analog"]) == 7
    assert len(p["signals"]["digital"]) == 8
    # every digital channel declares a polarity verdict
    assert all("polarity" in s for s in p["signals"]["digital"])
    # Reservoirs is excluded from detector inputs (r = 1.00 with TP3)
    assert "Reservoirs" not in p["detector"]["inputs"]
    # the feature pipeline (modality seam) is declared
    assert p["features"]["pipeline"]


def test_events_invariants():
    e = _load("events.yaml")
    assert len(e["failures"]) == 4
    ids = [f["id"] for f in e["failures"]]
    assert ids == ["F1", "F2", "F3", "F4"]
    # all four 2020 events are air leaks (verified against UCI, 2026-09-19)
    assert all(f["type"] == "air_leak" for f in e["failures"])
    # F2-F3 adjacency is declared, so the harness can pair the folds
    f3 = next(f for f in e["failures"] if f["id"] == "F3")
    assert f3.get("paired_with") == "F2"
    # the three padding types exist and are distinct settings
    assert set(e["guard_band"]) == {"before", "after"}
    assert set(e["healthy_buffer"]) == {"before_failure", "after_repair"}
    assert len(e["lead_horizons"]) == 3
    assert len(e.get("uncertain_periods", [])) >= 9
    assert len(e["data_quality_masks"]) == 10


def test_events_maintenance_hours_from_the_pdf():
    """v0.2: hour-level repair times; the PDF's "30Apr" is a typo for 30 May."""
    e = _load("events.yaml")
    repairs = {f["id"]: f["maintenance"] for f in e["failures"]}
    assert repairs["F1"] is None
    assert _t(repairs["F2"]) == pd.Timestamp("2020-05-30 12:00")
    assert _t(repairs["F3"]) == pd.Timestamp("2020-06-08 16:00")
    assert _t(repairs["F4"]) == pd.Timestamp("2020-07-16 00:00")
    for f in e["failures"]:
        if f["maintenance"] is not None:
            assert _t(f["maintenance"]) > _t(f["end"]), f["id"]


def test_freeze_masks_are_the_ten_verified_runs():
    """Ten logger freezes, 169.8 h, sorted and disjoint (EDA 2.2)."""
    masks = _load("events.yaml")["data_quality_masks"]
    assert all(m["kind"] == "logger_freeze" for m in masks)
    spans = [(_t(m["start"]), _t(m["end"])) for m in masks]
    assert spans == sorted(spans)
    assert all(a[1] < b[0] for a, b in zip(spans, spans[1:]))
    hours = sum((e - s).total_seconds() for s, e in spans) / 3600
    assert round(hours, 1) == 169.8


def test_uncertain_periods_rebuilt_from_the_episodes():
    e = _load("events.yaml")
    periods = e["uncertain_periods"]
    tags = [p["tag"] for p in periods]
    assert set(tags) == {"lps_heavy", "load_held", "probable_leak"}
    assert tags.count("lps_heavy") == 3
    assert tags.count("load_held") == 3
    assert tags.count("probable_leak") == 3
    # the Jun 12-14 and Jul 24-26 entries were logger-freeze artifacts
    for start, end in [("2020-06-12", "2020-06-15"), ("2020-07-24", "2020-07-27")]:
        artifact = (pd.Timestamp(start), pd.Timestamp(end))
        assert not any(_overlaps(_span(p), artifact) for p in periods), (start, end)


def test_regimes_tile_the_campaign():
    """Half-open regimes, no gaps or overlaps; the third starts at the F3 repair."""
    e = _load("events.yaml")
    regimes = e["regimes"]
    assert [r["name"] for r in regimes] == ["light_duty", "normal", "dv_pressure_flat"]
    assert _t(regimes[0]["start"]) == _t(e["campaign"][0])
    assert _t(regimes[-1]["end"]) == _t(e["campaign"][1]) + DAY
    for a, b in zip(regimes, regimes[1:]):
        assert _t(a["end"]) == _t(b["start"]), (a["name"], b["name"])
    f3 = next(f for f in e["failures"] if f["id"] == "F3")
    assert _t(regimes[2]["start"]) == _t(f3["maintenance"])


def test_healthy_candidates_are_clear_of_every_exclusion():
    """After their `exclude` spans, candidates touch no mask, uncertain or grey
    period, and no failure buffer (7 d before the report to 48 h after repair)."""
    e = _load("events.yaml")
    ids = [c["id"] for c in e["healthy_candidates"]]
    assert "H5" not in ids                       # 0 of 394 windows survive
    assert not any("conflict" in c for c in e["healthy_candidates"])

    before = _dur(e["healthy_buffer"]["before_failure"])
    after = _dur(e["healthy_buffer"]["after_repair"])
    blocked = [_span(m) for m in e["data_quality_masks"]]
    blocked += [_span(p) for p in e["uncertain_periods"]]
    blocked += [_span(g) for g in e["grey_periods"]]
    for f in e["failures"]:
        repair = f["maintenance"] if f["maintenance"] is not None else f["end"]
        blocked.append((_t(f["start"]) - before, _t(repair) + after))

    step = pd.Timedelta(minutes=1)
    for c in e["healthy_candidates"]:
        start, end = _span(c)
        assert (end - start) / DAY == c["days"], c["id"]
        cut = [_span(x) for x in c.get("exclude", [])]
        for s, x in cut:
            assert start <= s and x <= end, f"{c['id']}: exclude outside the candidate"
        # minute grid over the candidate, minus its excludes
        minutes = pd.date_range(start, end - step, freq=step)
        keep = pd.Series(True, index=minutes)
        for s, x in cut:
            keep &= ~((minutes + step > s) & (minutes < x))
        kept = minutes[keep.to_numpy()]
        for b in blocked:
            hit = (kept + step > b[0]) & (kept < b[1])
            assert not hit.any(), f"{c['id']} touches {b[0]} - {b[1]}"


def test_no_asset_names_outside_profiles():
    """Portability guard: MetroPT signal names stay out of shared code."""
    repo = PROFILE_DIR.parents[1]
    offenders = []
    for py in (repo / "services").rglob("*.py"):
        text = py.read_text(errors="ignore")
        if any(name in text for name in ("TP2", "Oil_temperature", "Caudal_impulses")):
            offenders.append(str(py))
    assert not offenders, f"asset-specific names in shared code: {offenders}"
