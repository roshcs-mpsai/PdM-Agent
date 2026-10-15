"""Streaming windows (T3; ING-10 to ING-12): the stream gives the batch windows.

Every test feeds records one at a time (or in chunks) through
pdm_common.stream.StreamWindows and compares the windows with the batch
pipeline run on the same records: the same candidate windows, the same
complete ones, and every feature within 1e-9. Synthetic streams run anywhere;
the recorded one needs the MetroPT-3 CSV under data/.
"""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pdm_common.features import features_file, load_feature_spec, window_features
from pdm_common.profile import analog_names, digital_names, load_profile
from pdm_common.quality import mask_grid_and_gaps
from pdm_common.stream import StreamWindows
from pdm_common.windows import window_ends

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
RAW = ROOT / "data" / "MetroPT3(AirCompressor).csv"


@pytest.fixture(scope="module")
def profile():
    return load_profile(PROFILE_PATH)


@pytest.fixture(scope="module")
def spec(profile):
    return load_feature_spec(features_file(profile, PROFILE_PATH))


def synthetic(profile, spec, days, *, seed=0, start="2021-03-01", no_rest=None):
    """Rows every 10 s: noisy analog channels (so no accidental freeze), the motor
    loaded, offloaded and resting in a 20-minute cycle, never resting inside the
    ``no_rest`` (start, end) day range."""
    rng = np.random.default_rng(seed)
    analog, digital = analog_names(profile), digital_names(profile)
    n = int(days * 8640)
    t = pd.Timestamp(start) + pd.to_timedelta(np.arange(n) * 10, unit="s")
    rows = pd.DataFrame({"timestamp": t})
    for i, name in enumerate(analog):
        rows[name] = 2.0 + i + rng.normal(0, 0.01, n)
    phase = np.arange(n) % 120
    motor = np.where(phase < 72, 6.5, np.where(phase < 108, 4.0, 0.2))
    if no_rest:
        day = np.arange(n) / 8640
        motor = np.where((day >= no_rest[0]) & (day < no_rest[1]) & (motor < 1), 4.0, motor)
    rows[spec.roles["motor_current"]] = motor + rng.normal(0, 0.05, n)
    rows[spec.roles["panel_pressure"]] = 8.5 + np.where(phase < 72, phase / 72, 1 - (phase - 72) / 48) \
        + rng.normal(0, 0.01, n)
    for name in digital:
        rows[name] = 0.0
    rows[spec.roles["load_command"]] = (phase < 72).astype(float)
    return rows


def freeze(rows, profile, first, n):
    """Rows first..first+n-1 repeat the analog values of row first-1 (a logger freeze)."""
    analog = analog_names(profile)
    rows.loc[first:first + n - 1, analog] = rows.loc[first - 1, analog].to_numpy()
    return rows


def drop(rows, first, n):
    return rows.drop(index=range(first, first + n)).reset_index(drop=True)


def batch(rows, profile, spec):
    _, _, grid, _ = mask_grid_and_gaps(rows, profile)
    ends = window_ends(grid.index, spec.win_bins, spec.stride)
    valid = (grid["observed"] | grid["bridged"]).astype(int).rolling(spec.win_bins).sum().to_numpy()[ends]
    complete = valid == spec.win_bins
    starts = grid.index[ends - (spec.win_bins - 1)]
    feats = window_features(grid, ends[complete], spec).set_index(starts[complete])
    return len(ends), feats


def stream(rows, profile, spec, every=1, excluded=None, **kwargs):
    sw = StreamWindows(profile, spec, **kwargs)
    cols = analog_names(profile) + digital_names(profile)
    values = rows[cols].to_numpy(float)
    ts = pd.DatetimeIndex(rows["timestamp"])
    out = []
    for i in range(len(rows)):
        record = dict(zip(cols, values[i]))
        ex = (excluded or {}).get(i, ())
        if every == 1:
            out += sw.add(ts[i], record, ex)
        else:
            sw.append(ts[i], record, ex)
            if i % every == every - 1:
                out += sw.compute()
    out += sw.flush()
    return sw, out


def assert_same(out, n_candidates, reference):
    assert len(out) == n_candidates
    got = pd.DataFrame([w.features for w in out if w.complete], index=[w.start for w in out if w.complete])
    assert got.index.equals(reference.index)
    assert list(got.columns) == list(reference.columns)
    assert (got.isna() == reference.isna()).all().all()
    diff = ((got - reference).abs() / reference.abs().clip(lower=1)).max().max()
    assert np.isnan(diff) or diff < 1e-9, diff


