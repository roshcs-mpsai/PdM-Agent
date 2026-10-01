"""R0.6 walking skeleton, end to end against a live broker.

replayer -> ingest -> z-score detector -> retained status -> status view,
each as its own process, exactly as they run on a laptop. Proves that a
replayed record appears as a health status, with a log line per window at
every hop and counts that reconcile.

Needs a broker:  docker compose up -d
Run:             PDM_TEST_BROKER=localhost:1883 pytest -q -m e2e

Synthetic data and a synthetic model keep it independent of the dataset;
nothing here names a MetroPT signal.
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

from pdm_common.hashing import canonical_json
from pdm_common.profile import load_profile, profile_hash, signal_names

ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
BROKER = os.environ.get("PDM_TEST_BROKER", "")
MINUTES = 20

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not BROKER, reason="set PDM_TEST_BROKER=host[:port] to run the e2e tests"),
]


def _host_port():
    host, _, port = BROKER.partition(":")
    return host, int(port or 1883)


def _write_csv(path, profile):
    names = signal_names(profile)
    t0 = dt.datetime(2020, 3, 21)
    lines = ["," + ",".join(["timestamp", *names])]
    for k in range(MINUTES * 6):
        ts = t0 + dt.timedelta(seconds=10 * k)
        values = [f"{10 * i + (k % 6) / 10:.3f}" for i in range(len(names))]
        lines.append(f"{k},{ts.isoformat(sep=' ')}," + ",".join(values))
    path.write_text("\n".join(lines) + "\n")


def _write_model(path, profile):
    """Means at the data's level, so scores stay small and the status good."""
    names = signal_names(profile)
    channels = {n: {"mean": 10.0 * names.index(n) + 0.25, "std": 1.0}
                for n in profile["detector"]["inputs"]}
    model = {"schema": "zscore_model_v1", "detector": "zscore", "asset": profile["asset"]["id"],
             "channels": channels, "threshold": 4.0, "threshold_rule": "test",
             "fit": {"candidate": "synthetic", "profile_sha256": profile_hash(PROFILE_PATH)},
             "in_sample_score": {}}
    path.write_text(canonical_json(model))


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


def test_a_replayed_record_reaches_the_status_view(tmp_path):
    import paho.mqtt.publish as publish

    host, port = _host_port()
    profile = load_profile(PROFILE_PATH)
    status_topic = profile["topics"]["status"]
    publish.single(status_topic, payload=None, retain=True, hostname=host, port=port)  # clear

    data, model = tmp_path / "telemetry.csv", tmp_path / "zscore.json"
    _write_csv(data, profile)
    _write_model(model, profile)
    broker = ["--broker", host, "--port", str(port)]
    ingest_log, detector_log = tmp_path / "ingest.log", tmp_path / "detector.log"

    ingest = _start(["services/ingest/ingest.py", "--profile", str(PROFILE_PATH), *broker,
                     "--quarantine", str(tmp_path / "quarantine.jsonl")], ingest_log)
    detector = _start(["services/detector/zscore.py", "--profile", str(PROFILE_PATH), *broker,
                       "--model", str(model)], detector_log)
    try:
        _wait_for(ingest_log, "connected to")
        _wait_for(detector_log, "connected to")

        replay = subprocess.run(
            [sys.executable, "services/replayer/replayer.py", "--profile", str(PROFILE_PATH),
             "--data", str(data), *broker, "--speed", "1000000", "--progress-every", "0"],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        assert replay.returncode == 0, replay.stderr
        assert f"replayed {MINUTES * 6} of {MINUTES * 6} rows" in replay.stdout

        # every minute but the last is closed by the stream; the last on shutdown
        _wait_for(detector_log, "detector score ", count=MINUTES - 1)
        counts = _stop(ingest)
        _wait_for(detector_log, "detector score ", count=MINUTES)
        scored = _stop(detector)
    finally:
        for proc in (ingest, detector):
            if proc.poll() is None:
                proc.kill()

    # counts reconcile at the ingest hop, and every window got one score
    assert counts["in"] == counts["accepted"] == MINUTES * 6
    assert counts["quarantined"] == 0 and counts["balanced"]
    assert counts["windows"] == MINUTES
    assert scored["windows"] == scored["scored"] == MINUTES and scored["dropped"] == 0

    # a log line per window at every hop
    assert ingest_log.read_text().count("ingest window ") == MINUTES
    assert detector_log.read_text().count("detector score ") == MINUTES

    # the status view shows the last window as a health status
    view = subprocess.run(
        [sys.executable, "services/dashboard/status_view.py", "--profile", str(PROFILE_PATH), *broker],
        cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert view.returncode == 0, view.stderr
    assert view.stdout.startswith(profile["asset"]["id"])
    assert "[green] good" in view.stdout
    assert "last window 2020-03-21T00:20:00Z" in view.stdout
