"""SPOT threshold (T11; DET-05): the GPD tail fit, the threshold formula and
the choice of q at the false-alarm budget."""
import math

import numpy as np
import pandas as pd
import pytest
from scipy.stats import genpareto

from pdm_common.gate import GateParams, ns
from pdm_common.spot import SpotFit, calibrate_q, fit_spot, threshold

GATE = GateParams(span_ns=30 * 60 * 10**9, min_fraction=0.8, hold_off_ns=6 * 3600 * 10**9, min_present=15)


def test_a_known_tail_is_recovered():
    rng = np.random.default_rng(0)
    body = rng.uniform(0, 3.0, 245_000)
    tail = genpareto.rvs(c=0.2, scale=1.0, size=5000, random_state=rng) + 3.0
    fit = fit_spot(np.r_[body, tail], init_quantile=0.98)
    assert fit.t == pytest.approx(3.0, abs=0.01)
    assert fit.gamma == pytest.approx(0.2, abs=0.1)
    assert fit.sigma == pytest.approx(1.0, rel=0.15)


def test_the_threshold_reproduces_from_its_saved_parameters():
    saved = {"t": 5.2553, "gamma": 1.465, "sigma": 2.7168, "n": 32915, "n_t": 330, "init_quantile": 0.99}
    fit = SpotFit(**saved)
    q = 1e-3
    r = q * saved["n"] / saved["n_t"]
    expected = saved["t"] + saved["sigma"] / saved["gamma"] * (r ** (-saved["gamma"]) - 1)
    assert threshold(fit, q) == pytest.approx(expected)
    assert threshold(fit, 1e-3) == threshold(SpotFit(**fit.as_dict()), 1e-3)


def test_near_zero_gamma_takes_the_exponential_limit():
    fit = SpotFit(t=2.0, gamma=1e-9, sigma=0.5, n=10_000, n_t=200, init_quantile=0.98)
    q = 1e-4
    assert threshold(fit, q) == pytest.approx(2.0 - 0.5 * math.log(q * 10_000 / 200))
    nearly = SpotFit(t=2.0, gamma=1e-4, sigma=0.5, n=10_000, n_t=200, init_quantile=0.98)
    assert threshold(nearly, q) == pytest.approx(threshold(fit, q), rel=1e-3)


def test_smaller_q_means_a_higher_threshold():
    fit = SpotFit(t=1.0, gamma=0.3, sigma=0.4, n=10_000, n_t=200, init_quantile=0.98)
    taus = [threshold(fit, q) for q in (1e-2, 1e-3, 1e-4, 1e-5)]
    assert taus == sorted(taus)


def test_calibrate_q_keeps_the_largest_q_within_budget():
    """Two weeks of one-minute scores with sustained bumps of rising height: a
    higher threshold opens fewer incidents, so the scan stops where the budget
    first holds for every smaller q."""
    rng = np.random.default_rng(3)
    n = 2 * 7 * 1440
    scores = rng.exponential(1.0, n)
    for k, height in enumerate(np.linspace(6, 60, 12)):          # 12 bumps of 40 min
        start = 600 + k * 1600
        scores[start:start + 40] = height
    end = ns(pd.date_range("2021-03-01", periods=n, freq="1min"))
    fit = fit_spot(scores, init_quantile=0.98)
    out = calibrate_q(fit, end, scores, GATE, budget=2.0)
    rates = [row["rate"] for row in out["scan"]]
    assert out["calibrate_rate"] <= 2.0 and out["within_budget"]
    picked = [row["q"] for row in out["scan"]].index(out["q"])
    assert all(r <= 2.0 for r in rates[picked:])
    if picked > 0:
        assert rates[picked - 1] > 2.0


def test_too_few_tail_scores_is_an_error():
    with pytest.raises(ValueError, match="SPOT needs more"):
        fit_spot(np.arange(100.0), init_quantile=0.95)


ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed_v4"


@pytest.mark.skipif(not (PROCESSED / "windows_10min.csv.gz").exists(), reason="needs data/processed_v4")
def test_spot_on_the_pca_calibrate_scores(tmp_path):
    """T11 done-check: the SPOT run keeps the held-out interval at or under two a
    week and still catches all four failures; both fits are logged when the first
    shape falls outside [0, 1]."""
    import json
    import subprocess
    import sys
    out = subprocess.run([sys.executable, str(ROOT / "eval" / "evaluate.py"), "--processed", str(PROCESSED),
                          "--detector", "pca", "--threshold", "spot", "--out", str(tmp_path)],
                         capture_output=True, text=True, timeout=600)
    assert out.returncode == 0, out.stderr
    record = json.loads((tmp_path / "pca_plus_spot" / "result.json").read_text())
    assert record["held_out_far"][2] <= 2.0
    assert all(v is not None for v in record["lead_h"].values())
    assert record["spot"]["freeze_rule"]["use_spot"] is True
    gammas = [f["gamma"] for f in record["spot"]["fits_tried"]]
    assert len(gammas) == 1 or not 0 <= gammas[0] <= 1