def test_record_by_record_matches_batch_through_freezes_and_gaps(profile, spec):
    rows = synthetic(profile, spec, days=0.4)
    rows = freeze(rows, profile, 300, 250)        # 41 min of identical rows: a freeze, masked
    rows = freeze(rows, profile, 900, 30)         # 5 min: not a freeze, kept
    rows = drop(rows, 1200, 1)                    # a 20 s step: bridged
    rows = drop(rows, 1400, 3)                    # a 40 s step: three missing points, not bridged
    rows = drop(rows, 1700, 720)                  # two hours: a long gap, then a resumption
    n, reference = batch(rows, profile, spec)
    sw, out = stream(rows, profile, spec, every=1)
    assert_same(out, n, reference)
    assert sw.counts["windows"] == n and sw.counts["complete"] == len(reference)
    assert sw.counts["dropped"] == n - len(reference) > 0


def test_windows_wait_for_bridging_and_for_a_possible_freeze(profile, spec):
    rows = synthetic(profile, spec, days=0.2)
    rows = freeze(rows, profile, 900, 250)
    sw = StreamWindows(profile, spec)
    cols = analog_names(profile) + digital_names(profile)
    first_seen = {}
    for i, row in rows.iterrows():
        for w in sw.add(row["timestamp"], {c: row[c] for c in cols}):
            first_seen[w.start] = row["timestamp"]
    lags = pd.Series({start + spec.window_length: seen for start, seen in first_seen.items()})
    lag_s = (lags - lags.index.tz_convert(None)).dt.total_seconds()
    # outside the freeze a window is released 20 s after it ends: its last point
    # (10 s before the end) waits 30 s for bridging
    assert lag_s.median() == 20 and lag_s.min() >= 20
    # windows reaching into the run wait until it has lasted the freeze minimum
    run_first = rows.loc[900, "timestamp"]
    held = lag_s[(lags.index.tz_convert(None) > run_first)]
    assert held.max() > 25 * 60


def test_chunked_compute_with_seven_day_trim_and_anchors(profile, spec):
    """Eight days without a motor rest, then a 50 h gap: the rest before them and
    the gap's start are older than the seven-day look-back, and the stream keeps
    them (hours_since_rest, gap_hours and gap_loss_rate depend on them)."""
    rows = synthetic(profile, spec, days=11.4, no_rest=(0.3, 8.6))
    rows = drop(rows, int(8.6 * 8640), int(50 * 360))
    n, reference = batch(rows, profile, spec)
    sw, out = stream(rows, profile, spec, every=2160)
    assert_same(out, n, reference)
    assert reference["hours_since_rest"].max() > 7 * 24
    assert reference["gap_hours"].max() == 48            # the cap
    # the buffer was cut once the anchors moved on
    first = pd.Timestamp(rows.loc[0, "timestamp"]).tz_localize("UTC")
    assert pd.Timestamp(sw._ts[0], tz="UTC") > first + pd.Timedelta(days=3)


def test_excluded_values_are_missing_on_the_grid(profile, spec):
    rows = synthetic(profile, spec, days=0.2)
    oil = spec.roles["oil_temperature"]
    excluded = {500: (oil,)}                                  # one bad reading: bridged
    excluded.update({i: (oil,) for i in range(1000, 1120)})   # twenty minutes: not usable
    _, out = stream(rows, profile, spec, every=60, excluded=excluded)
    by_start = {w.start: w for w in out}
    t500 = pd.Timestamp(rows.loc[500, "timestamp"]).tz_localize("UTC").floor("10s")
    around = [w for s, w in by_start.items() if s <= t500 < s + spec.window_length]
    assert around and all(w.complete and w.n_bridged >= 1 for w in around)
    t1050 = pd.Timestamp(rows.loc[1050, "timestamp"]).tz_localize("UTC")
    assert not any(w.complete for s, w in by_start.items() if s <= t1050 < s + spec.window_length)


def test_time_order_is_required(profile, spec):
    sw = StreamWindows(profile, spec)
    rows = synthetic(profile, spec, days=0.01)
    cols = analog_names(profile) + digital_names(profile)
    sw.add(rows.loc[1, "timestamp"], {c: rows.loc[1, c] for c in cols})
    with pytest.raises(ValueError):
        sw.add(rows.loc[0, "timestamp"], {c: rows.loc[0, c] for c in cols})


@pytest.mark.skipif(not RAW.exists(), reason="needs the MetroPT-3 CSV under data/")
def test_recorded_days_with_three_freezes_and_the_first_failure(profile, spec):
    df = pd.read_csv(RAW, index_col=0, parse_dates=["timestamp"])
    rows = df[(df["timestamp"] >= "2020-04-12") & (df["timestamp"] < "2020-04-21 06:00")].reset_index(drop=True)
    n, reference = batch(rows, profile, spec)
    _, out = stream(rows, profile, spec, every=720)
    assert_same(out, n, reference)
