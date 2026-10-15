"""Drift service (T25; OPS-04, OPS-06): per-signal drift checks every 6 stream hours.

Synthetic streams check the mechanics anywhere: cadence, the minimum data,
out-of-range values, freezes and gap edges left out, the schema, the store rows
and one candidate request per new alert. With data/processed_v4 present,
healthy weeks of 2020 go through the service against the committed reference
without an alert, and a +0.5 bar shift on the panel pressure is flagged within
24 h in each regime.
"""
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "replayer"))
sys.path.insert(0, str(ROOT / "services" / "drift"))

import drift as drift_service  # noqa: E402
import replayer  # noqa: E402
from pdm_common import schemas  # noqa: E402
from pdm_common.events import load_events  # noqa: E402
from pdm_common.profile import events_file, load_profile, profile_hash, signal_names  # noqa: E402
from pdm_common.store import Store  # noqa: E402

PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
PROCESSED = ROOT / "data" / "processed_v4"
REFERENCE = ROOT / "eval" / "frozen" / "metropt3_apu" / "drift_reference.json"
T0 = dt.datetime(2020, 6, 1, 1, 23, 10)        # naive UTC, as the replayer sends it; off the 6 h grid
THRESHOLD = 0.25                              # synthetic reference: a +0.5 shift is ~0.95 sd


@pytest.fixture(scope="module")
def profile():
    return load_profile(PROFILE_PATH)


@pytest.fixture(scope="module")
def sha():
    return profile_hash(PROFILE_PATH)


def panel_signal(profile) -> str:
    """The panel-pressure signal, found by its profile description rather than by name."""
    return next(s["name"] for s in profile["signals"]["analog"] if "panel" in s["desc"])


def _healthy(profile) -> dict:
    """Synthetic healthy operation: every analog signal normal around mid-range, sd 5 % of the range."""
    return {s["name"]: ((s["range"][0] + s["range"][1]) / 2, 0.05 * (s["range"][1] - s["range"][0]))
            for s in profile["signals"]["analog"]}


def synthetic_reference(profile, **changes) -> dict:
    rng = np.random.default_rng(0)
    healthy = _healthy(profile)
    entries = {}
    for name in profile["drift"]["signals"]:
        mean, sd = healthy[name]
        entries[name] = dict(drift_service.summarize(rng.normal(mean, sd, 100_000)), threshold=THRESHOLD)
    ref = {"schema": "drift_reference_v1", "asset": profile["asset"]["id"], "window": profile["drift"]["window"],
           "every": profile["drift"]["every"], "min_samples": 4320, "signals": list(profile["drift"]["signals"]),
           "regimes": [{"name": "synthetic", "start": "2020-01-01T00:00:00Z", "end": "2021-01-01T00:00:00Z"}],
           "by_regime": {"synthetic": entries}}
    ref.update(changes)
    return ref


