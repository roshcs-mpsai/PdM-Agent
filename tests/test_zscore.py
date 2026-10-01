"""z-score scorer (R0.4): fit on a healthy candidate, one score per window.

The fit is checked against an independent pandas computation of the same
one-minute means. Nothing here names a MetroPT signal: inputs come from the
profile. The real-data test runs only when the CSV is present (not in CI).
"""
import datetime as dt
import json
import math
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "detector"))

import zscore  # noqa: E402
from pdm_common.events import load_events  # noqa: E402
from pdm_common.profile import load_profile, profile_hash, signal_names  # noqa: E402

PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
DATA = ROOT / "data" / "MetroPT3(AirCompressor).csv"


@pytest.fixture(scope="module")
def profile():
    return load_profile(PROFILE_PATH)


def synthetic_csv(path, profile, start, minutes):
    """Rows every 10 s; signal i wobbles around 10 * i, deterministically."""
    names = signal_names(profile)
    rows = []
    for k in range(minutes * 6):
        ts = start + dt.timedelta(seconds=10 * k)
        values = [10 * i + math.sin(k / 7 + i) * (1 + i % 3) for i in range(len(names))]
        rows.append([k, ts.isoformat(sep=" ")] + values)
    df = pd.DataFrame(rows, columns=["", "timestamp", *names])
    df.to_csv(path, index=False)
    return df


EVENTS = {
    "healthy_candidates": [{
        "id": "HX", "start": dt.date(2020, 3, 21), "end": dt.date(2020, 3, 21), "days": 1,
        "exclude": [{"start": "2020-03-21 00:10", "end": "2020-03-21 00:11:59"}],
    }],
    "data_quality_masks": [], "uncertain_periods": [], "grey_periods": [],
}


@pytest.fixture()
def fitted(tmp_path, profile):
    path = tmp_path / "synthetic.csv"
    # 30 minutes inside the candidate day, then 10 minutes on the next day
    df = pd.concat([synthetic_csv(path, profile, dt.datetime(2020, 3, 21, 0, 0), 30)])
    tail = synthetic_csv(tmp_path / "tail.csv", profile, dt.datetime(2020, 3, 22, 0, 0), 10)
    pd.concat([df, tail]).to_csv(path, index=False)
    return zscore.fit(profile, EVENTS, path, "HX", threshold=3.0), df


def test_fit_matches_an_independent_computation(profile, fitted):
    model, df = fitted
    inputs = profile["detector"]["inputs"]
    t = pd.to_datetime(df["timestamp"])
    keep = ~((t >= "2020-03-21 00:10") & (t <= "2020-03-21 00:11:59"))
    means = df[keep].assign(minute=t[keep].dt.floor("1min")).groupby("minute")[inputs].mean()

    assert model["fit"]["windows"] == len(means) == 28        # 30 minutes less the excluded 2
    assert model["fit"]["rows_excluded"] == 12
    assert model["fit"]["rows"] == 168                         # the next day is not read
    for name in inputs:
        assert model["channels"][name]["mean"] == pytest.approx(means[name].mean(), rel=1e-12)
        assert model["channels"][name]["std"] == pytest.approx(means[name].std(), rel=1e-9)
    assert model["threshold"] == 3.0


def test_fit_refuses_an_unknown_candidate(profile, tmp_path):
    with pytest.raises(zscore.FitError, match="no healthy candidate 'H9'"):
        zscore.fit(profile, EVENTS, tmp_path / "unused.csv", "H9")


def window(profile, minute, features):
    start = dt.datetime(2020, 4, 17, 0, minute, tzinfo=dt.timezone.utc)
    return json.dumps({
        "schema": "window_v1", "asset": profile["asset"]["id"], "profile_sha256": "x",
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": (start + dt.timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "n": 6, "features": features,
    })


def test_every_window_gets_exactly_one_score(profile, fitted):
    model, _ = fitted
    scorer = zscore.Scorer(profile, model, profile_hash(PROFILE_PATH), "e" * 64)
    channels = model["channels"]
    outs = []
    for k in range(20):
        # walk one channel away from its mean, k/4 standard deviations
        features = {n: c["mean"] for n, c in channels.items()}
        first = next(iter(channels))
        features[first] += channels[first]["std"] * k / 4
        outs.append(scorer.handle(window(profile, k, features)))

    assert all(len(o) == 2 for o in outs)
    scores = [json.loads(o[0].payload) for o in outs]
    status = [json.loads(o[1].payload) for o in outs]
    assert all(o[0].topic == profile["topics"]["scores"] and not o[0].retain for o in outs)
    assert all(o[1].topic == profile["topics"]["status"] and o[1].retain for o in outs)
    assert scorer.counts["windows"] == scorer.counts["scored"] == 20

    for k, (s, st) in enumerate(zip(scores, status)):
        assert s["schema"] == "score_v1" and st["schema"] == "status_v1"
        assert s["score"] == pytest.approx(k / 4)
        assert s["score"] == max(s["errors"].values())
        assert s["alert"] is (k / 4 > 3.0)
        assert st["ne107"] == ("out_of_specification" if s["alert"] else "good")
        assert st["ts"] == s["window_end"]
        assert s["detector_run"] == st["detector_run"] == zscore.model_id(model)
        assert s["profile_sha256"] == profile_hash(PROFILE_PATH)
    assert scorer.counts["alerts"] == sum(k / 4 > 3.0 for k in range(20))


def test_bad_windows_are_dropped_and_counted(profile, fitted):
    model, _ = fitted
    scorer = zscore.Scorer(profile, model, "p" * 64)
    good = {n: c["mean"] for n, c in model["channels"].items()}
    missing = dict(good)
    missing.pop(next(iter(missing)))
    other = json.loads(window(profile, 0, good))
    other["asset"] = "another_asset"
    for payload in (window(profile, 0, missing), json.dumps(other), b"not json"):
        assert scorer.handle(payload) == []
    assert scorer.handle(window(profile, 1, good)) != []
    assert dict(scorer.counts) == {"windows": 4, "scored": 1, "dropped": 3, "alerts": 0}


def test_scorer_refuses_another_assets_model(profile, fitted):
    model, _ = fitted
    with pytest.raises(ValueError, match="model is for asset"):
        zscore.Scorer(profile, dict(model, asset="another_asset"), "p" * 64)


@pytest.mark.skipif(not DATA.exists(), reason="needs the MetroPT-3 CSV under data/")
def test_real_fit_on_h4(profile):
    """H4 (Mar 21-23): 25,688 rows, 4,245 one-minute windows, nothing excluded."""
    events = load_events(PROFILE_PATH.parent / "events.yaml")
    model = zscore.fit(profile, events, DATA, "H4")
    assert model["fit"]["rows"] == 25_688
    assert model["fit"]["rows_excluded"] == 0
    assert model["fit"]["windows"] == 4_245

    inputs = profile["detector"]["inputs"]
    df = pd.read_csv(DATA, usecols=["timestamp", *inputs], float_precision="round_trip")
    t = pd.to_datetime(df["timestamp"])
    h4 = df[(t >= "2020-03-21") & (t < "2020-03-24")].assign(minute=t.dt.floor("1min"))
    means = h4.groupby("minute")[inputs].mean()
    for name in inputs:
        assert model["channels"][name]["mean"] == pytest.approx(means[name].mean(), rel=1e-9)
        assert model["channels"][name]["std"] == pytest.approx(means[name].std(), rel=1e-9)
