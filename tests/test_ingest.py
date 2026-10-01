"""Ingest stub (R0.3): validation, quarantine, reconciliation, windows.

Messages come from the replayer's own build_message, so this is also the
contract test between the two services. No broker and no dataset needed;
nothing here names a MetroPT signal.
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
from pdm_common.profile import load_profile, profile_hash, signal_names  # noqa: E402

PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
T0 = datetime(2020, 4, 17, 0, 0, 0)


@pytest.fixture(scope="module")
def profile():
    return load_profile(PROFILE_PATH)


@pytest.fixture()
def ingest(profile, tmp_path):
    return ingest_service.Ingest(profile, profile_hash(PROFILE_PATH), tmp_path / "quarantine.jsonl")


def message(profile, seq, ts=None, **override):
    """A valid telemetry_json_v1 payload; signal i carries value i + seq/10."""
    ts = ts or T0 + timedelta(seconds=10 * seq)
    values = {s: float(i) + seq / 10 for i, s in enumerate(signal_names(profile))}
    _, payload = replayer.build_message(profile, seq, ts, values)
    if override:
        msg = json.loads(payload)
        msg.update(override)
        payload = json.dumps(msg)
    return payload


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


def test_one_minute_windows_of_the_detector_inputs(profile, ingest):
    outs = []
    for seq in range(13):               # 00:00:00 .. 00:02:00 at 10 s
        outs += ingest.handle(message(profile, seq))
    assert len(outs) == 2               # minutes 00:00 and 00:01 closed
    outs += ingest.flush()              # minute 00:02, one sample
    assert len(outs) == 3

    first = json.loads(outs[0].payload)
    assert outs[0].topic == profile["topics"]["windows"]
    assert outs[0].retain is False
    assert first["schema"] == "window_v1"
    assert first["asset"] == profile["asset"]["id"]
    assert first["profile_sha256"] == profile_hash(PROFILE_PATH)
    assert (first["start"], first["end"]) == ("2020-04-17T00:00:00Z", "2020-04-17T00:01:00Z")
    assert first["n"] == 6
    assert list(first["features"]) == sorted(profile["detector"]["inputs"])
    # signal i carries i + seq/10, so the mean over seq 0..5 is i + 0.25
    names = signal_names(profile)
    for name, value in first["features"].items():
        assert value == pytest.approx(names.index(name) + 0.25)
    assert json.loads(outs[2].payload)["n"] == 1
    assert outs[0].payload == json.dumps(first, sort_keys=True, separators=(",", ":"))


def test_naive_timestamps_are_read_in_the_source_timezone(profile, tmp_path):
    lisbon = dict(profile, source_timezone="Europe/Lisbon")
    service = ingest_service.Ingest(lisbon, "x" * 64, tmp_path / "q.jsonl")
    outs = []
    for seq in range(7):                # summer time: local 00:00 is 23:00 UTC
        outs += service.handle(message(profile, seq, ts=datetime(2020, 6, 1) + timedelta(seconds=10 * seq)))
    assert json.loads(outs[0].payload)["start"] == "2020-05-31T23:00:00Z"


def test_a_rejected_message_still_advances_seq(profile, ingest):
    """Quarantined is not lost: seq 0 ok, seq 1 rejected, seq 2 ok -> lost 0."""
    bad = json.loads(message(profile, 1))
    del bad["signals"][signal_names(profile)[0]]
    for payload in (message(profile, 0), json.dumps(bad), message(profile, 2)):
        ingest.handle(payload)
    report = ingest.reconciliation()
    assert (report["accepted"], report["quarantined"], report["lost"]) == (2, 1, 0)


def test_a_new_replay_closes_the_previous_open_window(profile, ingest):
    """Replay 1 and replay 2 of the same rows never share a window."""
    first = [o for seq in range(4) for o in ingest.handle(message(profile, seq))]
    assert first == []                                   # minute still open
    restart = ingest.handle(message(profile, 0))         # replay 2 begins
    assert [json.loads(o.payload)["n"] for o in restart] == [4]
    for seq in range(1, 4):
        ingest.handle(message(profile, seq))
    assert [json.loads(o.payload)["n"] for o in ingest.flush()] == [4]
    assert ingest.counts["quarantined"] == 0


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
