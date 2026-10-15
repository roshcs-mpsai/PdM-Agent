"""The checks registry (T15b, T36; AGT-05): one pure function per check.

Synthetic grids pin each rule and run anywhere. The data tests hold the port
to the detector-selection notebook: the 41 evidence packets it sent to the
triage arms (every result and every measured number), its A1-A3 verdicts, the
thresholds recomputed from healthy data, and C9 never firing on 2020 data.
"""
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pdm_common.checks import Evidence, load_registry
from pdm_common.events import load_events
from pdm_common.profile import analog_names, digital_names, events_file, load_profile

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
PROCESSED = ROOT / "data" / "processed_v4"
SELECTION = PROCESSED / "detector_selection"
OPENS = pd.Timestamp("2021-03-02 12:00", tz="UTC")


@pytest.fixture(scope="module")
def profile():
    return load_profile(PROFILE_PATH)


@pytest.fixture(scope="module")
def events(profile):
    return load_events(events_file(profile, PROFILE_PATH))


@pytest.fixture(scope="module")
def registry(profile):
    return load_registry(profile, ROOT)


def grid(profile, hours=30.0, end=OPENS, motor=6.5, tp3=9.0):
    """A healthy-looking grid ending at ``end`` (exclusive): every point observed."""
    idx = pd.date_range(end - pd.Timedelta(hours=hours), end - pd.Timedelta(seconds=10), freq="10s")
    g = pd.DataFrame(index=idx)
    for name in analog_names(profile):
        g[name] = 1.0
    for name in digital_names(profile):
        g[name] = 0.0
    g["Motor_current"] = motor
    g["TP3"] = tp3
    g["Reservoirs"] = tp3
    g["observed"] = True
    g["bridged"] = False
    return g


def gap(g, start, hours):
    """Mark ``hours`` from ``start`` as missing (no data)."""
    sel = (g.index >= start) & (g.index < start + pd.Timedelta(hours=hours))
    g.loc[sel, "observed"] = False
    g.loc[sel, analog_names_of(g)] = np.nan
    return g


def analog_names_of(g):
    return [c for c in g.columns if c not in ("observed", "bridged")]


def run(registry, profile, events, g, window=None, ids=None, opens=OPENS):
    ev = Evidence(opens, g, window or {}, profile=profile, events=events)
    return {r.id: r for r in registry.run(ev, ids)}


def test_the_registry_holds_every_declared_check(registry, profile):
    assert registry.ids == [f"C{i}" for i in range(1, 12)]
    declared = {c["id"] for c in profile["consistency_checks"]}
    assert set(registry.ids) <= declared
    with pytest.raises(KeyError, match="C99"):
        registry.run(Evidence(OPENS, grid(profile), {}, profile=profile, events={}), ["C99"])


def test_motor_off_command(registry, profile, events):
    g = grid(profile)
    assert run(registry, profile, events, g, {"cmd_motor_off": 0}, ["C1"])["C1"].result == "pass"
    r = run(registry, profile, events, g, {"cmd_motor_off": 3}, ["C1"])["C1"]
    assert r.result == "fail" and r.measured.startswith("3 of 60 points") and r.kind == "data_quality"


def test_restart_counts_from_the_end_of_the_last_gap(registry, profile, events):
    recent = gap(grid(profile), OPENS - pd.Timedelta(hours=2, minutes=20), 2)   # resumed 20 min before
    r = run(registry, profile, events, recent, ids=["C2"])["C2"]
    assert r.result == "fail" and r.measured == "data resumed 20 min before the alarm, after 2.0 h without data"
    older = gap(grid(profile), OPENS - pd.Timedelta(hours=3, minutes=10), 2)    # resumed 70 min before
    assert run(registry, profile, events, older, ids=["C2"])["C2"].result == "pass"
    assert run(registry, profile, events, grid(profile), ids=["C2"])["C2"].measured.startswith("no gap of 30 min")
    short = gap(grid(profile), OPENS - pd.Timedelta(minutes=50), 0.4)           # 24 min: not a gap
    assert run(registry, profile, events, short, ids=["C2"])["C2"].result == "pass"


