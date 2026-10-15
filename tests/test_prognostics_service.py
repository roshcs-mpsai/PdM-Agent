"""Prognostics service (T13, T14; PRG-01 to PRG-06): one prognosis per stream hour.

The data test streams windows and frozen-detector scores through the service
and holds every hourly row and probability to eval/prognostics.py's
hourly_table and the frozen bundle on the same hours.
"""
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "prognostics"))
sys.path.insert(0, str(ROOT / "eval"))

import prognosis as prognostics_service  # noqa: E402
from pdm_common.frozen import FrozenError  # noqa: E402
from pdm_common.hashing import canonical_json  # noqa: E402
from pdm_common.profile import load_profile  # noqa: E402
from pdm_common.store import Store  # noqa: E402

PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
PROCESSED = ROOT / "data" / "processed_v4"
T0 = dt.datetime(2020, 6, 1, tzinfo=dt.timezone.utc)


@pytest.fixture(scope="module")
def profile():
    return load_profile(PROFILE_PATH)


@pytest.fixture(scope="module")
def frozen(profile):
    return prognostics_service.load_for_profile(profile, PROFILE_PATH)


def z(t):
    return t.isoformat().replace("+00:00", "Z")


def window(service, end, **features):
    return canonical_json({"schema": "window_v1", "asset": service.asset, "end": z(end),
                           "start": z(end - dt.timedelta(minutes=10)),
                           "features": dict({"load_share_6h": 0.15, "rest_share_6h": 0.3, "oil_rise_1h": 0.5},
                                            **features)})


def score(service, end, value):
    return canonical_json({"schema": "score_v1", "asset": service.asset, "window_end": z(end), "score": value,
                           "alert": value > service.threshold})


def run(service, minutes, value=lambda m: 1.0, score_first=False):
    outs = []
    for m in range(1, minutes + 1):
        end = T0 + dt.timedelta(minutes=m)
        pair = [service.handle_score(score(service, end, value(m))), service.handle_window(window(service, end))] \
            if score_first else [service.handle_window(window(service, end)), service.handle_score(score(service, end, value(m)))]
        for o in pair:
            outs += o
    return [json.loads(o.payload) for o in outs]


def test_one_prognosis_per_stream_hour(profile, frozen):
    store = Store("sqlite://")
    store.init_schema()
    service = prognostics_service.Prognostics(profile, frozen, "p" * 64, store)
    docs = run(service, 30 * 60)
    assert [d["ts"] for d in docs] == [z(T0 + dt.timedelta(hours=h)) for h in range(1, 31)]
    last = docs[-1]
    assert last["schema"] == "prognosis_v1" and last["windows_in_history"] == 24 * 60
    for h, v in last["horizons"].items():
        assert 0 <= v["lower"] <= v["p"] <= v["upper"] <= 1
    ps = [last["horizons"][h]["p"] for h in ("24", "72", "168")]
    assert ps == sorted(ps)                                  # monotone in the horizon
    assert last["skill"]["meets_bar"] is False and last["skill"]["bar"] == 0.15
    assert {a["input"] for a in last["attribution"]} == set(frozen.survival.features)
    assert last["model_run_id"] == frozen.survival_run_id
    assert store.count("prognoses") == 30
    assert store.select("prognoses", order="ts")[-1]["probabilities"] == last["horizons"]


def test_window_and_score_may_arrive_in_either_order(profile, frozen):
    a = run(prognostics_service.Prognostics(profile, frozen, "p" * 64), 120)
    b = run(prognostics_service.Prognostics(profile, frozen, "p" * 64), 120, score_first=True)
    assert a == b and len(a) == 2


def test_alarms_raise_the_probability(profile, frozen):
    quiet = run(prognostics_service.Prognostics(profile, frozen, "p" * 64), 25 * 60)[-1]
    service = prognostics_service.Prognostics(profile, frozen, "p" * 64)
    loud = run(service, 25 * 60, value=lambda m: 50.0 if m > 20 * 60 else 1.0)[-1]
    assert loud["inputs"]["alert_share_24h"] > 0.1
    assert loud["horizons"]["72"]["p"] > quiet["horizons"]["72"]["p"]


def test_a_profile_asking_for_other_horizons_is_refused(profile, frozen):
    other = dict(profile, prognosis=dict(profile["prognosis"], horizons=["12h", "48h"]))
    with pytest.raises(FrozenError, match="horizons"):
        prognostics_service.Prognostics(other, frozen, "p" * 64)


@pytest.mark.skipif(not (PROCESSED / "windows_10min.csv.gz").exists(), reason="needs data/processed_v4")
def test_stream_rows_equal_the_hourly_table(profile, frozen):
    import prognostics as batch                       # eval/prognostics.py
    from pdm_common.events import load_events
    from pdm_common.profile import events_file
    windows = pd.read_csv(PROCESSED / "windows_10min.csv.gz", parse_dates=["window_start", "window_end"])
    scores = frozen.detector.score(windows[frozen.detector.features].to_numpy(float))
    threshold = float(frozen.survival_bundle["threshold_used"])
    table = batch.hourly_table(windows, scores, threshold, load_events(events_file(profile, PROFILE_PATH)), [],
                               windows["window_end"].max())
    service = prognostics_service.Prognostics(profile, frozen, "p" * 64)
    sl = windows[(windows["window_end"] > "2020-05-20") & (windows["window_end"] <= "2020-06-10")]
    docs = []
    for (_, w), sc in zip(sl.iterrows(), scores[sl.index]):
        end = w["window_end"].tz_localize("UTC").to_pydatetime()
        feats = {k: (None if pd.isna(w[k]) else float(w[k])) for k in ("load_share_6h", "rest_share_6h", "oil_rise_1h")}
        for out in service.handle_window(canonical_json({"schema": "window_v1", "asset": service.asset,
                                                         "end": z(end), "features": feats})):
            docs.append(json.loads(out.payload))
        for out in service.handle_score(score(service, end, float(sc))):
            docs.append(json.loads(out.payload))
    stream = {d["ts"]: d for d in docs}
    ref = table[(table["hour"] > "2020-05-21") & (table["hour"] <= "2020-06-10")]   # a full day of history
    assert len(ref) > 250          # at-risk hours with a complete window on the hour
    for _, r in ref.iterrows():
        d = stream[z(r["hour"].tz_localize("UTC").to_pydatetime())]
        for k in batch.INPUTS:
            want = r[k]
            got = d["inputs"][k]
            # the batch table differences a campaign-long cumulative sum, so its means carry ~1e-8 rounding
            assert (got is None and pd.isna(want)) or got == pytest.approx(want, rel=1e-6, abs=1e-9), (r["hour"], k)
        expected = batch.predict_bundle(frozen.survival_bundle, r.to_dict())
        for h in ("24", "72", "168"):
            assert d["horizons"][h]["p"] == pytest.approx(expected[h]["p"], rel=1e-6)
