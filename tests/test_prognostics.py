"""Survival prognostics (T13, T14; PRG-01 to PRG-06, EVL-06).

Synthetic tests pin the hourly table, the censoring rules, the constant
hazard, the monotone horizons and the bundle's intervals and attributions;
the data test runs the leave-one-event-out report on data/processed_v4.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

import prognostics as pg  # noqa: E402

PROCESSED = ROOT / "data" / "processed_v4"


def toy_events():
    return {"failures": [{"id": "F1", "start": "2021-03-08 12:00", "end": "2021-03-08 18:00",
                          "maintenance": "2021-03-09 00:00"}],
            "campaign": ["2021-03-01", "2021-03-10"], "data_quality_masks": []}


def toy_windows(days=10, gap=None):
    """One complete window per minute for ten days, a flat score, optional recording gap."""
    end = pd.date_range("2021-03-01 00:10", periods=days * 1440, freq="1min")
    if gap is not None:
        end = end[(end < gap[0]) | (end >= gap[1])]
    w = pd.DataFrame({"window_end": end, "load_share_6h": 0.1, "rest_share_6h": 0.6, "oil_rise_1h": 0.0})
    return w


def test_the_hourly_table_labels_time_to_the_next_failure():
    w = toy_windows()
    scores = np.ones(len(w))
    table = pg.hourly_table(w, scores, threshold=0.5, events=toy_events(), outages=[],
                            campaign_end=pd.Timestamp("2021-03-11"))
    first = table.iloc[0]
    assert first["hour"] == pd.Timestamp("2021-03-01 01:00")
    assert first["time_h"] == pytest.approx(7 * 24 + 11)
    assert first["event"] and first["next_failure"] == "F1"
    assert first["alert_share_24h"] == 1.0 and first["score_max_24h"] == 1.0
    # rows from the failure start to the repair are not at risk
    hours = pd.DatetimeIndex(table["hour"])
    assert not ((hours >= pd.Timestamp("2021-03-08 12:00")) & (hours < pd.Timestamp("2021-03-09"))).any()
    # after the repair there is no next failure: censored at the campaign end
    after = table[hours >= pd.Timestamp("2021-03-09")]
    assert (~after["event"]).all() and (after["censor"] == "campaign_end").all()


def test_a_long_outage_censors_but_an_overnight_stop_does_not():
    gap = (pd.Timestamp("2021-03-03 20:00"), pd.Timestamp("2021-03-04 05:00"))          # 9 h: overnight
    w = toy_windows(gap=gap)
    outage = [(pd.Timestamp("2021-03-05 00:00"), pd.Timestamp("2021-03-05 14:00"))]       # 14 h: censors
    table = pg.hourly_table(w, np.zeros(len(w)), 0.5, toy_events(), outage, pd.Timestamp("2021-03-11"))
    row = table[table["hour"] == pd.Timestamp("2021-03-03 12:00")].iloc[0]
    assert not row["event"] and row["censor"] == "long_outage"
    assert row["time_h"] == pytest.approx(36)
    later = table[table["hour"] == pd.Timestamp("2021-03-06 12:00")].iloc[0]
    assert later["event"] and later["time_h"] == pytest.approx(48)


def test_labels_drop_rows_whose_outcome_is_unknown():
    t = pd.DataFrame({"time_h": [10.0, 30.0, 30.0, 100.0], "event": [True, True, False, False]})
    y, known = pg.labels(t, 24)
    assert list(y) == [True, False, False, False] and list(known) == [True, True, True, True]
    y, known = pg.labels(t, 72)
    assert list(y) == [True, True, False, False] and list(known) == [True, True, False, True]


def test_constant_hazard_recovers_its_rate():
    """A ten-day series with one planted failure: one failure in 240 hours at risk."""
    w = toy_windows()
    table = pg.hourly_table(w, np.zeros(len(w)), 0.5, toy_events(), [], pd.Timestamp("2021-03-11"))
    lam = pg.constant_hazard(table)
    assert lam == pytest.approx(1 / len(table))
    assert lam == pytest.approx(1 / 228, rel=0.2)
    assert pg.hazard_probability(lam, 24) == pytest.approx(1 - np.exp(-24 * lam))


def synthetic_table(n=600, seed=0):
    """Rows where a rising alert share precedes failures: a model should beat the base rate."""
    rng = np.random.default_rng(seed)
    t = rng.uniform(1, 400, n)
    share = np.clip(1 - t / 400 + rng.normal(0, 0.05, n), 0, 1)
    return pd.DataFrame({"hour": pd.date_range("2021-01-01", periods=n, freq="1h"),
                         "score_max_24h": share * 10, "score_mean_24h": share, "alert_share_24h": share,
                         "load_share_6h": rng.uniform(0, 0.3, n), "rest_share_6h": rng.uniform(0.4, 0.8, n),
                         "oil_rise_1h": rng.normal(0, 1, n), "time_h": t, "event": True, "next_failure": "F1"})


def test_horizon_probabilities_are_monotone_and_learn_a_signal():
    table = synthetic_table()
    model = pg.HorizonModel("logistic").fit(table)
    p = model.predict(table[pg.INPUTS].to_numpy(float))
    assert p.shape == (len(table), 3)
    assert (np.diff(p, axis=1) >= 0).all()
    y, _ = pg.labels(table, 72)
    assert pg.brier(y, p[:, 1]) < pg.brier(y, np.full(len(y), y.mean()))


def test_the_bundle_carries_intervals_and_exact_attributions():
    table = synthetic_table()
    bundle = pg.fit_bundle(table, "logistic", n_boot=20)
    row = table.iloc[10][pg.INPUTS].to_dict()
    out = pg.predict_bundle(bundle, row)
    assert set(out) == {"24", "72", "168"}
    for h, v in out.items():
        assert v["lower"] <= v["p"] <= v["upper"]
    assert out["24"]["p"] <= out["72"]["p"] <= out["168"]["p"]
    contrib = pg.attribution(bundle, row, horizon=72)
    m = bundle["main"]["horizons"]["72"]
    prep = bundle["main"]["prep"]
    x = np.array([row[k] for k in bundle["features"]])
    z = (x - np.array(prep["mean"])) / np.array(prep["std"])
    assert sum(c["log_odds"] for c in contrib) == pytest.approx(float(np.dot(m["coef"], z)))
    # a missing input takes the training median, never fails
    assert pg.predict_bundle(bundle, {**row, "load_share_6h": None})["72"]["p"] > 0


@pytest.mark.skipif(not (PROCESSED / "grid_10s.csv.gz").exists(), reason="needs data/processed_v4")
def test_leave_one_event_out_report_on_metropt3(tmp_path):
    """T13 done-check shape: Brier skill per horizon with intervals, per event and
    pooled beside n = 4, whether or not the 0.15 bar is met."""
    table, data, events, outages, threshold = pg.build_table(PROCESSED, ROOT / "profiles/metropt3_apu/asset_profile.yaml",
                                                             None, None)
    assert table["next_failure"].dropna().nunique() == 4
    assert len(outages) == 15
    pred = pg.loeo(table, pg.blocks_from_manifest(data.manifest), "logistic")
    report = pg.score_predictions(pred)
    for h in pg.HORIZONS:
        pooled = report[h]["pooled"]
        assert pooled["n_events"] == 4
        lo, hi = pooled["skill_interval_90"]
        assert np.isfinite(pooled["skill"]) and lo <= hi
        assert set(report[h]["per_event"]) == {"F1", "F2", "F3", "F4"}
