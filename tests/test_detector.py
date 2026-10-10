"""Detector service (DET-03 to DET-08): window_v1 in, score_v1 out, with the frozen bundle only.

The data test streams recorded days through ingest's StreamWindows and the
service, and holds the scores to the frozen model applied to the evaluated
window table (data/processed_v4): the stream scores what was evaluated.
"""
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "detector"))
sys.path.insert(0, str(ROOT / "services" / "ingest"))

import detector as detector_service  # noqa: E402
import ingest as ingest_service  # noqa: E402
from pdm_common.features import features_file, load_feature_spec  # noqa: E402
from pdm_common.frozen import FrozenError  # noqa: E402
from pdm_common.hashing import canonical_json  # noqa: E402
from pdm_common.profile import analog_names, digital_names, load_profile, profile_hash  # noqa: E402
from pdm_common.store import Store  # noqa: E402

PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
RAW = ROOT / "data" / "MetroPT3(AirCompressor).csv"
PROCESSED = ROOT / "data" / "processed_v4"


@pytest.fixture(scope="module")
def profile():
    return load_profile(PROFILE_PATH)


@pytest.fixture(scope="module")
def frozen(profile):
    return detector_service.load_for_profile(profile, PROFILE_PATH)


@pytest.fixture()
def service(profile, frozen):
    store = Store("sqlite://")
    store.init_schema()
    return detector_service.Detector(profile, frozen, profile_hash(PROFILE_PATH), store)


def window(service, features, **override):
    doc = {"schema": "window_v1", "asset": service.asset, "features_sha256": service.features_sha256,
           "start": "2020-06-01T10:00:00Z", "end": "2020-06-01T10:10:00Z", "n_observed": 60, "n_bridged": 0,
           "out_of_range": {}, "profile_sha256": "p" * 64, "features": features}
    doc.update(override)
    return canonical_json(doc)


def healthy_features(frozen):
    """The healthy median of every frozen input: the point the scaler centres."""
    b = frozen.detector_bundle
    return dict(zip(b["features"], b["scaler"]["healthy_median"]))


def test_a_window_becomes_one_score(service, frozen):
    feats = healthy_features(frozen)
    out = service.handle(window(service, feats))
    assert len(out) == 1 and out[0].topic == service.scores_topic
    doc = json.loads(out[0].payload)
    expected = float(frozen.detector.score(frozen.detector.row(feats))[0])
    assert doc["score"] == pytest.approx(expected, rel=1e-12)
    assert doc["threshold"] == frozen.config["detector"]["threshold"]["value"]
    assert doc["alert"] is (expected > doc["threshold"])
    assert sorted(doc["errors"]) == sorted(frozen.detector.features)
    assert sum(doc["errors"].values()) == pytest.approx(doc["score"])
    assert doc["detector_run_id"] == frozen.detector_run_id and doc["imputed"] == []
    row = service.store.select("scores")[0]
    assert row["window_end"] == "2020-06-01T10:10:00Z" and row["score"] == pytest.approx(doc["score"])
    assert row["detector_run_id"] == frozen.detector_run_id


def test_an_anomalous_window_alerts_and_names_its_features(service, frozen):
    feats = healthy_features(frozen)
    name = frozen.detector.features[0]
    std = frozen.detector_bundle["scaler"]["std"][0]
    feats[name] += 30 * std
    doc = json.loads(service.handle(window(service, feats))[0].payload)
    assert doc["alert"] is True
    assert doc["top"][0][0] == name and 0 < doc["top"][0][1] <= 1


def test_missing_values_take_the_healthy_median_and_are_listed(service, frozen):
    feats = healthy_features(frozen)
    gone = frozen.detector.features[3]
    feats[gone] = None
    doc = json.loads(service.handle(window(service, feats))[0].payload)
    assert doc["imputed"] == [gone] and np.isfinite(doc["score"])


@pytest.mark.parametrize("override, reason", [
    ({"schema": "window_v0"}, "wrong_schema"),
    ({"asset": "another_asset"}, "unknown_asset"),
    ({"features_sha256": "0" * 64}, "features_changed"),
])
def test_windows_it_cannot_trust_are_refused(service, frozen, override, reason):
    assert service.handle(window(service, healthy_features(frozen), **override)) == []
    assert service.refused_by == {reason: 1} and service.counts["scored"] == 0
    assert service.store.count("scores") == 0


def test_it_loads_only_the_frozen_inputs(profile, tmp_path, frozen):
    with pytest.raises(FrozenError, match="not the frozen detector"):
        detector_service.load_for_profile(profile, PROFILE_PATH, run_id="some-other-run")
    # a profile folder whose events file changed after the freeze
    copy = tmp_path / "metropt3_apu"
    shutil.copytree(PROFILE_PATH.parent, copy)
    events = copy / "events.yaml"
    events.write_text(events.read_text() + "\n# edited after the freeze\n")
    with pytest.raises(FrozenError, match="changed since the freeze"):
        detector_service.load_for_profile(profile, copy / "asset_profile.yaml")


@pytest.mark.skipif(not (RAW.exists() and (PROCESSED / "windows_10min.csv.gz").exists()),
                    reason="needs the MetroPT-3 CSV and data/processed_v4")
def test_streamed_scores_equal_the_evaluated_ones(profile, frozen):
    """Apr 1-21, 2020 through ingest and the detector; from Apr 9 on every window's
    look-back lies inside the slice, so the scores must equal the frozen model on
    the evaluated table."""
    spec = load_feature_spec(features_file(profile, PROFILE_PATH))
    df = pd.read_csv(RAW, index_col=0, parse_dates=["timestamp"])
    rows = df[(df["timestamp"] >= "2020-04-01") & (df["timestamp"] < "2020-04-21 06:00")]
    ingest = ingest_service.Ingest(profile, profile_hash(PROFILE_PATH), spec=spec, telemetry_batch=10**9)
    det = detector_service.Detector(profile, frozen, profile_hash(PROFILE_PATH))
    cols = analog_names(profile) + digital_names(profile)
    outs = []

    def score(windows):
        for w in windows:
            if w.complete:
                outs.extend(det.handle(canonical_json(ingest.window_payload(w))))
    for i, (ts, values) in enumerate(zip(rows["timestamp"], rows[cols].to_numpy(float))):
        ingest.stream.append(ts, dict(zip(cols, values)))
        if i % 2160 == 2159:
            score(ingest.stream.compute())
    score(ingest.stream.flush())
    assert det.counts["refused"] == 0
    # the evaluated table, scored by the frozen model
    table = pd.read_csv(PROCESSED / "windows_10min.csv.gz", parse_dates=["window_start"])
    table = table[(table["window_start"] >= "2020-04-09") & (table["window_start"] < "2020-04-21")]
    reference = pd.Series(frozen.detector.score(table[frozen.detector.features].to_numpy(float)),
                          index=table["window_start"].dt.tz_localize("UTC"))
    docs = [json.loads(o.payload) for o in outs]
    streamed = pd.Series({pd.Timestamp(d["window_start"]): d["score"] for d in docs})
    common = reference.index.intersection(streamed.index)
    assert len(common) == len(reference) > 10_000
    rel = ((streamed[common] - reference[common]).abs() / reference[common].abs().clip(lower=1)).max()
    assert rel < 1e-4      # the table holds six significant digits per feature
    alerts = pd.Series({pd.Timestamp(d["window_start"]): d["alert"] for d in docs})[common]
    assert (alerts == (reference[common] > frozen.detector.threshold)).mean() > 0.999
