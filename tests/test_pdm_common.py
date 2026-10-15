"""pdm_common Version Zero: profile and events loading, hashing, time helpers.

Nothing here names a MetroPT signal; signal facts come from the profile.
"""
import datetime as dt
import hashlib
from pathlib import Path

import pytest

from pdm_common.events import event_span, events_hash, half_open, load_events
from pdm_common.hashing import canonical_json, sha256_file
from pdm_common.profile import (
    ProfileError,
    events_file,
    load_profile,
    profile_hash,
    signal_names,
)
from pdm_common.timeutil import iso_utc, parse_duration, to_utc

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
UTC = dt.timezone.utc


def test_profile_hash_is_the_sha256_of_the_file_bytes():
    expected = hashlib.sha256(PROFILE_PATH.read_bytes()).hexdigest()
    assert profile_hash(PROFILE_PATH) == expected == sha256_file(PROFILE_PATH)
    assert len(expected) == 64


def test_profile_hash_changes_with_content(tmp_path):
    copy = tmp_path / "asset_profile.yaml"
    copy.write_bytes(PROFILE_PATH.read_bytes())
    before = profile_hash(copy)
    copy.write_bytes(PROFILE_PATH.read_bytes() + b"\n# edited\n")
    assert profile_hash(copy) != before


def test_signal_names_come_from_the_profile_analog_first():
    profile = load_profile(PROFILE_PATH)
    names = signal_names(profile)
    analog = [s["name"] for s in profile["signals"]["analog"]]
    digital = [s["name"] for s in profile["signals"]["digital"]]
    assert names == analog + digital
    assert len(names) == len(set(names)) == 15


def test_missing_key_is_named(tmp_path):
    bad = tmp_path / "asset_profile.yaml"
    bad.write_text("asset: {id: x}\nsignals: {analog: [], digital: []}\n")
    with pytest.raises(ProfileError, match="topics: Field required"):
        load_profile(bad)


def test_invalid_yaml_reports_the_line(tmp_path):
    bad = tmp_path / "asset_profile.yaml"
    bad.write_text("asset:\n  id: x\n  bad: [unclosed\n")
    with pytest.raises(ProfileError, match=r"asset_profile\.yaml:\d+: not valid YAML"):
        load_profile(bad)


def test_events_file_resolves_next_to_the_profile():
    profile = load_profile(PROFILE_PATH)
    path = events_file(profile, PROFILE_PATH)
    assert path == PROFILE_PATH.parent / "events.yaml"
    events = load_events(path)
    assert [f["id"] for f in events["failures"]] == ["F1", "F2", "F3", "F4"]
    assert events_hash(path) == sha256_file(path)


def test_to_utc_reads_every_form_yaml_produces():
    # PyYAML gives a date for "2020-04-25", a datetime when seconds are
    # present, and a plain string for "2020-05-30 12:00".
    assert to_utc(dt.date(2020, 4, 25)) == dt.datetime(2020, 4, 25, tzinfo=UTC)
    assert to_utc(dt.datetime(2020, 4, 13, 18, 29, 20)) == dt.datetime(2020, 4, 13, 18, 29, 20, tzinfo=UTC)
    assert to_utc("2020-05-30 12:00") == dt.datetime(2020, 5, 30, 12, tzinfo=UTC)
    assert to_utc("2020-05-30T12:00:00Z") == dt.datetime(2020, 5, 30, 12, tzinfo=UTC)
    # a naive value is read in the declared source timezone
    lisbon_summer = to_utc("2020-06-01 12:00", tz="Europe/Lisbon")
    assert lisbon_summer == dt.datetime(2020, 6, 1, 11, tzinfo=UTC)


def test_event_span_conventions():
    # spans written with a time include their end instant
    start, end = event_span({"start": "2020-03-27 07:12", "end": "2020-03-27 11:38"})
    assert start == dt.datetime(2020, 3, 27, 7, 12, tzinfo=UTC)
    assert dt.datetime(2020, 3, 27, 11, 38, tzinfo=UTC) < end
    assert end - dt.datetime(2020, 3, 27, 11, 38, tzinfo=UTC) == dt.timedelta(microseconds=1)
    # date spans cover whole days, end day included
    start, end = event_span({"start": dt.date(2020, 4, 25), "end": dt.date(2020, 4, 27)})
    assert (start, end) == (dt.datetime(2020, 4, 25, tzinfo=UTC), dt.datetime(2020, 4, 28, tzinfo=UTC))
    # a quoted date is still a date: the whole end day counts
    assert event_span({"start": "2020-06-30", "end": "2020-07-02"})[1] == dt.datetime(2020, 7, 3, tzinfo=UTC)
    # regimes and buffers are half-open: no widening
    start, end = half_open({"start": "2020-03-01 00:00", "end": "2020-06-08 16:00"})
    assert end == dt.datetime(2020, 6, 8, 16, tzinfo=UTC)


def test_parse_duration():
    assert parse_duration("10s") == dt.timedelta(seconds=10)
    assert parse_duration("30min") == dt.timedelta(minutes=30)
    assert parse_duration("6h") == dt.timedelta(hours=6)
    assert parse_duration("7d") == dt.timedelta(days=7)
    with pytest.raises(ValueError):
        parse_duration("10 minutes")


def test_iso_utc_and_canonical_json():
    assert iso_utc(dt.datetime(2020, 4, 17, 0, 1, tzinfo=UTC)) == "2020-04-17T00:01:00Z"
    assert canonical_json({"b": 1, "a": [1.5, "x"]}) == '{"a":[1.5,"x"],"b":1}'
