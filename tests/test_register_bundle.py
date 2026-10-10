"""Registering a bundle in MLflow (T29; OPS-05). The mapping from a bundle to
parameters, metrics and artifacts is tested everywhere; the MLflow round trip
runs where MLflow is installed."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("register_bundle", ROOT / "tools" / "register_bundle.py")
register_bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(register_bundle)

SELECTION = {
    "selected": "PCA", "threshold": 2.2335, "budget_per_week": 2.0,
    "gate": {"span": "30min", "min_fraction": 0.8, "hold_off": "6h", "min_present": 15},
    "features": ["a", "b", "c"], "held_out_far": [1.269, 0.527, 2.16],
    "lead_h": {"F1": -0.8, "F2": -0.17, "F3": -0.22, "F4": 13.83},
    "inputs": {"windows_key": "dbc70407da5491e1", "fit_key": "475a701621efc708", "source_sha256": "db30",
               "windows": 239172, "fit_windows": 73793},
}


@pytest.fixture()
def bundle(tmp_path):
    folder = tmp_path / "detector_selection"
    folder.mkdir()
    (folder / "selection.json").write_text(json.dumps(SELECTION))
    (folder / "comparison.csv").write_text("detector,far\nPCA,1.27\n")
    return folder


def test_a_notebook_selection_maps_to_params_metrics_and_artifacts(bundle):
    desc = register_bundle.describe(bundle)
    assert desc["kind"] == "selection_json"
    assert desc["params"]["detector"] == "PCA"
    assert desc["params"]["threshold"] == "2.2335"
    assert desc["params"]["gate.span"] == "30min" and desc["params"]["gate.min_present"] == "15"
    assert desc["params"]["feature_count"] == "3"
    assert desc["params"]["inputs.windows_key"] == "dbc70407da5491e1"
    assert desc["metrics"] == {"held_out_far": 1.269, "held_out_far_lo": 0.527, "held_out_far_hi": 2.16,
                               "lead_h_F1": -0.8, "lead_h_F2": -0.17, "lead_h_F3": -0.22, "lead_h_F4": 13.83}
    assert desc["artifacts"] == ["comparison.csv", "selection.json"]


def test_a_detector_bundle_with_a_spot_threshold(tmp_path):
    record = {**SELECTION, "schema": "detector_bundle_v1", "detector": "PCA",
              "threshold": {"kind": "spot", "value": 5.26, "spot": {"q": 0.01, "fit": {"gamma": 1.46}, "scan": [1, 2]}}}
    record.pop("selected")
    (tmp_path / "bundle.json").write_text(json.dumps(record))
    desc = register_bundle.describe(tmp_path)
    assert desc["kind"] == "detector_bundle_v1"
    assert desc["params"]["threshold_kind"] == "spot" and desc["params"]["spot.q"] == "0.01"
    assert "spot.scan" not in desc["params"]


def test_dry_run_needs_no_mlflow(bundle, capsys):
    assert register_bundle.main([str(bundle), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["params"]["detector"] == "PCA"


def test_an_empty_folder_is_refused(tmp_path):
    with pytest.raises(SystemExit, match="nothing|no bundle.json"):
        register_bundle.describe(tmp_path)


def test_round_trip_through_mlflow(bundle, tmp_path, monkeypatch):
    mlflow = pytest.importorskip("mlflow")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "sqlite:///" + str(tmp_path / "mlflow.db"))
    run_id = register_bundle.register(bundle, "detector-selection", None, None, None)
    run = mlflow.get_run(run_id)
    assert run.data.params["detector"] == "PCA"
    assert run.data.metrics["lead_h_F4"] == pytest.approx(13.83)
    assert run.data.tags["pdm.bundle_kind"] == "selection_json"
