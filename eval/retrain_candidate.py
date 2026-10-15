"""Candidate training run from a drift alert or a newly labelled event (T25; OPS-06, PR-23).

    python eval/retrain_candidate.py --request runs/metropt3_apu/candidates/requests/CAND-<...>.json \\
        --processed data/processed_v4
    python eval/retrain_candidate.py --trigger labelled_event --event F4 --reason "label moved" \\
        --processed data/processed_v4 --dry-run

Fits a candidate detector the way the harness and the freeze do (eval/harness.py,
eval/evaluate.py, eval/detectors.py): the processed folder's windows, their
fit / calibrate / check roles, scaling on the fit windows, PCA on the fit
windows, the empirical threshold on the calibrate weeks at the budget of two
gated incidents a week and the SPOT threshold with the freeze rule, all judged
on the held-out check weeks and the leave-one-event-out blocks. It writes

    runs/<asset>/candidates/<UTC time>/bundle.json      detector_bundle_v1 plus a ``candidate`` block
    runs/<asset>/candidates/<UTC time>/candidate.json   the run record: trigger, data, the frozen
                                                        detector's figures beside the candidate's,
                                                        and where it was registered

and registers the folder in MLflow with tools/register_bundle.py -- a new
version of pdm-detector-<asset>, no alias -- when MLflow is installed and
MLFLOW_TRACKING_URI answers. Otherwise, or with --dry-run, register_bundle's
dry-run output is saved beside the bundle (register_dry_run.json).

It never writes eval/config.yaml or anything under eval/, and never sets an
alias: a candidate is a non-production version. Only eval/promote.py, with a
recorded team approval and outside the evaluation lock, moves production
(OPS-07, PR-23).
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import sys
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "eval"))

from pdm_common.events import load_events  # noqa: E402
from pdm_common.hashing import sha256_file  # noqa: E402
from pdm_common.profile import events_file, load_profile  # noqa: E402

PROFILE = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
CONFIG = ROOT / "eval" / "config.yaml"
RECORD_SCHEMA = "candidate_run_v1"
REQUEST_SCHEMA = "candidate_request_v1"
TRIGGERS = ("drift", "labelled_event")


def mlflow_target() -> tuple[bool, str]:
    """(reachable, URI or the reason it is not): MLflow installed and MLFLOW_TRACKING_URI answering."""
    try:
        import mlflow  # noqa: F401
    except ImportError:
        return False, "mlflow is not installed here"
    uri = os.environ.get("MLFLOW_TRACKING_URI")
    if not uri:
        return False, "MLFLOW_TRACKING_URI is not set"
    if uri.startswith(("http://", "https://")):
        try:
            urllib.request.urlopen(uri.rstrip("/") + "/health", timeout=3)
        except Exception as exc:                    # refused, timed out, HTTP error
            return False, f"{uri} does not answer ({exc})"
    return True, uri


def _register_bundle():
    spec = importlib.util.spec_from_file_location("register_bundle", ROOT / "tools" / "register_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the trigger --------------------------------------------------------------------------------

def read_trigger(args, profile: dict, events: dict) -> dict:
    asset = profile["asset"]["id"]
    if args.request:
        path = Path(args.request)
        try:
            request = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise SystemExit(f"{path}: cannot read the request: {exc}")
        if not isinstance(request, dict) or request.get("schema") != REQUEST_SCHEMA or request.get("asset") != asset:
            raise SystemExit(f"{path}: not a {REQUEST_SCHEMA} for asset {asset}")
        return dict(request["trigger"], request=str(path), request_id=request.get("request_id"))
    if args.trigger not in TRIGGERS:
        raise SystemExit("give --request (a drift service request) or --trigger drift|labelled_event")
    trigger = {"kind": args.trigger, "reason": args.reason, "request": None}
    if args.trigger == "labelled_event":
        if not args.event:
            raise SystemExit("--trigger labelled_event needs --event (a failure id or expert number in events.yaml)")
        failures = {f["id"]: f for f in events.get("failures") or []}
        expert = {str(e["n"]): e for e in events.get("expert_reported") or []}
        found = failures.get(args.event) or expert.get(args.event.lstrip("#"))
        if found is None:
            raise SystemExit(f"{args.event} is neither a failure id nor an expert-reported number in events.yaml; "
                             "label it there first")
        trigger["event"] = {"id": args.event, "start": str(found.get("start")), "end": str(found.get("end"))}
    return trigger


# --- the fit, as the harness and the freeze do it -----------------------------------------------------

def fit_candidate(processed: Path, profile: dict, budget: float, init_quantile: float):
    """PCA on the fit windows, empirical and SPOT thresholds, the freeze rule: (bundle, data)."""
    import detectors as dets
    import evaluate
    import harness as h
    from pdm_common.gate import gate_params

    data = h.load(processed)
    roles = h.assign_roles(data.windows)
    features = h.detector_features(data.feature_dict)
    scaled = h.scale(data.windows, features, roles)
    ctx = h.context(data, roles, gate_params(profile), budget=budget)
    det = dets.tabular_detectors()["pca"]
    det.fit(scaled.X[scaled.fit_rows])
    scores = det.score(scaled.X)
    failures = list(data.failures)
    opts = argparse.Namespace(budget=budget, init_quantile=init_quantile)
    results = {det.name: h.evaluate(scores, ctx)}
    spot = evaluate.spot_threshold(scores, ctx, opts)
    spot_name = f"{det.name} + SPOT"
    results[spot_name] = h.evaluate(scores, ctx, threshold=spot["threshold"])
    r = results[spot_name]
    caught = sum(not np.isnan(r[f"{ev} lead"]) for ev in failures)
    use_spot = bool(r["far hi"] <= budget and caught == len(failures))
    spot["freeze_rule"] = {"far_interval_within_budget": bool(r["far hi"] <= budget), "caught": caught,
                           "use_spot": use_spot}
    r["spot"] = spot
    bundle = evaluate.detector_bundle(det, det.name, results, use_spot, scaled, data, ctx, opts, failures,
                                      profile["asset"]["id"])
    print(f"fitted {det.name} on {int(scaled.fit_rows.sum()):,} fit windows of {processed} "
          f"(windows key {data.windows_key})")
    return bundle, data


def frozen_figures() -> dict:
    """The frozen detector's pinned figures, for the reviewers' comparison (read only)."""
    import yaml
    config = yaml.safe_load(CONFIG.read_text())["frozen"]["detector"]
    path = ROOT / config["bundle"]
    bundle = json.loads(path.read_text()) if path.exists() else {}
    return {"bundle": config["bundle"], "bundle_sha256": config["bundle_sha256"], "run_id": config.get("run_id"),
            "threshold": config.get("threshold"), "metrics": _metrics(bundle), "inputs": bundle.get("inputs", {})}


def _metrics(bundle: dict) -> dict:
    m = bundle.get("metrics") or {}
    return {k: m.get(k) for k in ("held_out_far", "far_with_uncertain", "lead_h", "lead_h_median")}


# --- output ------------------------------------------------------------------------------------------

def check_out_root(out_root: Path) -> None:
    resolved = out_root.resolve()
    if resolved == (ROOT / "eval").resolve() or (ROOT / "eval").resolve() in resolved.parents:
        raise SystemExit(f"{out_root}: a candidate never goes under eval/ (eval/config.yaml and eval/frozen "
                         "hold the frozen models); use runs/<asset>/candidates")


def candidate_folder(out_root: Path) -> Path:
    check_out_root(out_root)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder, k = out_root / stamp, 1
    while folder.exists():
        k += 1
        folder = out_root / f"{stamp}-{k}"
    folder.mkdir(parents=True)
    return folder


def register(folder: Path, name: str, experiment: str, trigger: dict) -> dict:
    """A new version of ``name`` with no alias, tagged as a candidate."""
    from mlflow.tracking import MlflowClient

    run_id = _register_bundle().register(folder, experiment, name, None, f"candidate {folder.name}")
    client = MlflowClient()
    client.set_tag(run_id, "pdm.candidate", "true")
    client.set_tag(run_id, "pdm.trigger", trigger["kind"])
    versions = client.search_model_versions(f"name='{name}' and run_id='{run_id}'")
    version = max(int(v.version) for v in versions) if versions else None
    if version is not None:
        client.set_model_version_tag(name, str(version), "pdm.stage", "candidate")
    return {"registered": True, "run_id": run_id, "model": name, "version": version, "alias": None}


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--request", help="a candidate request written by services/drift (candidate_request_v1)")
    p.add_argument("--trigger", choices=TRIGGERS, help="without --request: what triggered the run")
    p.add_argument("--event", help="with --trigger labelled_event: the failure id or expert number in events.yaml")
    p.add_argument("--reason", default=None, help="free text kept in the record")
    p.add_argument("--processed", required=True, help="processed folder to fit on (windows_10min.csv.gz, ...)")
    p.add_argument("--profile", default=str(PROFILE))
    p.add_argument("--out", default=None, help="default runs/<asset>/candidates")
    p.add_argument("--experiment", default="candidates")
    p.add_argument("--budget", type=float, default=2.0, help="false alarms per week (PRD G1)")
    p.add_argument("--init-quantile", type=float, default=0.98, help="SPOT's starting quantile, as the freeze")
    p.add_argument("--dry-run", action="store_true", help="no MLflow: save register_bundle's dry-run output")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    profile = load_profile(args.profile)
    asset = profile["asset"]["id"]
    events = load_events(events_file(profile, args.profile))
    trigger = read_trigger(args, profile, events)
    config_before = sha256_file(CONFIG)
    out_root = Path(args.out) if args.out else ROOT / "runs" / asset / "candidates"
    check_out_root(out_root)

    bundle, data = fit_candidate(Path(args.processed), profile, args.budget, args.init_quantile)
    folder = candidate_folder(out_root)
    created = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    bundle["candidate"] = {"status": "candidate", "production": False, "created_at": created, "trigger": trigger}
    (folder / "bundle.json").write_text(json.dumps(bundle, sort_keys=True, indent=1) + "\n")
    frozen = frozen_figures()
    model = f"pdm-detector-{asset}"
    record = {
        "schema": RECORD_SCHEMA, "asset": asset, "created_at": created, "status": "candidate", "production": False,
        "trigger": trigger,
        "processed": {"path": str(args.processed), "manifest_sha256": sha256_file(Path(args.processed) / "manifest.json"),
                      "windows_key": data.windows_key, "fit_key": bundle["inputs"]["fit_key"],
                      "catalogue": (data.catalogue or {}).get("dataset")},
        "bundle": {"path": "bundle.json", "sha256": sha256_file(folder / "bundle.json"),
                   "threshold": {k: bundle["threshold"][k] for k in ("kind", "value")}, "metrics": _metrics(bundle)},
        "frozen": dict(frozen, same_fit_windows=frozen["inputs"].get("fit_key") == bundle["inputs"]["fit_key"]
                       and frozen["inputs"].get("windows_key") == data.windows_key),
        "registration": None,
        "promotion": (f"not by this pipeline: python eval/promote.py --model {model} --version <version> "
                      "--alias production --requested-by <name> --approved-by <another team member> "
                      "--approval-ref <minutes or link> (OPS-07)"),
    }
    record["frozen"].pop("inputs")
    (folder / "candidate.json").write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")

    reachable, where = (False, "--dry-run") if args.dry_run else mlflow_target()
    if reachable:
        record["registration"] = register(folder, model, args.experiment, trigger)
        print(f"registered {model} version {record['registration']['version']} (run {record['registration']['run_id']}),"
              " no alias: a non-production version")
    else:
        dry = _register_bundle().describe(folder)
        (folder / "register_dry_run.json").write_text(json.dumps(dry, indent=2) + "\n")
        record["registration"] = {"registered": False, "reason": where, "dry_run": "register_dry_run.json",
                                  "command": f"MLFLOW_TRACKING_URI=<registry> python tools/register_bundle.py {folder} "
                                             f"--name {model} --experiment {args.experiment}"}
        print(f"not registered ({where}); register_bundle's dry run is in {folder / 'register_dry_run.json'}")
    if sha256_file(CONFIG) != config_before:
        raise SystemExit(f"{CONFIG} changed during the candidate run; it must not")
    record["config_unchanged"] = True
    (folder / "candidate.json").write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")

    m, f = record["bundle"]["metrics"], frozen["metrics"]
    print(f"candidate {folder}: {bundle['threshold']['kind']} threshold {bundle['threshold']['value']:.4f}, held-out "
          f"FAR {m['held_out_far']} a week, leads {m['lead_h']} h")
    print(f"frozen    {frozen['bundle']}: held-out FAR {f['held_out_far']} a week, leads {f['lead_h']} h"
          + (" (fitted on the same windows)" if record["frozen"]["same_fit_windows"] else ""))
    print("production alias unchanged; promotion only through eval/promote.py with a recorded team approval")
    return 0


if __name__ == "__main__":
    sys.exit(main())
