"""The persistence gate (T12a; INC-01 to INC-04): the eight checks from the
detector-selection notebook's Section 2, now against pdm_common.gate."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pdm_common.gate import GateParams, gate, gate_params, gate_passes, incidents, lead_at_report, ns
from pdm_common.profile import load_profile

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"


@pytest.fixture(scope="module")
def params():
    return gate_params(load_profile(PROFILE_PATH))


def opened(alerting, params, length=12 * 60):
    """Minutes (from the start) at which incidents open on a one-minute series."""
    end = ns(pd.date_range("2020-03-01", periods=length, freq="1min"))
    alerts = np.zeros(length, dtype=bool)
    for a, b in alerting:
        alerts[a:b] = True
    opens, _, _ = gate(end, alerts, params)
    return [int((o - end[0]) // (60 * 10**9)) for o in opens]


def test_params_come_from_the_profile(params):
    assert params == GateParams(span_ns=30 * 60 * 10**9, min_fraction=0.8,
                                hold_off_ns=6 * 3600 * 10**9, min_present=15)


def test_a_one_window_spike_opens_nothing(params):
    assert opened([(100, 101)], params) == []


def test_a_40_minute_excursion_opens_one_incident_23_minutes_in(params):
    assert opened([(100, 140)], params) == [123]


def test_a_second_excursion_an_hour_later_joins_the_first(params):
    assert len(opened([(100, 140), (200, 240)], params)) == 1


def test_one_seven_hours_later_opens_a_second_incident(params):
    assert len(opened([(100, 140), (600, 640)], params)) == 2


def test_a_lone_window_after_a_gap_opens_nothing(params):
    after_gap = ns(np.r_[pd.date_range("2020-03-01", periods=30, freq="1min").to_numpy(),
                         np.array(["2020-03-01T05:00"], dtype="datetime64[ns]")])
    passes = gate_passes(after_gap, np.r_[np.zeros(30, bool), True],
                         params.span_ns, params.min_fraction, params.min_present)
    assert not passes.any()


def test_lead_at_report_cases():
    hour = ns(pd.date_range("2020-03-01", periods=10, freq="1h"))
    assert lead_at_report(hour[[1]], hour[[3]], hour[2], hour[4]) == 1.0     # open when the report starts
    assert lead_at_report(hour[[3]], hour[[6]], hour[2], hour[4]) == -1.0    # opens an hour into the report
    assert np.isnan(lead_at_report(hour[[1]], hour[[2]], hour[3], hour[5]))  # closed before: missed


def test_incidents_close_hold_off_after_their_last_pass(params):
    end = ns(pd.date_range("2020-03-01", periods=5, freq="1min"))
    opens, last, closes = incidents(end, np.array([False, True, True, False, True]), params.hold_off_ns)
    assert list(opens) == [end[1]] and list(last) == [end[4]]
    assert closes[0] == end[4] + params.hold_off_ns
    empty = incidents(end, np.zeros(5, bool), params.hold_off_ns)
    assert all(len(x) == 0 for x in empty)


def test_ns_reads_aware_and_naive_times_alike():
    naive = pd.date_range("2020-03-01", periods=3, freq="1min")
    aware = naive.tz_localize("UTC")
    assert (ns(naive) == ns(aware)).all()
    assert (ns(naive.to_numpy()) == ns(pd.Series(naive))).all()
