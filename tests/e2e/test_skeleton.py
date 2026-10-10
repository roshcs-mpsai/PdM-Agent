"""The walking skeleton, end to end against a live broker (R0.6, carried to R1).

replayer -> ingest -> frozen detector, each as its own process, exactly as
they run on a laptop. Proves that replayed records become 10-minute windows
and scores, with a log line per window at every hop and counts that
reconcile. The gate and the status view join in tests/e2e/test_status.py.

Needs a broker:  docker compose up -d
Run:             PDM_TEST_BROKER=localhost:1883 pytest -q -m e2e

Synthetic data keeps it independent of the dataset (the detector is the
frozen bundle in eval/frozen); nothing here names a MetroPT signal.
"""
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pdm_common.profile import load_profile

ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
BROKER = os.environ.get("PDM_TEST_BROKER", "")
MINUTES = 30
WINDOWS = MINUTES - 10 + 1      # 10-minute windows at a 1-minute stride

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not BROKER, reason="set PDM_TEST_BROKER=host[:port] to run the e2e tests"),
]


def _host_port():
    host, _, port = BROKER.partition(":")
    return host, int(port or 1883)


def _write_csv(path, profile):
    """Values inside every profile range that change every row (no logger freeze)."""
    analog, digital = profile["signals"]["analog"], profile["signals"]["digital"]
    names = [s["name"] for s in analog + digital]
    t0 = dt.datetime(2020, 3, 21)
    lines = ["," + ",".join(["timestamp", *names])]
    for k in range(MINUTES * 6):
        ts = t0 + dt.timedelta(seconds=10 * k)
        values = [f"{s['range'][0] + (s['range'][1] - s['range'][0]) * (0.3 + 0.001 * k):.4f}" for s in analog]
        values += [f"{k % 2:.1f}" for _ in digital]
        lines.append(f"{k},{ts.isoformat(sep=' ')}," + ",".join(values))
    path.write_text("\n".join(lines) + "\n")


def _start(args, log):
    return subprocess.Popen([sys.executable, *args], cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=open(log, "w"), text=True)


def _wait_for(log, text, count=1, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(log).read_text().count(text) >= count:
            return
        time.sleep(0.1)
    raise AssertionError(f"{log}: wanted {count} x {text!r}\n{Path(log).read_text()[-2000:]}")


def _stop(proc):
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=20)
    assert proc.returncode == 0, out
    return json.loads(out.strip().splitlines()[-1])


def test_replayed_records_become_windows_and_scores(tmp_path, monkeypatch):
    host, port = _host_port()
    profile = load_profile(PROFILE_PATH)
    monkeypatch.setenv("PDM_CATALOGUE", str(tmp_path / "catalogue.jsonl"))
    data = tmp_path / "telemetry.csv"
    _write_csv(data, profile)
    broker = ["--broker", host, "--port", str(port)]
    store = ["--store", f"sqlite:///{tmp_path}/pdm.db"]
    ingest_log, detector_log = tmp_path / "ingest.log", tmp_path / "detector.log"

    ingest = _start(["services/ingest/ingest.py", "--profile", str(PROFILE_PATH), *broker, *store,
                     "--quarantine", str(tmp_path / "quarantine.jsonl"),
                     "--replay-manifest", str(tmp_path / "manifest.json")], ingest_log)
    detector = _start(["services/detector/detector.py", "--profile", str(PROFILE_PATH), *broker, *store],
                      detector_log)
    try:
        _wait_for(ingest_log, "connected to")
        _wait_for(detector_log, "connected to")

        replay = subprocess.run(
            [sys.executable, "services/replayer/replayer.py", "--profile", str(PROFILE_PATH),
             "--data", str(data), *broker, "--speed", "1000000", "--progress-every", "0",
             "--manifest", str(tmp_path / "manifest.json")],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        assert replay.returncode == 0, replay.stderr
        assert f"replayed {MINUTES * 6} of {MINUTES * 6} rows" in replay.stdout

        # all but the last window are released by the stream; the last on shutdown
        _wait_for(detector_log, "detector score ", count=WINDOWS - 1)
        counts = _stop(ingest)
        _wait_for(detector_log, "detector score ", count=WINDOWS)
        scored = _stop(detector)
    finally:
        for proc in (ingest, detector):
            if proc.poll() is None:
                proc.kill()

    # counts reconcile at the ingest hop, and every window got one score
    assert counts["in"] == counts["accepted"] == MINUTES * 6
    assert counts["quarantined"] == 0 and counts["balanced"]
    assert counts["windows_complete"] == WINDOWS and counts["windows_dropped"] == 0
    assert scored["windows"] == scored["scored"] == WINDOWS and scored["refused"] == 0

    # a log line per score, and the windows in the catalogue with the replay's lineage
    assert detector_log.read_text().count("detector score ") == WINDOWS
    entry = json.loads((tmp_path / "catalogue.jsonl").read_text().splitlines()[-1])
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert entry["rows"] == WINDOWS and entry["lineage"]["replay_log_sha256"] == manifest["log_sha256"]