def test_bridged_points_behind_the_gate(registry, profile, events):
    g = grid(profile)
    g.loc[OPENS - pd.Timedelta(minutes=35), "bridged"] = True
    g.loc[OPENS - pd.Timedelta(minutes=35), "observed"] = False
    r = run(registry, profile, events, g, ids=["C3"])["C3"]
    assert r.result == "fail" and r.value == 1
    g2 = grid(profile)
    g2.loc[OPENS - pd.Timedelta(minutes=45), ["bridged", "observed"]] = [True, False]   # outside the 40 min
    assert run(registry, profile, events, g2, ids=["C3"])["C3"].result == "pass"


@pytest.mark.parametrize("cid, feature", [("C4", "hours_since_rest"), ("C5", "load_share_6h"),
                                          ("C6", "Oil_temperature_mean"), ("C7", "lps_minutes_24h")])
def test_feature_thresholds_from_the_profile(registry, profile, events, cid, feature):
    limit = next(c["threshold"] for c in profile["consistency_checks"] if c["id"] == cid)
    g = grid(profile)
    assert run(registry, profile, events, g, {feature: limit * 0.9}, [cid])[cid].result == "pass"
    r = run(registry, profile, events, g, {feature: limit * 1.1 + 0.01}, [cid])[cid]
    assert r.result == "fail" and r.threshold == limit and r.kind == "fault_signature"


def test_load_share_without_data_is_inconclusive(registry, profile, events):
    r = run(registry, profile, events, grid(profile), {"load_share_6h": None}, ["C5"])["C5"]
    assert r.result == "inconclusive"


def test_leak_down_across_a_recent_gap(registry, profile, events):
    def leaky(drop, ended_hours_ago):
        g = grid(profile, hours=30)
        start = OPENS - pd.Timedelta(hours=ended_hours_ago + 2)
        g.loc[g.index >= start + pd.Timedelta(hours=2), "TP3"] = 9.0 - drop
        g.loc[g.index >= start + pd.Timedelta(hours=2), "Reservoirs"] = 9.0 - drop
        return gap(g, start, 2)
    fast = run(registry, profile, events, leaky(8.0, 2), ids=["C8"])["C8"]     # about 4 bar/h
    limit = fast.threshold
    assert fast.value == pytest.approx(8.0 / (2 + 10 / 3600), rel=1e-3) and fast.result == ("fail" if fast.value > limit else "pass")
    slow = run(registry, profile, events, leaky(1.0, 2), ids=["C8"])["C8"]
    assert slow.result == "pass"
    late = run(registry, profile, events, leaky(8.0, 7), ids=["C8"])["C8"]
    assert late.result == "inconclusive"


def test_an_injected_tp3_drift_trips_the_divergence_check(registry, profile, events):
    """R2a.3: a TP3 drift of 0.3 bar for 2 h trips C9; matching sensors pass it."""
    g = grid(profile)
    assert run(registry, profile, events, g, ids=["C9"])["C9"].result == "pass"
    drifted = g.copy()
    span = drifted.index >= OPENS - pd.Timedelta(hours=2)
    ramp = np.linspace(0, 0.3, int(span.sum()))
    drifted.loc[span, "TP3"] += ramp
    r = run(registry, profile, events, drifted, ids=["C9"])["C9"]
    assert r.result == "fail" and r.kind == "instrument" and r.value == pytest.approx(0.3, abs=0.01)
    # apart for only 5 minutes: not sustained
    blip = g.copy()
    blip.loc[(blip.index >= OPENS - pd.Timedelta(minutes=20)) & (blip.index < OPENS - pd.Timedelta(minutes=15)), "TP3"] += 0.5
    assert run(registry, profile, events, blip, ids=["C9"])["C9"].result == "pass"


def test_polarity_audit_stays_inconclusive(registry, profile, events):
    r = run(registry, profile, events, grid(profile), ids=["C10"])["C10"]
    assert r.result == "inconclusive" and "Pressure_switch" in r.measured


def test_refill_rate_after_a_restart(registry, profile, events):
    def restart(rate_bar_per_min):
        g = grid(profile, motor=6.5)
        resume = OPENS - pd.Timedelta(minutes=20)
        after = g.index >= resume
        minutes = (g.index[after] - resume).total_seconds() / 60
        g.loc[after, "TP3"] = 6.0 + rate_bar_per_min * minutes
        g.loc[after, "Reservoirs"] = g.loc[after, "TP3"]
        return gap(g, resume - pd.Timedelta(hours=1), 1)
    fast = run(registry, profile, events, restart(0.5), ids=["C11"])["C11"]     # 7 min below cut-out
    assert fast.result == "pass" and fast.value == pytest.approx(0.5, rel=1e-6)
    slow = run(registry, profile, events, restart(0.05), ids=["C11"])["C11"]
    assert slow.result == "fail"
    assert run(registry, profile, events, grid(profile), ids=["C11"])["C11"].result == "inconclusive"


