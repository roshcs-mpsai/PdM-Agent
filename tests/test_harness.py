"""The evaluation harness (T27; EVL-01 to EVL-04).

Synthetic tests pin the threshold rule, the lead definition and one
end-to-end run on a planted excursion; they run in CI. The golden test
reproduces the Oct 4 selection.json from data/processed_v4 when it is there.
"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

import harness as h  # noqa: E402
from pdm_common.gate import GateParams, ns  # noqa: E402

PROCESSED = ROOT / "data" / "processed_v4"
GOLDEN = PROCESSED / "detector_selection" / "selection.json"
GATE = GateParams(span_ns=30 * 60 * 10**9, min_fraction=0.8, hold_off_ns=6 * 3600 * 10**9, min_present=15)


def test_pick_threshold_keeps_the_lowest_threshold_with_every_higher_one_in_budget():
    # thresholds ascend, so rates fall; one dip back under budget does not count
    assert h.pick_threshold(np.array([5.0, 3.0, 1.9, 2.5, 1.0, 0.5])) == 4
    assert h.pick_threshold(np.array([1.0, 0.5])) == 0           # the budget never bound
    assert h.pick_threshold(np.array([5.0, 4.0, 3.0])) == 2      # never met: the highest
    assert h.pick_threshold(np.array([3.0, 2.0, 1.0]), budget=2.0) == 1   # at the budget counts


def test_threshold_grid_spans_the_upper_quantiles_and_one_above_the_maximum():
    scores = np.arange(1000, dtype=float)
    grid = h.threshold_grid(scores)
    assert grid[0] == pytest.approx(np.quantile(scores, 0.5))
    assert grid[-1] > scores.max()
    assert (np.diff(grid) > 0).all()


def synthetic_context(minutes=3 * 1440, planted=(1517, 1637), report=1600):
    """Three days of one-minute windows: day 1 calibrate, day 2 the test block of
    one event reported at minute 1600, day 3 check; quiet apart from healthy noise
    on day 1 and a two-hour excursion planted from minute 1517."""
    t = pd.date_range("2021-03-01", periods=minutes, freq="1min")
    end = ns(t + pd.Timedelta(minutes=10))
    is_cal = np.zeros(minutes, bool)
    is_cal[:1440] = True
    is_check = np.zeros(minutes, bool)
    is_check[2880:] = True
    block = np.zeros(minutes, bool)
    block[1440:2880] = True
    report_start = pd.Timestamp(t[report] + pd.Timedelta(minutes=10))
    ctx = h.Context(end_ns=end, is_cal=is_cal, is_check=is_check, period_of=np.full(minutes, -1),
                    uncertain=[], failures={"F1": (report_start, report_start + pd.Timedelta(hours=1))},
                    block={"F1": block}, start_ns={"F1": ns([report_start])[0]},
                    stop_ns={"F1": ns([report_start + pd.Timedelta(hours=1)])[0]},
                    during={"F1": np.zeros(minutes, bool)}, before={"F1": np.zeros(minutes, bool)}, gate=GATE)
    rng = np.random.default_rng(1)
    scores = np.full(minutes, -5.0)
    scores[:1440] = rng.normal(0, 1, 1440)          # healthy noise sets the threshold
    scores[planted[0]:planted[1]] = 10.0
    return ctx, scores


def test_a_planted_two_hour_excursion_yields_a_one_hour_lead():
    """The excursion starts 83 min before the report; the gate opens 23 min into
    it, so the incident is open an hour before the report starts."""
    ctx, scores = synthetic_context()
    r = h.evaluate(scores, ctx, n_boot=50)
    assert r["F1 lead"] == pytest.approx(1.0)
    assert r["far"] == 0.0 and r["F1 caught share"] == 1.0
    assert r["calibrate incidents"] == 0


def test_a_given_threshold_is_used_as_is():
    ctx, scores = synthetic_context()
    r = h.evaluate(scores, ctx, n_boot=20, threshold=100.0)
    assert r["threshold"] == 100.0 and r["threshold kind"] == "given"
    assert np.isnan(r["F1 lead"])                      # nothing crosses 100: the event is missed


def test_selection_rule_trace():
    results = {name: {"far": far, "far lo": lo, **{f"{ev} lead median": lead for ev, lead in leads.items()}}
               for name, far, lo, leads in [
                   ("z-score", 1.3, 0.5, {"F1": -0.8, "F2": 14.0}),
                   ("PCA", 1.2, 0.5, {"F1": -0.8, "F2": 13.8}),
                   ("Deep", 1.0, 0.4, {"F1": -0.8, "F2": 13.9}),
                   ("Noisy", 3.0, 2.5, {"F1": 5.0, "F2": 20.0})]}
    info = {"z-score": {"family": "baseline", "attribution": True, "multivariate": False, "complexity": 0},
            "PCA": {"family": "baseline", "attribution": True, "multivariate": True, "complexity": 1},
            "Deep": {"family": "deep, features", "attribution": True, "multivariate": True, "complexity": 3},
            "Noisy": {"family": "classical", "attribution": False, "multivariate": True, "complexity": 2}}
    rule = h.select(results, info, ["F1", "F2"])
    assert rule["eligible"] == ["z-score", "PCA", "Deep"]          # Noisy's interval misses the budget
    assert rule["deep_dropped_evl05"] == ["Deep"]                  # no hour gained anywhere
    assert rule["selected"] == "PCA"                               # attribution, then multivariate


def test_roles_leave_out_windows_that_read_a_test_block():
    w = pd.DataFrame({"window_start": pd.date_range("2021-03-01", periods=4, freq="1min"),
                      "zone": ["negative", "negative", "grey", "negative"],
                      "neg_role": ["fit", "calibrate", "none", "check"],
                      "lookback_F1": [False, True, False, False]})
    assert list(h.assign_roles(w)) == ["fit", "other", "other", "check"]


def test_without_data_the_command_line_prints_the_fold_plan():
    out = subprocess.run([sys.executable, str(ROOT / "eval" / "evaluate.py")], capture_output=True, text=True)
    assert out.returncode == 0
    assert out.stdout.count("fold holds out") == 4
    assert "[paired with F2]" in out.stdout


@pytest.mark.skipif(not GOLDEN.exists(), reason="needs data/processed_v4 with detector_selection/selection.json")
def test_the_harness_reproduces_the_oct_4_selection(tmp_path):
    """T27 done-check: PCA's threshold, held-out FAR with its interval and the
    leads at the threshold and over the re-draws, to the notebook's digits."""
    out = subprocess.run([sys.executable, str(ROOT / "eval" / "evaluate.py"), "--processed", str(PROCESSED),
                          "--detector", "pca", "--golden", str(GOLDEN), "--out", str(tmp_path)],
                         capture_output=True, text=True, timeout=600)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "golden check passed" in out.stdout
    record = json.loads((tmp_path / "pca" / "result.json").read_text())
    golden = json.loads(GOLDEN.read_text())
    assert record["held_out_far"] == golden["held_out_far"]
    assert record["lead_h"] == golden["lead_h"]
    assert record["inputs"]["windows_key"] == golden["inputs"]["windows_key"]
    assert record["inputs"]["fit_key"] == golden["inputs"]["fit_key"]
