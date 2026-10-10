"""The stack in one process (T28): eval/pipeline.py runs the deployed service classes on
an in-memory bus. The data test replays the F4 lead-up through it and gets the
incidents the evaluated (batch) gate opens on the frozen scores."""
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

from pipeline import Bus, Pipeline  # noqa: E402
from pdm_common.mqtt import Out  # noqa: E402
from pdm_common.store import Store  # noqa: E402

PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
RAW = ROOT / "data" / "MetroPT3(AirCompressor).csv"
SCORES = ROOT / "eval" / "out" / "freeze" / "pca" / "scores.npz"
WINDOWS = ROOT / "data" / "processed_v4" / "windows_10min.csv.gz"


def store():
    s = Store("sqlite://")
    s.init_schema()
    return s


def synthetic(profile, minutes, anomalous_after=None):
    import replayer
    for seq in range(minutes * 6):
        values = {}
        shift = 0.3 if anomalous_after is not None and seq >= anomalous_after * 6 else 0.0
        for s in profile["signals"]["analog"]:
            lo, hi = s["range"]
            values[s["name"]] = lo + (hi - lo) * (0.3 + 0.0001 * seq + shift)
        for s in profile["signals"]["digital"]:
            values[s["name"]] = float(seq % 2)
        yield replayer.build_message(profile, seq, dt.datetime(2020, 6, 1) + dt.timedelta(seconds=10 * seq), values)[1]


def test_the_bus_delivers_depth_first_in_order_and_keeps_retained():
    bus = Bus(record=True)
    seen = []
    bus.subscribe("a", lambda p: [Out("b", p + "1"), Out("c", p + "2", retain=True)])
    bus.subscribe("b", lambda p: seen.append(("b", p)) or [Out("c", p + "3")])
    bus.subscribe("c", lambda p: seen.append(("c", p)) or [])
    bus.publish(Out("a", "x"))
    assert seen == [("b", "x1"), ("c", "x2"), ("c", "x13")]
    assert bus.retained == {"c": "x2"} and bus.counts["c"] == 2


def test_a_synthetic_excursion_flows_to_an_incident():
    s = store()
    pipe = Pipeline(PROFILE_PATH, s, record=True, compute_every=60)
    pipe.feed(synthetic(pipe.profile, 120, anomalous_after=60))
    pipe.flush()
    report = pipe.report()
    assert report["ingest"]["balanced"] and report["detector"]["refused"] == 0
    assert report["gate"]["opened"] == 1 and s.count("incidents") == 1
    opened = pipe.bus.messages(pipe.topics["incidents"])[0]
    assert opened["event"] == "opened" and opened["checks"]
    assert json.loads(pipe.bus.retained[pipe.topics["status"]])["ne107"] == "maintenance_required"
    assert s.count("scores") == report["detector"]["scored"] == s.count("windows")


@pytest.mark.skipif(not (RAW.exists() and SCORES.exists() and WINDOWS.exists()),
                    reason="needs the MetroPT-3 CSV, the frozen scores (tools/freeze.py) and data/processed_v4")
def test_the_f4_lead_up_opens_the_incidents_the_evaluated_gate_opens():
    s = store()
    pipe = Pipeline(PROFILE_PATH, s)
    pipe.replay(RAW, from_ts=dt.datetime(2020, 7, 7), to_ts=dt.datetime(2020, 7, 16, 6))
    streamed = [pd.Timestamp(i["opened_at"]).tz_convert(None) for i in s.select("incidents", order="opened_at")]
    from pdm_common.gate import gate, gate_params
    scores = np.load(SCORES)["scores"]
    ends = pd.read_csv(WINDOWS, usecols=["window_end"], parse_dates=["window_end"])["window_end"]
    opens, _, _ = gate(ends.to_numpy().astype("datetime64[ns]").astype("int64"),
                       scores > pipe.frozen.detector.threshold, gate_params(pipe.profile))
    batch = [pd.Timestamp(o) for o in opens if pd.Timestamp("2020-07-07") <= pd.Timestamp(o) < pd.Timestamp("2020-07-16 06:00")]
    assert len(streamed) == len(batch) == 3
    # from Jul 14 on every window's seven-day look-back lies inside the replay: identical openings
    assert streamed[1:] == batch[1:] == [pd.Timestamp("2020-07-14 02:33"), pd.Timestamp("2020-07-15 00:46")]
    # Jul 8 reads a look-back that starts with the replay, so it may move by a minute or two
    assert abs(streamed[0] - batch[0]) <= pd.Timedelta(minutes=2)