def stream(profile, start, hours, *, seed=1, seq0=0, shifts=(), edit=None):
    """telemetry_json_v1 every 10 s from ``start``. ``shifts``: (signal, from, amount) added from
    that time on; ``edit(i, ts, values)`` may change a record's values in place."""
    rng = np.random.default_rng(seed)
    healthy = _healthy(profile)
    analog = list(healthy)
    digital = [s["name"] for s in profile["signals"]["digital"]]
    n = int(hours * 360)
    noise = rng.normal(size=(n, len(analog)))
    out = []
    for i in range(n):
        ts = start + dt.timedelta(seconds=10 * i)
        values = {a: healthy[a][0] + healthy[a][1] * noise[i, j] for j, a in enumerate(analog)}
        values.update({d: float((i // 7) % 2) for d in digital})
        for name, since, amount in shifts:
            if ts >= since:
                values[name] += amount
        if edit is not None:
            edit(i, ts, values)
        out.append(replayer.build_message(profile, seq0 + i, ts, values)[1])
    return out


def run(service, payloads) -> list[dict]:
    """Feed the payloads; every message published must follow drift_v1 and be retained."""
    docs = []
    for p in payloads:
        for out in service.handle_telemetry(p):
            assert out.topic == drift_service.DRIFT_TOPIC and out.retain
            assert schemas.validate("drift_v1", out.payload) == [], out.payload[:300]
            docs.append(json.loads(out.payload))
    return docs


def iso(t: dt.datetime) -> str:
    return t.replace(tzinfo=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def alerting(doc) -> list[str]:
    return [e["signal"] for e in doc["signals"] if e["alert"]]


# --- mechanics, on synthetic streams ----------------------------------------------------------------

def test_a_stationary_stream_raises_no_alert(profile, sha):
    service = drift_service.DriftService(profile, synthetic_reference(profile), profile_sha256=sha)
    docs = run(service, stream(profile, T0, 4 * 24))
    assert len(docs) == 16 and not any(d["alert"] for d in docs)
    judged = [e for d in docs for e in d["signals"] if e["distance"] is not None]
    assert len(judged) >= 13 * len(profile["drift"]["signals"])
    assert max(e["distance"] for e in judged) < THRESHOLD / 3
    assert all(d["profile_sha256"] == sha and d["window"] == "24h" and d["candidate"] is None for d in docs)
    assert [e["signal"] for e in docs[-1]["signals"]] == profile["drift"]["signals"]


def test_a_half_unit_shift_on_one_signal_is_flagged_within_24_hours_and_named(profile):
    panel = panel_signal(profile)
    service = drift_service.DriftService(profile, synthetic_reference(profile))
    since = T0 + dt.timedelta(days=2, hours=1, minutes=40)            # 03:03:10, between checks
    docs = run(service, stream(profile, T0, 4 * 24, shifts=[(panel, since, 0.5)]))
    before = [d for d in docs if d["ts"] <= iso(since)]
    assert before and not any(d["alert"] for d in before)
    first = next(d for d in docs if d["alert"])
    lag = pd.Timestamp(first["ts"]) - pd.Timestamp(iso(since))
    assert pd.Timedelta(0) < lag <= pd.Timedelta(hours=24)
    assert alerting(first) == [panel]
    assert [e["verdict"] for e in first["signals"] if e["signal"] == panel] == ["alert"]
    assert all(d["alert"] for d in docs if d["ts"] >= first["ts"])          # the shift stays, so does the alert


def test_checks_fall_on_epoch_aligned_boundaries_and_need_half_a_window(profile):
    service = drift_service.DriftService(profile, synthetic_reference(profile))
    docs = run(service, stream(profile, T0, 30))                          # Jun 1 01:23 -> Jun 2 07:23
    assert [d["ts"] for d in docs] == ["2020-06-01T06:00:00Z", "2020-06-01T12:00:00Z", "2020-06-01T18:00:00Z",
                                       "2020-06-02T00:00:00Z", "2020-06-02T06:00:00Z"]
    n = [d["signals"][0]["n"] for d in docs]
    assert n == [1661, 3821, 5981, 8141, 8640]                            # 10 s samples in each window
    verdicts = [{e["verdict"] for e in d["signals"]} for d in docs]
    assert verdicts[:2] == [{"no_verdict"}, {"no_verdict"}] and all(v == {"ok"} for v in verdicts[2:])
    assert all(e["distance"] is None and not e["alert"] and "4320 needed" in e["note"]
               for d in docs[:2] for e in d["signals"])


def test_a_recording_gap_is_checked_late_and_empty_windows_are_skipped(profile):
    service = drift_service.DriftService(profile, synthetic_reference(profile))
    before = stream(profile, T0, 30)
    after = stream(profile, T0 + dt.timedelta(hours=70), 1, seq0=len(before))   # resumes Jun 3 23:23
    assert len(run(service, before)) == 5
    docs = run(service, after)
    # the windows ending Jun 2 12:00 to Jun 3 06:00 still hold data from before the gap and are checked
    # when data resumes; Jun 3 12:00 and 18:00 hold none and are skipped; Jun 4 00:00 has 37 min
    assert [d["ts"] for d in docs] == ["2020-06-02T12:00:00Z", "2020-06-02T18:00:00Z", "2020-06-03T00:00:00Z",
                                       "2020-06-03T06:00:00Z", "2020-06-04T00:00:00Z"]
    assert [d["signals"][0]["n"] for d in docs] == [6979, 4819, 2659, 499, 221]
    assert all(e["distance"] is None for d in docs[2:] for e in d["signals"])
    assert service.counts["checks_without_data"] == 2


def test_out_of_range_readings_are_left_out(profile):
    panel = panel_signal(profile)
    hi = next(s["range"][1] for s in profile["signals"]["analog"] if s["name"] == panel)

    def spike(i, ts, values):
        if i % 5 == 0:
            values[panel] = hi + 50.0                                     # a broken reading, every fifth record

    service = drift_service.DriftService(profile, synthetic_reference(profile))
    docs = run(service, stream(profile, T0, 2 * 24, edit=spike))
    last = docs[-1]
    by_signal = {e["signal"]: e for e in last["signals"]}
    assert not any(d["alert"] for d in docs)
    assert by_signal[panel]["n"] == 8640 * 4 // 5                         # the in-range four fifths
    assert all(e["n"] == 8640 for s, e in by_signal.items() if s != panel)
    assert service.counts["out_of_range_values"] == 2 * 24 * 360 // 5


def test_a_logger_freeze_is_left_out(profile):
    """Two hours of identical analog rows at the top of the range: a freeze, masked as ingest masks it."""
    frozen_values = {}

    def freeze(i, ts, values):
        if dt.datetime(2020, 6, 2, 9) <= ts < dt.datetime(2020, 6, 2, 11):
            if not frozen_values:
                frozen_values.update({s["name"]: s["range"][1] for s in profile["signals"]["analog"]})
            values.update(frozen_values)

    service = drift_service.DriftService(profile, synthetic_reference(profile))
    docs = run(service, stream(profile, T0, 2 * 24, edit=freeze))
    after = [d for d in docs if d["ts"] >= "2020-06-02T12:00:00Z"]
    assert after and not any(d["alert"] for d in docs)
    # the first identical row is the freeze's start, as pdm_common.quality.freeze_runs reads it
    assert after[0]["signals"][0]["n"] == 8640 - (2 * 360 - 1)


def test_the_refill_after_a_recording_gap_is_left_out(profile):
    """Four restarts in a day, each refilling from the bottom of the range for 15 min. The healthy
    reference holds no gap edges (they are grey), so the check leaves them out too; a reference
    built without that rule keeps them, and then the restarts alone read as drift."""
    panel = panel_signal(profile)
    low = next(s["range"][0] for s in profile["signals"]["analog"] if s["name"] == panel)

    def day_with_restarts():
        payloads = []
        for k in range(5):                                       # 4 h of data, then a 1 h recording gap
            def refill(i, ts, values, k=k):
                if k > 0 and i < 90:
                    values[panel] = low
            payloads += stream(profile, dt.datetime(2020, 6, 1, 0, 0, 5) + k * dt.timedelta(hours=5), 4,
                               seed=k, seq0=len(payloads), edit=refill)
        return payloads + stream(profile, dt.datetime(2020, 6, 2, 0, 0, 5), 0.01, seq0=len(payloads))

    edges = synthetic_reference(profile, gap_edges={"after_gaps_of": "30min", "length": "15min"})
    service = drift_service.DriftService(profile, edges)
    docs = run(service, day_with_restarts())
    assert not any(d["alert"] for d in docs) and service.counts["gap_edge_records"] == 4 * 90
    assert docs[-1]["ts"] == "2020-06-02T00:00:00Z" and docs[-1]["signals"][0]["n"] == 5 * 1440 - 4 * 90
    kept = drift_service.DriftService(profile, synthetic_reference(profile))
    assert any(alerting(d) == [panel] for d in run(kept, day_with_restarts()))


def test_a_check_without_enough_data_gives_no_verdict_and_keeps_the_alert(profile):
    panel = panel_signal(profile)
    store = Store("sqlite://")
    store.init_schema()
    service = drift_service.DriftService(profile, synthetic_reference(profile), store)
    shifted = stream(profile, T0, 2 * 24, shifts=[(panel, T0, 0.5)])
    docs = run(service, shifted)
    assert docs[-1]["alert"] and alerting(docs[-1]) == [panel]
    # 3 h of data after a 2-day gap: the windows are short of data, the alert holds
    later = stream(profile, T0 + dt.timedelta(days=4), 3, seq0=len(shifted), shifts=[(panel, T0, 0.5)])
    later += stream(profile, dt.datetime(2020, 6, 5, 6), 0.01, seq0=len(shifted) + 1080)   # crosses 06:00
    docs = run(service, later)
    thin = docs[-1]
    assert thin["ts"] == "2020-06-05T06:00:00Z" and thin["alert"]
    entry = {e["signal"]: e for e in thin["signals"]}[panel]
    assert entry["distance"] is None and entry["verdict"] == "no_verdict" and entry["alert"] is True
    row = store.select("drift_alerts", {"at": thin["ts"], "signal": panel})[0]
    assert row["distance"] is None and row["alert"] is True
    # a day of healthy data clears it
    healthy = stream(profile, T0 + dt.timedelta(days=5), 30, seed=5, seq0=len(shifted) + 2000)
    docs = run(service, healthy)
    assert not docs[-1]["alert"] and service.report()["active"] == []


def test_drift_alerts_rows_are_written_for_every_check_and_signal(profile, sha):
    store = Store("sqlite://")
    store.init_schema()
    service = drift_service.DriftService(profile, synthetic_reference(profile), store, profile_sha256=sha)
    docs = run(service, stream(profile, T0, 2 * 24))
    signals = profile["drift"]["signals"]
    rows = store.select("drift_alerts", order="id")
    assert len(rows) == len(docs) * len(signals)
    first = rows[:len(signals)]
    assert [r["signal"] for r in first] == signals
    assert {(r["asset_id"], r["at"], r["regime"]) for r in first} == {("metropt3_apu", "2020-06-01T06:00:00Z",
                                                                        "synthetic")}
    last = {r["signal"]: r for r in rows[-len(signals):]}
    for e in docs[-1]["signals"]:
        r = last[e["signal"]]
        assert r["distance"] == pytest.approx(e["distance"]) and r["threshold"] == THRESHOLD and r["alert"] is False


def test_a_new_alert_produces_exactly_one_candidate_request(profile, tmp_path):
    """Two signals shifted by the same ~0.95 sd at the same time go into alert at the same check:
    one request naming both, and none for the checks that follow while the alert holds."""
    panel = panel_signal(profile)
    other = next(s for s in profile["drift"]["signals"] if s != panel)
    healthy = _healthy(profile)
    service = drift_service.DriftService(profile, synthetic_reference(profile), requests_dir=tmp_path)
    since = T0 + dt.timedelta(days=1)
    shifts = [(panel, since, 0.5), (other, since, 0.5 / healthy[panel][1] * healthy[other][1])]
    docs = run(service, stream(profile, T0, 3 * 24, shifts=shifts))
    with_candidate = [d for d in docs if d["candidate"] is not None]
    assert len(with_candidate) == 1 and len([d for d in docs if d["alert"]]) > 3
    request = with_candidate[0]["candidate"]
    files = list(tmp_path.glob("*.json"))
    assert [f.name for f in files] == [request["request_id"] + ".json"]
    assert json.loads(files[0].read_text()) == request
    assert request["schema"] == "candidate_request_v1" and request["trigger"]["kind"] == "drift"
    assert request["trigger"]["at"] == with_candidate[0]["ts"]
    assert {s["signal"] for s in request["trigger"]["signals"]} == set(alerting(with_candidate[0]))
    assert "retrain_candidate.py" in request["next_step"] and "promote" in request["promotion"]
    assert service.counts["candidate_requests"] == 1


def test_bad_messages_are_ignored(profile):
    service = drift_service.DriftService(profile, synthetic_reference(profile))
    good = stream(profile, T0, 0.05)
    msg = json.loads(good[0])
    broken = ["{not json", json.dumps(dict(msg, asset="other")), json.dumps(dict(msg, schema="telemetry_json_v0")),
              json.dumps(dict(msg, signals={k: v for k, v in list(msg["signals"].items())[1:]})),
              json.dumps(dict(msg, signals=dict(msg["signals"], **{panel_signal(profile): "n/a"}))),
              json.dumps(dict(msg, ts="yesterday"))]
    assert run(service, broken + good + [good[3]]) == []
    c = service.counts
    assert c["accepted"] == len(good) and c["ignored_out_of_order"] == 1
    assert {k for k in c if k.startswith("ignored_")} == {
        "ignored_bad_json", "ignored_other_asset", "ignored_wrong_schema", "ignored_missing_signal",
        "ignored_non_numeric", "ignored_bad_timestamp", "ignored_out_of_order"}


def test_a_window_outside_every_calibrated_regime_gets_no_check(profile):
    service = drift_service.DriftService(profile, synthetic_reference(profile))     # calibrated for 2020 only
    assert run(service, stream(profile, dt.datetime(2022, 3, 1, 1), 30)) == []
    assert service.counts["checks_without_reference"] == 5 and service.counts["checks"] == 0


def test_a_new_replay_starts_a_new_stream(profile):
    service = drift_service.DriftService(profile, synthetic_reference(profile))
    first = run(service, stream(profile, T0, 30))
    again = run(service, stream(profile, T0, 30))                          # seq 0 again, same times
    assert [d["ts"] for d in again] == [d["ts"] for d in first]


def test_the_reference_must_fit_the_profile(profile):
    events = load_events(events_file(profile, PROFILE_PATH))
    with pytest.raises(drift_service.DriftError, match="every"):
        drift_service.DriftService(profile, synthetic_reference(profile, every="4h"))
    broken = synthetic_reference(profile)
    del broken["by_regime"]["synthetic"][panel_signal(profile)]["threshold"]
    with pytest.raises(drift_service.DriftError, match="threshold"):
        drift_service.DriftService(profile, broken)
    with pytest.raises(drift_service.DriftError, match="regimes"):
        drift_service.DriftService(profile, synthetic_reference(profile), events=events)
    with pytest.raises(drift_service.DriftError, match="asset"):
        drift_service.DriftService(profile, synthetic_reference(profile, asset="another_asset"))


def test_the_distance_is_the_normed_wasserstein_distance():
    rng = np.random.default_rng(2)
    healthy = rng.normal(5.0, 2.0, 200_000)
    ref = drift_service.summarize(healthy)
    assert len(ref["quantiles"]) == 200 and ref["std"] == pytest.approx(2.0, rel=0.01)
    # a pure shift of the healthy distribution by d is a distance of d / sd
    assert drift_service.drift_distance(healthy[:20_000] + 1.0, ref["quantiles"], ref["std"]) == pytest.approx(0.5,
                                                                                                         abs=0.02)
    assert drift_service.drift_distance(healthy[:20_000], ref["quantiles"], ref["std"]) < 0.02
    # a constant reference: the standard deviation is floored at 0.001, as in Evidently
    assert drift_service.drift_distance([1.0, 1.0], [0.999] * 200, 0.0) == pytest.approx(1.0)


# --- the committed reference on the 2020 data ----------------------------------------------------------

needs_data = pytest.mark.skipif(not (PROCESSED / "grid_10s.csv.gz").exists(), reason="needs data/processed_v4")


@pytest.fixture(scope="module")
def grid(profile):
    names = signal_names(profile)
    g = pd.read_csv(PROCESSED / "grid_10s.csv.gz", usecols=["timestamp", *names, "observed", "zone"],
                    parse_dates=["timestamp"])
    return g[g["observed"].astype(bool)].reset_index(drop=True)


@pytest.fixture(scope="module")
def real_service(profile):
    reference = drift_service.load_reference(REFERENCE)
    events = load_events(events_file(profile, PROFILE_PATH))

    def make():
        return drift_service.DriftService(profile, reference, events=events)
    return make


def replay(service, profile, grid, start, days, shift=None) -> list[dict]:
    """Every observed grid point of [start, start + days) as telemetry, optionally with
    (signal, from, amount) added; zones are not filtered: the stream as recorded."""
    names = signal_names(profile)
    s0 = pd.Timestamp(start)
    part = grid[(grid["timestamp"] >= s0) & (grid["timestamp"] < s0 + pd.Timedelta(days=days))]
    values = part[names].to_numpy(float)
    times = part["timestamp"].dt.to_pydatetime()
    col = names.index(shift[0]) if shift else None
    payloads = []
    for i, (ts, row) in enumerate(zip(times, values)):
        if shift and ts >= shift[1]:
            row = row.copy()
            row[col] += shift[2]
        payloads.append(replayer.build_message(profile, i, ts, dict(zip(names, row.tolist())))[1])
    return run(service, payloads)


@needs_data
def test_the_committed_reference_is_what_the_tool_builds(capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("drift_reference", ROOT / "tools" / "drift_reference.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    assert tool.main(["--processed", str(PROCESSED), "--check"]) == 0, capsys.readouterr().out


@needs_data
@pytest.mark.parametrize("start, days, regime", [("2020-02-03", 21, "light_duty"),
                                                 ("2020-06-29", 7, "dv_pressure_flat"),
                                                 ("2020-07-27", 14, "dv_pressure_flat")])
def test_healthy_weeks_of_february_and_after_jun_8_raise_no_alert(profile, grid, real_service, start, days, regime):
    """The known shifts: light duty in February, and the summer after the Jun 8 repair (the
    flat channel itself is not in drift.signals). Each is judged against its own regime. The
    stretch from Jul 27 holds the Jul 31 window whose restarts read as drift before the gap
    edges were left out (TP3 0.287 against 0.280)."""
    docs = replay(real_service(), profile, grid, start, days)
    judged = [d for d in docs if d["signals"][0]["distance"] is not None]
    assert len(judged) >= 3 * days, len(judged)
    assert {e["regime"] for d in judged for e in d["signals"]} == {regime}
    assert not any(d["alert"] for d in docs), [(d["ts"], alerting(d)) for d in docs if d["alert"]]
    assert {e["signal"] for e in docs[-1]["signals"]} == set(profile["drift"]["signals"])


@needs_data
@pytest.mark.parametrize("start, regime", [("2020-02-10", "light_duty"), ("2020-05-04", "normal"),
                                           ("2020-06-29", "dv_pressure_flat")])
def test_a_half_bar_shift_on_the_panel_pressure_is_flagged_within_24_hours(profile, grid, real_service, start,
                                                                           regime):
    panel = panel_signal(profile)
    since = (pd.Timestamp(start) + pd.Timedelta(days=2, hours=3)).to_pydatetime()      # between checks
    docs = replay(real_service(), profile, grid, start, 4, shift=(panel, since, 0.5))
    assert not any(d["alert"] for d in docs if d["ts"] <= iso(since))
    first = next((d for d in docs if d["alert"]), None)
    assert first is not None, "the shift was not flagged"
    lag = pd.Timestamp(first["ts"]) - pd.Timestamp(iso(since))
    assert lag <= pd.Timedelta(hours=24), lag
    assert alerting(first) == [panel] and first["signals"][0]["regime"] == regime
    assert first["candidate"]["trigger"]["signals"][0]["signal"] == panel
