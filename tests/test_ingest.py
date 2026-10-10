"""Ingest (T3; ING-06 to ING-08, ING-13): validation, quarantine, flags,
windows, store and reconciliation.

Messages come from the replayer's own build_message, so this is also the
contract test between the two services. No broker and no dataset needed;
nothing here names a MetroPT signal. The windows themselves are held to the
batch pipeline in tests/test_stream.py.
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "replayer"))
sys.path.insert(0, str(ROOT / "services" / "ingest"))

import ingest as ingest_service  # noqa: E402
import replayer  # noqa: E402
from pdm_common import catalogue  # noqa: E402
from pdm_common.features import features_file, load_feature_spec  # noqa: E402
from pdm_common.profile import load_profile, profile_hash, signal_names  # noqa: E402
from pdm_common.store import Store  # noqa: E402

PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
T0 = datetime(2020, 4, 17, 0, 0, 0)


@pytest.fixture(scope="module")
def profile():
    return load_profile(PROFILE_PATH)


@pytest.fixture(scope="module")
def spec(profile):
    return load_feature_spec(features_file(profile, PROFILE_PATH))


@pytest.fixture()
def ingest(profile, spec, tmp_path):
    return ingest_service.Ingest(profile, profile_hash(PROFILE_PATH), tmp_path / "quarantine.jsonl", spec=spec)


def message(profile, seq, ts=None, **override):
    """A well-formed telemetry_json_v1 payload; signal i carries value i + seq/10."""
    ts = ts or T0 + timedelta(seconds=10 * seq)
    values = {s: float(i) + seq / 10 for i, s in enumerate(signal_names(profile))}
    _, payload = replayer.build_message(profile, seq, ts, values)
    if override:
        msg = json.loads(payload)
        msg.update(override)
        payload = json.dumps(msg)
    return payload


def plausible(profile, seq, ts=None, set_values=None):
    """A message whose values sit inside every range and change every row (no freeze)."""
    ts = ts or T0 + timedelta(seconds=10 * seq)
    values = {}
    for s in profile["signals"]["analog"]:
        lo, hi = s["range"]
        values[s["name"]] = lo + (hi - lo) * (0.3 + 0.001 * (seq % 200))
    for s in profile["signals"]["digital"]:
        values[s["name"]] = float(seq % 2)
    values.update(set_values or {})
    return replayer.build_message(profile, seq, ts, values)[1]


def quarantined(ingest):
    path = ingest.quarantine_path
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_valid_message_is_accepted(profile, ingest):
    assert ingest.handle(message(profile, 0)) == []        # first window still open
    assert ingest.counts["accepted"] == 1
    assert quarantined(ingest) == []


def test_missing_signal_and_wrong_asset_are_quarantined(profile, ingest):
    """The R0.3 check: both land in quarantine, and in = accepted + quarantined."""
    good = message(profile, 0)
    no_signal = json.loads(message(profile, 1))
    dropped = signal_names(profile)[-1]
    del no_signal["signals"][dropped]
    wrong_asset = message(profile, 2, asset="some_other_asset")

    for payload in (good, json.dumps(no_signal), wrong_asset):
        ingest.handle(payload)

    rejects = quarantined(ingest)
    assert [r["error_code"] for r in rejects] == ["missing_signal", "unknown_asset"]
    assert dropped in rejects[0]["error"]
    assert rejects[1]["payload"] == wrong_asset           # raw payload kept for audit
    report = ingest.reconciliation()
    assert (report["in"], report["accepted"], report["quarantined"]) == (3, 1, 2)
    assert report["balanced"]


@pytest.mark.parametrize("mutate, code", [
    (lambda m: m.update(schema="telemetry_json_v9"), "wrong_schema"),
    (lambda m: m["signals"].update({next(iter(m["signals"])): "8.2"}), "non_numeric"),
    (lambda m: m["signals"].update({next(iter(m["signals"])): True}), "non_numeric"),
    (lambda m: m["signals"].update({"not_a_signal": 1.0}), "unknown_signal"),
    (lambda m: m.update(ts="yesterday"), "bad_timestamp"),
    (lambda m: m.update(seq=-1), "bad_seq"),
    (lambda m: m.update(extra_field=1), "unexpected_field"),
])
def test_each_fault_type_has_its_code(profile, ingest, mutate, code):
    msg = json.loads(message(profile, 0))
    mutate(msg)
    assert ingest.handle(json.dumps(msg)) == []
    assert [r["error_code"] for r in quarantined(ingest)] == [code]


def test_non_finite_and_broken_json(profile, ingest):
    nan = message(profile, 0).replace(":0.0,", ":NaN,", 1)
    assert "NaN" in nan
    ingest.handle(nan)
    ingest.handle(b'{"schema": "telemetry_json_v1"')
    assert [r["error_code"] for r in quarantined(ingest)] == ["non_numeric", "bad_json"]
    assert ingest.reconciliation()["balanced"]


def test_duplicate_and_out_of_order_timestamps(profile, ingest):
    ingest.handle(message(profile, 0))
    ingest.handle(message(profile, 1))
    ingest.handle(message(profile, 2, ts=T0 + timedelta(seconds=10)))   # same ts as seq 1
    ingest.handle(message(profile, 3, ts=T0))                           # older
    assert [r["error_code"] for r in quarantined(ingest)] == ["duplicate_ts", "stale_ts"]
    assert ingest.counts["accepted"] == 2


def test_seq_gaps_count_as_lost_and_seq_zero_starts_a_new_replay(profile, ingest):
    for seq in (0, 1, 4, 5):            # 2 and 3 never arrive
        ingest.handle(message(profile, seq))
    assert ingest.counts["lost"] == 2
    ingest.handle(message(profile, 0))  # a second replay of the same file
    assert ingest.counts["quarantined"] == 0
    assert ingest.counts["accepted"] == 5


def test_ten_minute_windows_with_the_declared_features(profile, spec, ingest):
    outs = []
    for seq in range(70):               # 00:00:00 .. 00:11:30 at 10 s
        outs += ingest.handle(plausible(profile, seq))
    # a window is released once its last point is 30 s old: [00:00, 00:10) at 00:10:20
    assert len(outs) == 2
    assert ingest.flush() == []         # nothing more is complete
    first = json.loads(outs[0].payload)
    assert outs[0].topic == profile["topics"]["windows"] and outs[0].retain is False
    assert first["schema"] == "window_v1" and first["asset"] == profile["asset"]["id"]
    assert first["profile_sha256"] == profile_hash(PROFILE_PATH)
    assert len(first["features_sha256"]) == 64
    assert (first["start"], first["end"]) == ("2020-04-17T00:00:00Z", "2020-04-17T00:10:00Z")
    assert (first["n_observed"], first["n_bridged"], first["out_of_range"]) == (60, 0, {})
    assert sorted(first["features"]) == sorted(spec.names)
    assert outs[0].payload == json.dumps(first, sort_keys=True, separators=(",", ":"))
    assert json.loads(outs[1].payload)["start"] == "2020-04-17T00:01:00Z"
    report = ingest.reconciliation()
    assert (report["windows_complete"], report["balanced"]) == (2, True)


def test_out_of_range_is_flagged_kept_and_kept_off_the_grid(profile, spec, tmp_path):
    store = Store("sqlite://")
    store.init_schema()
    service = ingest_service.Ingest(profile, "x" * 64, spec=spec, store=store)
    analog = profile["signals"]["analog"][0]
    bad = analog["range"][1] + 100.0
    outs = []
    for seq in range(70):
        outs += service.handle(plausible(profile, seq, set_values={analog["name"]: bad} if seq == 30 else None))
    outs += service.flush()
    report = service.reconciliation()
    assert (report["accepted"], report["quarantined"], report["out_of_range_records"]) == (70, 0, 1)
    assert report["out_of_range_by_signal"] == {analog["name"]: 1}
    row = store.select("telemetry", {"ts": "2020-04-17T00:05:00Z"})[0]
    assert row["out_of_range"] == {analog["name"]: bad} and row["signals"][analog["name"]] == bad
    first = json.loads(outs[0].payload)
    assert first["out_of_range"] == {analog["name"]: 1}
    assert first["n_bridged"] == 1 and first["n_observed"] == 59       # the bad point is bridged
    assert first["features"][f"{analog['name']}_max"] is None or first["features"][f"{analog['name']}_max"] < bad
    assert store.count("windows") == 2 and store.count("telemetry") == 70


def test_store_gets_quarantine_rows(profile, spec):
    store = Store("sqlite://")
    store.init_schema()
    service = ingest_service.Ingest(profile, "x" * 64, spec=spec, store=store)
    service.handle(plausible(profile, 0))
    service.handle(message(profile, 1, asset="some_other_asset"))
    rows = store.select("quarantine")
    assert [r["error_code"] for r in rows] == ["unknown_asset"]
    assert json.loads(rows[0]["payload"])["asset"] == "some_other_asset"


def test_catalogue_entry_for_the_windows(profile, spec, ingest, tmp_path):
    for seq in range(70):
        ingest.handle(plausible(profile, seq))
    manifest = tmp_path / "replay_manifest.json"
    manifest.write_text(json.dumps({"data_file": "x.csv", "data_sha256": "d" * 64, "log_sha256": "l" * 64}))
    entry = ingest.catalogue_entry(replay_manifest=manifest, profile_path=PROFILE_PATH,
                                   path=tmp_path / "catalogue.jsonl")
    assert entry["kind"] == "stream_windows" and entry["rows"] == 2
    assert entry["lineage"]["data_sha256"] == "d" * 64 and len(entry["lineage"]["events_sha256"]) == 64
    assert catalogue.find(tmp_path / "catalogue.jsonl", kind="stream_windows")[0]["entry_sha256"] == \
        entry["entry_sha256"]


def test_naive_timestamps_are_read_in_the_source_timezone(profile, spec, tmp_path):
    lisbon = dict(profile, source_timezone="Europe/Lisbon")
    service = ingest_service.Ingest(lisbon, "x" * 64, tmp_path / "q.jsonl", spec=spec)
    outs = []
    for seq in range(64):               # summer time: local 00:00 is 23:00 UTC
        outs += service.handle(plausible(profile, seq, ts=datetime(2020, 6, 1) + timedelta(seconds=10 * seq)))
    assert json.loads(outs[0].payload)["start"] == "2020-05-31T23:00:00Z"


def test_a_rejected_message_still_advances_seq(profile, ingest):
    """Quarantined is not lost: seq 0 ok, seq 1 rejected, seq 2 ok -> lost 0."""
    bad = json.loads(message(profile, 1))
    del bad["signals"][signal_names(profile)[0]]
    for payload in (message(profile, 0), json.dumps(bad), message(profile, 2)):
        ingest.handle(payload)
    report = ingest.reconciliation()
    assert (report["accepted"], report["quarantined"], report["lost"]) == (2, 1, 0)


def test_a_new_replay_releases_the_previous_stream(profile, ingest):
    """Replay 1 and replay 2 of the same rows never share a window."""
    first = [o for seq in range(61) for o in ingest.handle(plausible(profile, seq))]
    assert first == []                                   # [00:00, 00:10) not yet final
    restart = ingest.handle(plausible(profile, 0))       # replay 2 begins: replay 1 is flushed
    assert [json.loads(o.payload)["start"] for o in restart] == ["2020-04-17T00:00:00Z"]
    for seq in range(1, 61):
        ingest.handle(plausible(profile, seq))
    again = ingest.flush()
    assert [o.payload for o in again] == [o.payload for o in restart]
    assert ingest.counts["quarantined"] == 0 and ingest.reconciliation()["replays"] == 2


def test_a_quarantined_seq_zero_still_starts_the_new_replay(profile, ingest):
    for seq in range(4):
        ingest.handle(message(profile, seq))
    broken = message(profile, 0).replace(":0.0,", ":NaN,", 1)   # replay 2, seq 0 rejected
    ingest.handle(broken)
    for seq in range(1, 4):
        ingest.handle(message(profile, seq))
    report = ingest.reconciliation()
    assert report["quarantined_by_code"] == {"non_numeric": 1}
    assert report["accepted"] == 7 and report["balanced"]


@pytest.mark.parametrize("ts", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_a_timestamp_that_overflows_utc_is_quarantined(profile, ingest, ts):
    ingest.handle(json.dumps(dict(json.loads(message(profile, 0)), ts=ts)))
    assert [r["error_code"] for r in quarantined(ingest)] == ["bad_timestamp"]
    assert ingest.reconciliation()["balanced"]


def test_an_unexpected_error_is_quarantined_not_lost(profile, ingest, monkeypatch):
    def broken(payload):
        raise RuntimeError("bug")
    monkeypatch.setattr(ingest, "_parse", broken)
    ingest.handle(message(profile, 0))
    assert [r["error_code"] for r in quarantined(ingest)] == ["internal_error"]
    assert ingest.reconciliation()["balanced"]
