"""The Week-7 freeze (T30; DET-08, PR-09): the frozen bundles load only when
every pinned hash matches, a bundle scores exactly as the harness's PCA does,
and an unpinned run is refused."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml
from sklearn.decomposition import PCA

from pdm_common.features import features_file, load_feature_spec
from pdm_common.frozen import (
    DetectorModel,
    FrozenError,
    features_sha256,
    load_frozen,
    profile_detector_sha256,
)
from pdm_common.hashing import sha256_file
from pdm_common.profile import events_file, load_profile

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "eval" / "config.yaml"
PROFILE_PATH = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
PROCESSED = ROOT / "data" / "processed_v4"


def toy_bundle(X):
    pca = PCA(n_components=0.95, svd_solver="full", random_state=0).fit(X)
    median, mean, std = np.zeros(X.shape[1]), np.zeros(X.shape[1]), np.ones(X.shape[1])
    bundle = {"schema": "detector_bundle_v1", "features": [f"f{i}" for i in range(X.shape[1])],
              "scaler": {"healthy_median": median.tolist(), "mean": mean.tolist(), "std": std.tolist()},
              "model": {"kind": "pca", "components": pca.components_.tolist(), "mean": pca.mean_.tolist()},
              "threshold": {"kind": "empirical", "value": 1.0}}
    return pca, bundle


def test_a_bundle_scores_exactly_as_scikit_learn_pca():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(2000, 6)) @ rng.normal(size=(6, 6))
    pca, bundle = toy_bundle(X)
    model = DetectorModel(bundle)
    expected = ((X - pca.inverse_transform(pca.transform(X))) ** 2).sum(axis=1)
    assert np.allclose(model.score(X), expected, rtol=1e-12, atol=1e-12)
    errors = model.errors(X[:3])
    assert errors.shape == (3, 6) and np.allclose(errors.sum(axis=1), expected[:3])


def test_missing_features_take_the_healthy_median():
    rng = np.random.default_rng(1)
    X = rng.normal(size=(500, 4))
    _, bundle = toy_bundle(X)
    model = DetectorModel(bundle)
    row = model.row({"f0": 0.0, "f1": float("nan"), "f2": True, "f3": None})
    assert np.isnan(row[1:]).all()
    assert model.score(row)[0] == model.score(np.zeros(4))[0]


def test_the_repository_config_pins_the_current_inputs():
    """Anything that changes the detector's inputs after the freeze breaks this test."""
    frozen = yaml.safe_load(CONFIG.read_text())["frozen"]
    profile = load_profile(PROFILE_PATH)
    spec = load_feature_spec(features_file(profile, PROFILE_PATH))
    assert frozen["events_sha256"] == sha256_file(events_file(profile, PROFILE_PATH))
    assert frozen["profile_detector_sha256"] == profile_detector_sha256(profile)
    assert frozen["features_sha256"] == features_sha256(spec.raw)
    loaded = load_frozen(CONFIG, ROOT, profile=profile, events_path=events_file(profile, PROFILE_PATH),
                         spec_raw=spec.raw)
    assert loaded.detector.threshold == frozen["detector"]["threshold"]["value"]
    assert loaded.survival_bundle["schema"] == "survival_bundle_v1"


def test_profile_edits_outside_the_detector_sections_keep_the_hash():
    profile = load_profile(PROFILE_PATH)
    before = profile_detector_sha256(profile)
    edited = {**profile, "consistency_checks": [], "south": {"opcua": None}}
    assert profile_detector_sha256(edited) == before
    moved = {**profile, "gate": {**profile["gate"], "min_fraction": 0.7}}
    assert profile_detector_sha256(moved) != before


@pytest.fixture()
def copy_of_the_freeze(tmp_path):
    frozen = yaml.safe_load(CONFIG.read_text())["frozen"]
    for key in ("detector", "survival"):
        target = tmp_path / frozen[key]["bundle"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / frozen[key]["bundle"], target)
    (tmp_path / "eval").mkdir(exist_ok=True)
    shutil.copy(CONFIG, tmp_path / "eval" / "config.yaml")
    return tmp_path, frozen


def test_an_altered_bundle_is_refused(copy_of_the_freeze):
    root, frozen = copy_of_the_freeze
    path = root / frozen["detector"]["bundle"]
    bundle = json.loads(path.read_text())
    bundle["threshold"]["value"] *= 0.9                     # someone lowers the threshold by hand
    path.write_text(json.dumps(bundle))
    with pytest.raises(FrozenError, match="refusing to load an unpinned model"):
        load_frozen(root / "eval" / "config.yaml", root)


def test_another_run_id_is_refused(copy_of_the_freeze):
    root, _ = copy_of_the_freeze
    with pytest.raises(FrozenError, match="is not the frozen detector"):
        load_frozen(root / "eval" / "config.yaml", root, run_id="0123456789abcdef")


def test_changed_events_are_refused(copy_of_the_freeze, tmp_path):
    root, _ = copy_of_the_freeze
    events = tmp_path / "events.yaml"
    events.write_text((ROOT / "profiles/metropt3_apu/events.yaml").read_text() + "\n# edited\n")
    with pytest.raises(FrozenError, match="changed since the freeze"):
        load_frozen(root / "eval" / "config.yaml", root, events_path=events)


@pytest.mark.skipif(not (PROCESSED / "windows_10min.csv.gz").exists(), reason="needs data/processed_v4")
def test_evaluate_frozen_reproduces_the_logged_metrics():
    out = subprocess.run([sys.executable, str(ROOT / "eval/evaluate.py"), "--processed", str(PROCESSED), "--frozen"],
                         capture_output=True, text=True, timeout=600)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "matches the metrics logged at the freeze" in out.stdout
    refused = subprocess.run([sys.executable, str(ROOT / "eval/evaluate.py"), "--processed", str(PROCESSED),
                              "--frozen", "--run-id", "not-the-pinned-run"], capture_output=True, text=True, timeout=600)
    assert refused.returncode == 2 and "DET-08" in refused.stdout
