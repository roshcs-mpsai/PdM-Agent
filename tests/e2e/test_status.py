"""Replayer -> ingest -> detector -> gate -> status view, against a live broker (INC-07).

The skeleton of tests/e2e/test_skeleton.py with the gate added: every score
reaches the gate, which publishes a retained NE 107 status, and the status
view prints it. The synthetic stream sits far from healthy operation, so the
detector alerts on every window: the status is out of specification until the
gate passes at the 15th window (half the span present, all of it alerting),
then an incident opens and, with no diagnosis yet, the status is maintenance
required.

Needs a broker:  docker compose up -d
Run:             PDM_TEST_BROKER=localhost:1883 pytest -q -m e2e
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_skeleton import BROKER, MINUTES, PROFILE_PATH, ROOT, WINDOWS, _host_port, _start, _stop, _wait_for, \
    _write_csv  # noqa: E402

from pdm_common.profile import load_profile  # noqa: E402

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not BROKER, reason="set PDM_TEST_BROKER=host[:port] to run the e2e tests"),
]


def test_scores_become_a_retained_ne107_status(tmp_path, monkeypatch):
    import paho.mqtt.publish as publish

    host, port = _host_port()
    profile = load_profile(PROFILE_PATH)
    publish.single(profile["topics"]["status"], payload=None, retain=True, hostname=host, port=port)  # clear
    monkeypatch.setenv("PDM_CATALOGUE", str(tmp_path / "catalogue.jsonl"))
    data = tmp_path / "telemetry.csv"
    _write_csv(data, profile)
    broker = ["--broker", host, "--port", str(port)]
    store = ["--store", f"sqlite:///{tmp_path}/pdm.db"]
    logs = {name: tmp_path / f"{name}.log" for name in ("ingest", "detector", "gate")}
    procs = {
        "ingest": _start(["services/ingest/ingest.py", "--profile", str(PROFILE_PATH), *broker, *store,
                          "--quarantine", str(tmp_path / "quarantine.jsonl")], logs["ingest"]),
        "detector": _start(["services/detector/detector.py", "--profile", str(PROFILE_PATH), *broker, *store],
                           logs["detector"]),
        "gate": _start(["services/gate/gate.py", "--profile", str(PROFILE_PATH), *broker, *store], logs["gate"]),
    }
    try:
        for name in procs:
            _wait_for(logs[name], "connected to")
        replay = subprocess.run(
            [sys.executable, "services/replayer/replayer.py", "--profile", str(PROFILE_PATH), "--data", str(data),
             *broker, "--speed", "1000000", "--progress-every", "0", "--manifest", str(tmp_path / "manifest.json")],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        assert replay.returncode == 0, replay.stderr
        _wait_for(logs["detector"], "detector score ", count=WINDOWS - 1)
        counts = _stop(procs["ingest"])
        _wait_for(logs["detector"], "detector score ", count=WINDOWS)
        _stop(procs["detector"])
        gated = _stop(procs["gate"])
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.kill()

    assert counts["windows_complete"] == WINDOWS and counts["in"] == MINUTES * 6
    assert gated["scores"] == WINDOWS and gated["opened"] == 1 and gated["status"] == "maintenance_required"
    gate_log = logs["gate"].read_text()
    assert "status None -> out_of_specification" in gate_log
    assert "status out_of_specification -> maintenance_required" in gate_log
    assert "incident INC-metropt3_apu-20200321T0024Z opened" in gate_log

    view = subprocess.run(
        [sys.executable, "services/dashboard/status_view.py", "--profile", str(PROFILE_PATH), *broker],
        cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert view.returncode == 0, view.stderr
    assert view.stdout.startswith(profile["asset"]["id"])
    assert "[amber] maintenance_required" in view.stdout
    assert "last window 2020-03-21T00:30:00Z" in view.stdout
