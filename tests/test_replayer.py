"""Replayer contract: profile-driven schema, header validation, determinism.

All tests run without a broker and without the dataset: rows are synthesized
from whatever signals the asset profile declares, so nothing here names a
MetroPT signal (the services rule) and CI needs no data.

Set PDM_TEST_BROKER=host[:port] to also run the live-broker round-trip test.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "replayer"))

import replayer  # noqa: E402

PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"


@pytest.fixture(scope="module")
def profile():
    return replayer.load_profile(PROFILE_PATH)


@pytest.fixture()
def synthetic_csv(tmp_path, profile):
    """A small CSV shaped like the real file: index, timestamp, all signals."""
    signals = replayer.signal_names(profile)
    path = tmp_path / "synthetic.csv"
    t0 = datetime(2020, 2, 1, 0, 0, 0)
    lines = ["," + ",".join(["timestamp", *signals])]
    for i in range(12):
        ts = t0 + timedelta(seconds=10 * i)
        values = [f"{(i * 1.25 + j) % 9:.3f}" if j < 7 else f"{(i + j) % 2:.1f}"
                  for j in range(len(signals))]
        lines.append(f"{i},{ts.isoformat(sep=' ')}," + ",".join(values))
    path.write_text("\n".join(lines) + "\n")
    return path


def test_message_matches_profile_schema(profile):
    """telemetry_json_v1: fields, topic and signal set all come from the profile."""
    signals = replayer.signal_names(profile)
    ts = datetime(2020, 2, 1, 0, 0, 10)
    values = {s: 1.0 for s in signals}
    topic, payload = replayer.build_message(profile, 7, ts, values)
    msg = json.loads(payload)
    assert topic == profile["topics"]["telemetry"]
    assert msg["schema"] == profile["topics"]["payload_schema"]
    assert msg["asset"] == profile["asset"]["id"]
    assert msg["seq"] == 7
    assert msg["ts"] == "2020-02-01T00:00:10"
    assert set(msg["signals"]) == set(signals)
    # canonical form: byte-stable across runs
    assert payload == json.dumps(msg, sort_keys=True, separators=(",", ":"))


def test_missing_column_is_rejected_by_name(tmp_path, profile, synthetic_csv):
    """A file missing a profile signal fails fast, naming the column."""
    signals = replayer.signal_names(profile)
    dropped = signals[-1]
    lines = synthetic_csv.read_text().splitlines()
    header = lines[0].split(",")
    idx = header.index(dropped)
    bad = tmp_path / "missing_column.csv"
    bad.write_text("\n".join(
        ",".join(v for i, v in enumerate(line.split(",")) if i != idx)
        for line in lines
    ))
    with pytest.raises(SystemExit, match=dropped):
        list(replayer.replay(profile, bad, None, speed=1e9))


def test_two_replays_are_byte_identical(tmp_path, profile, synthetic_csv):
    """PR-01: same file, same arguments -> byte-identical message logs."""
    logs = []
    for name in ("a.log", "b.log"):
        log = tmp_path / name
        counts = replayer.replay(
            profile, synthetic_csv, None, speed=1e9, log_path=log
        )
        assert counts["published"] == 12
        assert counts["skipped"] == 0
        logs.append(log.read_bytes())
    assert logs[0] == logs[1]


def test_from_to_and_limit_filter(profile, synthetic_csv, tmp_path):
    counts = replayer.replay(
        profile, synthetic_csv, None, speed=1e9,
        from_ts=datetime(2020, 2, 1, 0, 0, 30),
        to_ts=datetime(2020, 2, 1, 0, 1, 40),
        limit=5,
    )
    assert counts["published"] == 5
    assert counts["filtered"] == 3  # the three rows before --from


def test_step_wait_paces_and_caps():
    t0 = datetime(2020, 2, 1)
    t1 = t0 + timedelta(seconds=10)
    gap = t0 + timedelta(days=3)
    assert replayer.step_wait(None, t0, 1.0, 60.0) == 0.0
    assert replayer.step_wait(t0, t1, 1.0, 60.0) == pytest.approx(10.0)
    assert replayer.step_wait(t0, t1, 10.0, 60.0) == pytest.approx(1.0)
    assert replayer.step_wait(t0, gap, 10.0, 60.0) == 60.0   # capped
    assert replayer.step_wait(t1, t0, 1.0, 60.0) == 0.0      # out of order


def test_cli_dry_run(tmp_path, synthetic_csv):
    """The CLI wires up end to end without a broker."""
    log = tmp_path / "cli.log"
    result = subprocess.run(
        [sys.executable, str(ROOT / "services" / "replayer" / "replayer.py"),
         "--profile", str(PROFILE_PATH), "--data", str(synthetic_csv),
         "--dry-run", "--speed", "1000000", "--log", str(log),
         "--progress-every", "0"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "replayed 12 of 12 rows" in result.stdout
    assert len(log.read_text().splitlines()) == 12


@pytest.mark.skipif(
    not os.environ.get("PDM_TEST_BROKER"),
    reason="set PDM_TEST_BROKER=host[:port] to run the live-broker test",
)
def test_round_trip_against_live_broker(profile, synthetic_csv, tmp_path):
    """Everything published is received, byte for byte, in order."""
    import paho.mqtt.client as mqtt

    broker = os.environ["PDM_TEST_BROKER"]
    host, _, port = broker.partition(":")
    port = int(port or 1883)
    received = []

    sub = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    sub.on_message = lambda c, u, m: received.append(f"{m.topic} {m.payload.decode()}")
    sub.connect(host, port)
    sub.subscribe(profile["topics"]["telemetry"], qos=1)
    sub.loop_start()

    log = tmp_path / "sent.log"
    publish, close = replayer.make_mqtt_publisher(host, port, qos=1)
    try:
        counts = replayer.replay(
            profile, synthetic_csv, publish, speed=100.0, log_path=log
        )
    finally:
        close()
    deadline = datetime.now() + timedelta(seconds=10)
    while len(received) < counts["published"] and datetime.now() < deadline:
        pass
    sub.loop_stop()
    sub.disconnect()

    assert counts["published"] == 12
    assert received == log.read_text().splitlines()


# --- the replay manifest and fault injection (ING-05, Journey C) ---------------------------

def _faults(profile, entries):
    return replayer.Faults({"schema": "faults_v1", "faults": entries}, replayer.signal_names(profile))


def test_manifest_records_inputs_and_is_stable(tmp_path, profile, synthetic_csv):
    manifests = []
    for name in ("a", "b"):
        counts = replayer.replay(profile, synthetic_csv, None, speed=1e9, log_path=tmp_path / f"{name}.log",
                                 manifest_path=tmp_path / f"{name}.json", profile_path=PROFILE_PATH,
                                 arguments={"speed": 1e9})
        m = json.loads((tmp_path / f"{name}.json").read_text())
        manifests.append(m)
        import hashlib
        assert m["log_sha256"] == hashlib.sha256((tmp_path / f"{name}.log").read_bytes()).hexdigest()
        assert m["log_sha256"] == counts["log_sha256"]
    a, b = ({k: v for k, v in m.items() if k != "written_at"} for m in manifests)
    assert a == b
    assert a["schema"] == "replay_manifest_v1" and a["published"] == a["messages"] == 12
    assert len(a["data_sha256"]) == 64 and len(a["profile_sha256"]) == 64 and len(a["events_sha256"]) == 64
    assert a["first_ts"] == "2020-02-01T00:00:00" and a["last_ts"] == "2020-02-01T00:01:50"


def test_value_faults_change_one_signal(profile, synthetic_csv, tmp_path):
    sig = replayer.signal_names(profile)[1]
    faults = _faults(profile, [
        {"kind": "offset", "signal": sig, "from": "2020-02-01T00:00:20", "to": "2020-02-01T00:00:40", "value": 5},
        {"kind": "stuck", "signal": sig, "from": "2020-02-01T00:01:00", "to": "2020-02-01T00:01:30"},
        {"kind": "set", "signal": sig, "at": "2020-02-01T00:01:35", "value": 99.0},
    ])
    clean, faulty = tmp_path / "clean.log", tmp_path / "faulty.log"
    replayer.replay(profile, synthetic_csv, None, speed=1e9, log_path=clean)
    replayer.replay(profile, synthetic_csv, None, speed=1e9, log_path=faulty, faults=faults)
    read = lambda p: [json.loads(line.split(" ", 1)[1]) for p_ in [p] for line in p_.read_text().splitlines()]  # noqa: E731
    a, b = read(clean), read(faulty)
    diff = {m["seq"]: (m["signals"][sig], n["signals"][sig]) for m, n in zip(a, b) if m != n}
    assert {k: v[1] - v[0] for k, v in diff.items() if k in (2, 3)} == {2: 5.0, 3: 5.0}
    assert b[7]["signals"][sig] == b[6]["signals"][sig] == b[8]["signals"][sig] == a[6]["signals"][sig]
    assert b[10]["signals"][sig] == 99.0 and 10 in diff
    assert all(m["signals"][s] == n["signals"][s] for m, n in zip(a, b) for s in m["signals"] if s != sig)
    assert faults.applied == {"offset": 2, "drift": 0, "stuck": 3, "set": 1, "drop": 0, "corrupt": 0}


def test_drop_leaves_a_seq_gap_and_corrupt_damages_one_message(profile, synthetic_csv, tmp_path):
    faults = _faults(profile, [
        {"kind": "drop", "from": "2020-02-01T00:00:30", "to": "2020-02-01T00:00:50"},
        {"kind": "corrupt", "at": "2020-02-01T00:01:00", "mode": "bad_json"},
        {"kind": "corrupt", "at": "2020-02-01T00:01:10", "mode": "duplicate"},
        {"kind": "corrupt", "at": "2020-02-01T00:01:20", "mode": "missing_signal"},
    ])
    log = tmp_path / "faulty.log"
    counts = replayer.replay(profile, synthetic_csv, None, speed=1e9, log_path=log, faults=faults)
    lines = [line.split(" ", 1)[1] for line in log.read_text().splitlines()]
    assert counts["dropped"] == 2 and counts["published"] == 10 and counts["messages"] == 11
    seqs = [json.loads(p)["seq"] for p in lines if p.startswith("{") and p.endswith("}")]
    assert 3 not in seqs and 4 not in seqs and seqs.count(7) == 2
    assert sum(1 for p in lines if not p.endswith("}")) == 1                 # the bad_json one
    missing = [json.loads(p) for p in lines if p.endswith("}") and json.loads(p)["seq"] == 8][0]
    assert len(missing["signals"]) == len(replayer.signal_names(profile)) - 1


def test_faults_file_is_checked(profile):
    with pytest.raises(replayer.FaultsError, match="not in the profile"):
        _faults(profile, [{"kind": "offset", "signal": "no_such_signal", "from": "2020-01-01", "to": "2020-01-02",
                           "value": 1}])
    with pytest.raises(replayer.FaultsError, match="schema"):
        replayer.Faults({"faults": []}, replayer.signal_names(profile))
