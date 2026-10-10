"""Register a model bundle in MLflow (T29; OPS-05, PR-23).

    MLFLOW_TRACKING_URI=http://localhost:5000 python tools/register_bundle.py \\
        eval/frozen/metropt3_apu/detector --name pdm-detector-metropt3_apu --alias production
    python tools/register_bundle.py data/processed_v4/detector_selection --dry-run   # what would be logged

A bundle is a folder with one JSON record and the files beside it:

* ``bundle.json`` with ``schema: detector_bundle_v1`` or ``survival_bundle_v1``
  (written by ``eval/evaluate.py --save-bundle`` and ``eval/prognostics.py``),
* or a notebook ``selection.json`` (the Oct 4 detector selection) -- the
  Colab bundle import, so a run made in Colab reaches the registry,
* or a deep model's saved scores (``scores_*.json`` beside ``scores_*.npy``).

The script starts a run in the experiment (``detector-selection`` by default),
logs the record's parameters and metrics, logs every file in the folder as an
artifact under ``bundle/``, and, with ``--name``, logs the bundle as an MLflow
pyfunc model and registers it under that name; ``--alias`` then points an
alias (``production``) at the new version. It prints the run ID, which goes
into ``eval/config.yaml`` (T30). ``--dry-run`` prints the parameters, metrics
and artifacts without MLflow, so the mapping is testable anywhere.

MLflow refuses nothing here on its own: promotion of a production alias
after the freeze goes through ``eval/promote.py`` (OPS-07), which checks for
a recorded team approval.
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from pdm_common.hashing import sha256_file  # noqa: E402

MAX_PARAM = 500        # MLflow's limit on a parameter value


def read_record(folder: Path) -> tuple[str, dict, Path]:
    """(kind, record, record path) for a bundle folder."""
    for name in ("bundle.json", "selection.json"):
        path = folder / name
        if path.exists():
            record = json.loads(path.read_text())
            kind = record.get("schema", "selection_json") if name == "bundle.json" else "selection_json"
            return kind, record, path
    scores = sorted(folder.glob("scores_*.json"))
    if scores:
        return "scores", json.loads(scores[0].read_text()), scores[0]
    raise SystemExit(f"{folder}: no bundle.json, selection.json or scores_*.json to register")


def _flat(prefix: str, value, out: dict) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _flat(f"{prefix}.{k}" if prefix else str(k), v, out)
    elif isinstance(value, (list, tuple)):
        out[prefix] = json.dumps(value)[:MAX_PARAM]
    else:
        out[prefix] = value


def params_and_metrics(kind: str, record: dict) -> tuple[dict, dict]:
    """What the run logs for each kind of record."""
    params, metrics = {}, {}
    if kind in ("detector_bundle_v1", "selection_json"):
        threshold = record.get("threshold")
        if isinstance(threshold, dict):
            params["threshold"] = threshold.get("value")
            params["threshold_kind"] = threshold.get("kind")
            if threshold.get("spot"):
                _flat("spot", {k: v for k, v in threshold["spot"].items() if k != "scan"}, params)
        else:
            params["threshold"] = threshold
            params["threshold_kind"] = "empirical"
        params["detector"] = record.get("selected") or record.get("detector")
        _flat("gate", record.get("gate", {}), params)
        params["feature_count"] = len(record.get("features", []))
        _flat("inputs", {k: v for k, v in record.get("inputs", {}).items()
                         if k in ("windows_key", "fit_key", "source_sha256", "fit_windows")}, params)
        far = record.get("held_out_far") or (record.get("metrics") or {}).get("held_out_far")
        if far:
            metrics.update(held_out_far=far[0], held_out_far_lo=far[1], held_out_far_hi=far[2])
        leads = record.get("lead_h") or (record.get("metrics") or {}).get("lead_h") or {}
        for ev, lead in leads.items():
            if lead is not None:
                metrics[f"lead_h_{ev}"] = lead
    elif kind == "survival_bundle_v1":
        params["model"] = record.get("model", {}).get("kind")
        _flat("features", {"names": record.get("features", [])}, params)
        _flat("inputs", record.get("inputs", {}), params)
        params["horizons_h"] = json.dumps(record.get("horizons_h"))
        for key, value in (record.get("metrics") or {}).items():
            if isinstance(value, (int, float)) and value is not None:
                metrics[key] = value
    elif kind == "scores":
        for key, value in record.items():
            if isinstance(value, (int, float, str)) and not isinstance(value, bool):
                params[key] = value
    params = {k: (str(v)[:MAX_PARAM] if v is not None else "none") for k, v in params.items()}
    return params, metrics


def describe(folder: Path) -> dict:
    kind, record, path = read_record(folder)
    params, metrics = params_and_metrics(kind, record)
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    return {"kind": kind, "record": str(path), "record_sha256": sha256_file(path), "params": params,
            "metrics": metrics, "artifacts": [str(p.relative_to(folder)) for p in files]}


def _log_model(mlflow, folder: Path, record_path: Path, name: str):
    """Log the bundle as a pyfunc model (MLflow 2 takes artifact_path, 3 takes name)."""
    import mlflow.pyfunc

    class PdmBundle(mlflow.pyfunc.PythonModel):
        def load_context(self, context):
            self.bundle = json.loads(Path(context.artifacts["bundle"]).read_text())

        def predict(self, context, model_input, params=None):
            return self.bundle

    kwargs = {"python_model": PdmBundle(), "artifacts": {"bundle": str(record_path)},
              "registered_model_name": name}
    if "name" in inspect.signature(mlflow.pyfunc.log_model).parameters:
        kwargs["name"] = "model"
    else:
        kwargs["artifact_path"] = "model"
    return mlflow.pyfunc.log_model(**kwargs)


def register(folder: Path, experiment: str, name: str | None, alias: str | None, run_name: str | None) -> str:
    import mlflow
    from mlflow.tracking import MlflowClient

    desc = describe(folder)
    if os.environ.get("MLFLOW_TRACKING_URI"):
        mlflow.set_tracking_uri(os.environ["MLFLOW_TRACKING_URI"])
    mlflow.set_experiment(experiment)
    with mlflow.start_run(run_name=run_name or desc["params"].get("detector") or folder.name) as run:
        mlflow.log_params(desc["params"])
        if desc["metrics"]:
            mlflow.log_metrics(desc["metrics"])
        mlflow.set_tags({"pdm.bundle_kind": desc["kind"], "pdm.record_sha256": desc["record_sha256"],
                         "pdm.source_folder": str(folder)})
        mlflow.log_artifacts(str(folder), artifact_path="bundle")
        if name:
            _log_model(mlflow, folder, Path(desc["record"]), name)
        run_id = run.info.run_id
    if name and alias:
        client = MlflowClient()
        versions = client.search_model_versions(f"name='{name}' and run_id='{run_id}'")
        version = max(int(v.version) for v in versions)
        client.set_registered_model_alias(name, alias, str(version))
        print(f"alias {alias} -> {name} version {version}")
    return run_id


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("folder", help="bundle folder")
    p.add_argument("--experiment", default="detector-selection")
    p.add_argument("--name", help="register the bundle as this model (e.g. pdm-detector-metropt3_apu)")
    p.add_argument("--alias", help="point this alias (e.g. production) at the new version")
    p.add_argument("--run-name")
    p.add_argument("--dry-run", action="store_true", help="print what would be logged; no MLflow needed")
    args = p.parse_args(argv)
    folder = Path(args.folder)
    if args.dry_run:
        print(json.dumps(describe(folder), indent=2))
        return 0
    run_id = register(folder, args.experiment, args.name, args.alias, args.run_name)
    print(f"run_id {run_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
