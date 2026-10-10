"""Zone timeline (T7; CFG-05): every timestamp gets exactly one label, by the
precedence events.yaml declares, with spans read from the file.

Synthetic events keep these tests independent of the dataset.
"""
import numpy as np
import pandas as pd
import pytest

from pdm_common.zones import (
    gap_edges,
    label_grid,
    regime_of,
    uncertain_tags,
    zone_codes,
    zone_scheme,
)

T0 = pd.Timestamp("2021-03-01", tz="UTC")


def toy_events():
    return {
        "failures": [{"id": "F1", "start": "2021-03-10 00:00", "end": "2021-03-10 06:00",
                      "maintenance": "2021-03-10 12:00"}],
        "guard_band": {"before": "24h", "after": "12h"},
        "expert_band": {"before": "1h", "after": "6h"},
        "healthy_buffer": {"before_failure": "7d", "after_repair": "48h"},
        "gap_edges": {"after_gaps_of": "30min", "length": "15min"},
        "expert_reported": [{"n": 1, "start": "2021-03-05 10:00", "end": "2021-03-05 10:20"}],
        "grey_periods": [{"start": "2021-03-15 08:00", "end": "2021-03-15 09:00"}],
        "uncertain_periods": [{"start": "2021-03-16 00:00", "end": "2021-03-16 01:00", "tag": "load_held"},
                              {"start": "2021-03-17 00:00", "end": "2021-03-17 01:00", "tag": "probable_leak"}],
        "data_quality_masks": [{"start": "2021-03-09 23:00:00", "end": "2021-03-10 01:00:00", "kind": "logger_freeze"}],
        "regimes": [{"name": "a", "start": "2021-03-01 00:00", "end": "2021-03-12 00:00"},
                    {"name": "b", "start": "2021-03-12 00:00", "end": "2021-04-01 00:00"}],
    }


def minute_grid(days=31):
    return pd.date_range(T0, periods=days * 1440, freq="1min")


def detail_at(events, times, codes, when):
    scheme = zone_scheme(events)
    return scheme.detail[codes[times.get_loc(pd.Timestamp(when, tz="UTC"))]]


def test_scheme_follows_the_eda_precedence():
    scheme = zone_scheme(toy_events())
    assert scheme.detail == ("negative", "buffer", "gap_edge", "grey_period", "uncertain:load_held",
                             "uncertain:probable_leak", "expert_reported", "failure", "masked")
    assert scheme.zone_of[scheme.code["expert_reported"]] == "grey"
    assert scheme.zone_of[scheme.code["failure"]] == "positive"
    assert scheme.episode_codes() == (4, 7)
    assert uncertain_tags(toy_events()) == ["load_held", "probable_leak"]


def test_every_timestamp_gets_exactly_one_known_label():
    events, times = toy_events(), minute_grid()
    codes = zone_codes(times, events, edges=[])
    scheme = zone_scheme(events)
    assert len(codes) == len(times)
    assert set(np.unique(codes)) <= set(range(len(scheme.detail)))


def test_precedence_where_spans_overlap():
    events, times = toy_events(), minute_grid()
    codes = zone_codes(times, events, edges=[])
    # the freeze mask wins over the guard band it sits in
    assert detail_at(events, times, codes, "2021-03-10 00:30") == "masked"
    # the guard band starts 24 h before the report, inside the 7 d buffer
    assert detail_at(events, times, codes, "2021-03-09 00:00") == "failure"
    assert detail_at(events, times, codes, "2021-03-08 23:59") == "buffer"
    # the expert band covers the whole end minute plus six hours, inside the buffer
    assert detail_at(events, times, codes, "2021-03-05 09:00") == "expert_reported"
    assert detail_at(events, times, codes, "2021-03-05 16:21") == "expert_reported"
    assert detail_at(events, times, codes, "2021-03-05 16:22") == "buffer"
    # uncertain and grey periods include their end instant
    assert detail_at(events, times, codes, "2021-03-16 01:00") == "uncertain:load_held"
    assert detail_at(events, times, codes, "2021-03-16 01:01") == "negative"
    assert detail_at(events, times, codes, "2021-03-15 09:00") == "grey_period"
    # the buffer is half-open: 48 h after the repair is negative again
    assert detail_at(events, times, codes, "2021-03-12 11:59") == "buffer"
    assert detail_at(events, times, codes, "2021-03-12 12:00") == "negative"


def test_gap_edges_follow_raw_gaps_of_thirty_minutes_or_more():
    gaps = pd.DataFrame({"gap_start": pd.to_datetime(["2021-03-20 10:00:05", "2021-03-21 10:00:05"], utc=True),
                         "gap_end": pd.to_datetime(["2021-03-20 10:31:07", "2021-03-21 10:10:00"], utc=True),
                         "seconds": [1862.0, 595.0]})
    edges = gap_edges(gaps, toy_events(), step="10s")
    assert edges == [(pd.Timestamp("2021-03-20 10:31:00", tz="UTC"), pd.Timestamp("2021-03-20 10:46:07", tz="UTC"))]
    times = pd.date_range("2021-03-20 10:30", "2021-03-20 11:00", freq="10s", tz="UTC")
    codes = zone_codes(times, toy_events(), edges)
    scheme = zone_scheme(toy_events())
    at = lambda s: scheme.detail[codes[times.get_loc(pd.Timestamp(s, tz="UTC"))]]  # noqa: E731
    assert at("2021-03-20 10:30:50") == "negative"
    assert at("2021-03-20 10:31:00") == "gap_edge"
    assert at("2021-03-20 10:46:00") == "gap_edge"
    assert at("2021-03-20 10:46:10") == "negative"


def test_label_grid_and_regimes():
    times = pd.date_range("2021-03-11 23:58", periods=5, freq="1min", tz="UTC")
    grid = pd.DataFrame({"observed": True, "bridged": False}, index=times)
    labelled = label_grid(grid, toy_events(), edges=[])
    assert list(labelled.columns[-3:]) == ["zone_code", "zone", "zone_detail"]
    assert set(labelled["zone"].astype(str)) <= {"negative", "grey", "positive", "masked"}
    assert list(regime_of(times, toy_events())) == ["a", "a", "b", "b", "b"]
    assert regime_of([pd.Timestamp("2022-01-01", tz="UTC")], toy_events())[0] is None


@pytest.mark.parametrize("key", ["failures"])
def test_a_missing_failures_list_is_an_error(key):
    events = toy_events()
    del events[key]
    with pytest.raises(KeyError):
        zone_codes(minute_grid(1), events, edges=[])