def test_nothing_at_or_after_the_opening_is_read(registry, profile, events):
    """As-of by construction: data from the opening on cannot change a result."""
    g = grid(profile, end=OPENS + pd.Timedelta(hours=3))
    clean = {k: v.result for k, v in run(registry, profile, events, g).items()}
    g.loc[g.index >= OPENS, ["observed"]] = False                 # a gap and wild values after the opening
    g.loc[g.index >= OPENS, "TP3"] = 0.0
    g.loc[g.index >= OPENS, "bridged"] = True
    assert {k: v.result for k, v in run(registry, profile, events, g).items()} == clean


def test_triage_rules_are_evidence_only(registry):
    from types import SimpleNamespace as R
    restart_only = [R(id="C2", result="fail"), R(id="C4", result="fail"), R(id="C11", result="pass")]
    t = registry.triage(restart_only)
    assert t["policy"] == "none" and t["acted_on"] is False
    assert {k: v["would_dismiss"] for k, v in t["rules"].items()} == {"A1": False, "A2": True, "A3": False, "A4": True}
    slow_refill = restart_only[:2] + [R(id="C11", result="fail")]
    assert registry.triage(slow_refill)["rules"]["A4"]["would_dismiss"] is False


# --- the port, against the notebook's outputs ---------------------------------------------------

needs_processed = pytest.mark.skipif(not (SELECTION / "triage_packets.jsonl").exists(),
                                     reason="needs data/processed_v4 with the detector-selection outputs")


@pytest.fixture(scope="module")
def processed(profile):
    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    import calibrate_checks
    g = calibrate_checks.read_grid(PROCESSED, profile)
    w = pd.read_csv(PROCESSED / "windows_10min.csv.gz", parse_dates=["window_end"]).set_index("window_end")
    return g, w


@needs_processed
def test_the_41_evidence_packets_are_reproduced(registry, profile, events, processed):
    g, w = processed
    numbers = re.compile(r"-?\d+(?:\.\d+)?")
    packets = [json.loads(line) for line in (SELECTION / "triage_packets.jsonl").read_text().splitlines()]
    verdicts = pd.read_csv(SELECTION / "triage_incidents.csv")
    assert len(packets) == len(verdicts) == 41
    for pk, (_, row) in zip(packets, verdicts.iterrows()):
        opens = pd.Timestamp(pk["opens"])
        sent = json.loads(pk["request"].split("\n", 1)[1])["checks"]
        ev = Evidence(opens, g, w.loc[opens].to_dict(), profile=profile, events=events)
        results = registry.run(ev)
        mine = {r.id: r for r in results}
        for c in sent:
            r = mine[c["id"]]
            assert r.result == c["result"], (pk["incident"], c["id"], r.measured, c["measured"])
            if c["id"] != "C10" and r.result != "inconclusive":
                assert numbers.findall(r.measured) == numbers.findall(c["measured"]), (pk["incident"], c["id"])
        rules = registry.triage(results)["rules"]
        for arm in ("A1", "A2", "A3"):
            assert rules[arm]["would_dismiss"] == (row[arm] == "dismiss"), (pk["incident"], arm)


@needs_processed
def test_thresholds_match_healthy_data(profile):
    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    import calibrate_checks
    computed = calibrate_checks.calibrate(PROCESSED, PROFILE_PATH)
    assert calibrate_checks.compare(computed, profile) == []


@needs_processed
def test_the_divergence_check_never_fires_on_2020_data(processed, profile):
    g, _ = processed
    limit = next(c["threshold"] for c in profile["consistency_checks"] if c["id"] == "C9")
    valid = (g["observed"] | g["bridged"]).to_numpy()
    apart = valid & (np.abs(g["TP3"].to_numpy() - g["Reservoirs"].to_numpy()) > limit)
    run10 = np.convolve(apart.astype(int), np.ones(60, int), mode="full")[:len(apart)] == 60
    assert not run10.any()
