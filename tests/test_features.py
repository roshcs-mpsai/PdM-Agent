"""Windows and features in pdm_common (T5, T6; ING-11, ING-12, DET-04).

Synthetic tests pin each rule and run in CI. The data test rebuilds the whole
window table from the raw MetroPT-3 file and holds it to the EDA v4 notebook's
output in data/processed_v4 (skipped when either is absent).
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pdm_common.features import (
    FeatureSpecError,
    features_file,
    load_feature_spec,
    trailing_mean_of_steps,
    trailing_sum,
    window_features,
)
from pdm_common.profile import analog_names, digital_names, load_profile
from pdm_common.quality import to_grid
from pdm_common.windows import add_folds, make_windows, window_ends, windowing_params
from pdm_common.zones import zone_scheme

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
RAW = ROOT / "data" / "MetroPT3(AirCompressor).csv"
REFERENCE = ROOT / "data" / "processed_v4"


@pytest.fixture(scope="module")
def profile():
    return load_profile(PROFILE_PATH)


@pytest.fixture(scope="module")
def spec(profile):
    return load_feature_spec(features_file(profile, PROFILE_PATH))


def synthetic_grid(profile, spec, hours=7.0, current=6.0, start="2021-03-01", drop_seconds=(), rest_minutes=0):
    """Raw rows every 10 s on the profile's channels, the motor role at ``current``
    (stopped for the first ``rest_minutes``), the panel role at 9 bar; rows whose
    offset (s) is in ``drop_seconds`` are left out."""
    analog, digital = analog_names(profile), digital_names(profile)
    n = int(hours * 360)
    t = pd.Timestamp(start) + pd.to_timedelta(np.arange(n) * 10, unit="s")
    rows = pd.DataFrame({"timestamp": t})
    for i, name in enumerate(analog):
        rows[name] = 1.0 + i + 0.001 * np.arange(n)
    rows[spec.roles["motor_current"]] = np.where(np.arange(n) < rest_minutes * 6, 0.0, current)
    rows[spec.roles["panel_pressure"]] = 9.0
    for name in digital:
        rows[name] = 0.0
    keep = ~np.isin(np.arange(n) * 10, list(drop_seconds))
    rows = rows[keep].reset_index(drop=True)
    grid = to_grid(rows, np.zeros(len(rows), bool), analog, digital, step="10s", max_bridge_s=30)
    return grid


def test_the_feature_file_declares_the_42_features(spec):
    assert len(spec.names) == 42 == len(set(spec.names))
    assert len(spec.flagged("detector_v1")) == 21
    assert spec.win_bins == 60
    assert spec.max_lookback() == pd.Timedelta(days=7)
    roles = spec.dictionary()["role"].value_counts().to_dict()
    assert roles == {"summary statistic": 25, "engineered": 5, "state": 5, "leak": 5, "data-quality flag": 2}


def test_trailing_sums():
    assert list(trailing_sum([1, 2, np.nan, 4], 2)) == [1, 3, 2, 4]
    steps = np.array([True, True, False])
    out = trailing_mean_of_steps(np.array([1.0, 3.0, 100.0]), steps, n=3, min_steps=2)
    assert np.isnan(out[0]) and np.isnan(out[1]) and out[2] == 2.0 and out[3] == 2.0


def test_load_share_is_one_after_six_loaded_hours(profile, spec):
    grid = synthetic_grid(profile, spec, hours=7.5, current=6.0, rest_minutes=10)
    ends = window_ends(grid.index, spec.win_bins, spec.stride)
    feats = window_features(grid, ends, spec)
    assert feats["load_share_6h"].iloc[-1] == pytest.approx(1.0)
    assert feats["rest_share_6h"].iloc[-1] == pytest.approx(0.0)
    # the motor last rested ten minutes in: hours since rest grows with the run
    last_point, last_rest = 2699 * 10, 59 * 10            # seconds from the start
    assert feats["hours_since_rest"].iloc[-1] == pytest.approx((last_point - last_rest) / 3600)
    # an hour of data is needed before a share is scored
    early = feats["share_coverage_6h"] * 6 < 1.0
    assert feats.loc[early, "load_share_6h"].isna().all()
    assert len(feats.columns) == 42


def test_a_stopped_motor_reads_as_rest(profile, spec):
    grid = synthetic_grid(profile, spec, hours=2, current=0.0)
    ends = window_ends(grid.index, spec.win_bins, spec.stride)
    feats = window_features(grid, ends, spec)
    assert feats["rest_share_6h"].iloc[-1] == pytest.approx(1.0)
    assert feats["hours_since_rest"].iloc[-1] == 0.0


def labelled(grid):
    out = grid.copy()
    out["zone_code"] = 0
    return out


def toy_events():
    return {"failures": [{"id": "F1", "start": "2021-03-20 00:00", "end": "2021-03-20 06:00", "maintenance": None}],
            "guard_band": {"before": "24h", "after": "12h"}, "expert_band": {"before": "1h", "after": "6h"},
            "healthy_buffer": {"before_failure": "7d", "after_repair": "48h"},
            "gap_edges": {"after_gaps_of": "30min", "length": "15min"},
            "regimes": [{"name": "only", "start": "2021-01-01 00:00", "end": "2022-01-01 00:00"}]}


def test_a_window_over_a_40s_gap_is_dropped_and_one_with_bridged_points_kept(profile, spec):
    # a 30 s gap leaves two empty points, bridged; a 40 s gap leaves three, not bridged
    grid = synthetic_grid(profile, spec, hours=1, drop_seconds=(610, 620, 1810, 1820, 1830))
    assert grid["bridged"].sum() == 2 and (~(grid["observed"] | grid["bridged"])).sum() == 3
    wp = windowing_params(profile)
    ends = window_ends(grid.index, wp["win_bins"], wp["stride"])
    win = make_windows(labelled(grid), ends, toy_events(), win_bins=wp["win_bins"],
                       window_length=wp["length"], lookback=pd.Timedelta(hours=6), step=wp["step"],
                       scheme=zone_scheme(toy_events()))
    start = win["window_start"].dt.tz_convert(None)
    over_bridge = win[(start <= pd.Timestamp("2021-03-01 00:10")) & (start + pd.Timedelta(minutes=10)
                                                                    > pd.Timestamp("2021-03-01 00:10:20"))]
    over_gap = win[(start <= pd.Timestamp("2021-03-01 00:30:10")) & (start + pd.Timedelta(minutes=10)
                                                                      > pd.Timestamp("2021-03-01 00:30:30"))]
    assert len(over_bridge) and over_bridge["complete"].all()
    assert len(over_gap) and not over_gap["complete"].any()
    # windows start on full minutes and the non-overlapping subset starts on ten
    assert (win["window_start"].dt.second == 0).all()
    assert (win.loc[win["aligned"], "window_start"].dt.minute % 10 == 0).all()


def test_folds_roles_and_the_asymmetric_blocks(profile, spec):
    times = pd.date_range("2021-03-01", "2021-04-05", freq="1min", tz="UTC")
    win = pd.DataFrame({"window_start": times, "window_end": times + pd.Timedelta(minutes=10),
                        "zone": "negative"})
    events = toy_events()
    events["failures"].append({"id": "F2", "start": "2021-03-22 00:00", "end": "2021-03-22 06:00",
                               "maintenance": "2021-03-22 12:00"})
    s1, e1 = pd.Timestamp("2021-03-20 00:00", tz="UTC"), pd.Timestamp("2021-03-20 06:00", tz="UTC")
    s2, e2 = pd.Timestamp("2021-03-22 00:00", tz="UTC"), pd.Timestamp("2021-03-22 06:00", tz="UTC")
    win.loc[(win.window_end > s1 - pd.Timedelta(hours=24)) & (win.window_start <= e1 + pd.Timedelta(hours=12)), "zone"] = "positive"
    win.loc[(win.window_end > s2 - pd.Timedelta(hours=24)) & (win.window_start <= e2 + pd.Timedelta(hours=12)), "zone"] = "positive"
    spans = add_folds(win, events)
    # F2's block starts where F1's guard band ends; F1's stops where F2's begins
    assert spans["F2"][0] == e1 + pd.Timedelta(hours=12)
    assert spans["F1"][1] == s2 - pd.Timedelta(hours=24)
    assert not (win["block_F1"] & (win["event"] == "F2")).any()
    # roles by ISO week; non-negative windows carry none
    week = win["window_start"].dt.isocalendar().week.astype(int)
    neg = win["zone"] == "negative"
    assert (win.loc[neg & (week % 2 == 0), "neg_role"] == "fit").all()
    assert (win.loc[neg & (week % 4 == 1), "neg_role"] == "calibrate").all()
    assert (win.loc[neg & (week % 4 == 3), "neg_role"] == "check").all()
    assert (win.loc[~neg, "neg_role"] == "none").all()
    # look-back flags: negative windows within 7 days after a block read it
    assert win.loc[win["lookback_F2"], "window_start"].min() >= spans["F2"][1] - pd.Timedelta(minutes=10)


def test_the_feature_file_is_checked(tmp_path, spec):
    import yaml
    data = dict(spec.raw)
    data["features"] = [dict(f) for f in spec.raw["features"]]
    data["features"][25] = {**data["features"][25], "compute": {"kind": "share_of_time", "role": "vibration",
                                                               "above": 1, "below": 2, "window": "6h",
                                                               "min_data": "1h"}}
    data["features"][26] = {**data["features"][26], "compute": {"kind": "wavelet"}}
    path = tmp_path / "features.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(FeatureSpecError) as info:
        load_feature_spec(path)
    text = str(info.value)
    assert "unknown kind 'wavelet'" in text
    assert "give exactly one of above or below" in text
    assert "role 'vibration' is not mapped" in text


@pytest.mark.skipif(not (RAW.exists() and (REFERENCE / "windows_10min.csv.gz").exists()),
                    reason="needs the raw MetroPT-3 CSV and data/processed_v4")
def test_the_pipeline_reproduces_the_eda_v4_window_table():
    """T5-T7 done-check: counts, window key, labels and all 42 features."""
    import importlib.util
    spec_ = importlib.util.spec_from_file_location("build_windows", ROOT / "tools" / "build_windows.py")
    build_windows = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(build_windows)
    result = build_windows.build(PROFILE_PATH, data_path=RAW, log=lambda *a: None)
    expected = json.loads((REFERENCE / "manifest.json").read_text())["expected_counts"]
    w = result["windows"]
    assert len(w) == expected["complete_windows"] == 239_172
    assert int((w.zone == "negative").sum()) == expected["negative_windows"] == 188_293
    assert build_windows.compare(result, REFERENCE, log=lambda *a: None) == []
