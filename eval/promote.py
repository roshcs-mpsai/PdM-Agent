"""Promote a registered model version, only with a recorded team approval (T25; OPS-07, PR-23).

    python eval/promote.py --model pdm-detector-metropt3_apu --version 3 --alias production \\
        --requested-by "Gayatri Suresh Chaudhari" --approved-by "Rosh Sreedharan" \\
        --approval-ref "team minutes 2026-11-24, item 3" [--store "$PDM_STORE"]

It refuses -- exit code 2, the reasons printed, and a ``promotions`` row with
the outcome ``refused: <reasons>`` -- when

* the evaluation lock in eval/config.yaml is on and today (UTC) is before its
  ``until`` date (the lock lapses on that date);
* the approver is not a team member (eval/approvers.yaml);
* the approver is the requester: two people are needed;
* the approval reference -- the minutes, pull request or issue where the team
  approved -- is empty.

Otherwise it records ``approved`` first and then, if MLflow is installed and
MLFLOW_TRACKING_URI answers, points the alias at the version; a failure there
adds a ``failed: ...`` row and exits 1. Without MLflow it records ``approved:
alias not moved (no MLflow here)`` and prints the exact command to run. The
store comes from --store or $PDM_STORE (default sqlite:///runs/pdm.db, as for
every service); ``promotions`` is insert-only, so every attempt stays on record.

Nothing else in the pipeline moves an alias: a drift alert or a new label ends
at a candidate version (eval/retrain_candidate.py), never in production (PR-23).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import sys
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from pdm_common.timeutil import iso_utc  # noqa: E402

CONFIG = ROOT / "eval" / "config.yaml"
APPROVERS = ROOT / "eval" / "approvers.yaml"
NO_MLFLOW = "approved: alias not moved (no MLflow here)"


def _norm(name) -> str:
    return " ".join(str(name or "").split()).casefold()


def load_approvers(path=APPROVERS) -> dict[str, str]:
    """Every accepted spelling (normalised) -> the approver's name as listed."""
    data = yaml.safe_load(Path(path).read_text()) or {}
    names = {}
    for entry in data.get("approvers") or []:
        for spelling in [entry["name"], *(entry.get("also") or [])]:
            names[_norm(spelling)] = entry["name"]
    return names


def lock_reason(config_path=CONFIG, today: dt.date | None = None) -> str | None:
    """Why the evaluation lock forbids a promotion today, or None. An unreadable lock refuses."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    try:
        config = yaml.safe_load(Path(config_path).read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        return f"cannot read the evaluation lock in {config_path}: {exc}"
    lock = config.get("evaluation_lock") or {}
    if not lock.get("locked"):
        return None
    until = lock.get("until")
    try:
        if isinstance(until, dt.datetime):
            until = until.date()
        elif not isinstance(until, dt.date):
            until = dt.date.fromisoformat(str(until))
    except ValueError:
        return f"the evaluation lock in {config_path} is on with no valid until date ({until!r})"
    if today < until:
        return f"the evaluation lock is on until {until.isoformat()} ({Path(config_path).name})"
    return None


def refusal_reasons(version, alias, requested_by, approved_by, approval_ref, *, config_path=CONFIG,
                    approvers_path=APPROVERS, today: dt.date | None = None) -> list[str]:
    reasons = []
    lock = lock_reason(config_path, today)
    if lock:
        reasons.append(lock)
    try:
        team = load_approvers(approvers_path)
    except (OSError, yaml.YAMLError, KeyError, TypeError) as exc:
        team = {}
        reasons.append(f"cannot read the approvers in {approvers_path}: {exc}")
    if not str(version).strip().isdigit():
        reasons.append(f"version {version!r} is not a registered version number")
    if not str(alias or "").strip():
        reasons.append("no alias to move")
    if not _norm(requested_by):
        reasons.append("no requester")
    approver = team.get(_norm(approved_by))
    if approver is None:
        reasons.append(f"{approved_by!r} is not a team member who can approve ({Path(approvers_path).name})")
    elif _norm(requested_by) and team.get(_norm(requested_by), _norm(requested_by)) == approver:
        reasons.append(f"{approved_by!r} requested the promotion and cannot approve it: two people are needed")
    if not str(approval_ref or "").strip():
        reasons.append("no approval reference (the minutes, pull request or issue that records the approval)")
    return reasons


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
        except Exception as exc:
            return False, f"{uri} does not answer ({exc})"
    return True, uri


def mlflow_command(model: str, alias: str, version) -> str:
    args = ", ".join(json.dumps(str(a)) for a in (model, alias, version))
    code = f"from mlflow import MlflowClient; MlflowClient().set_registered_model_alias({args})"
    uri = os.environ.get("MLFLOW_TRACKING_URI") or "http://localhost:5000"
    return f"MLFLOW_TRACKING_URI={shlex.quote(uri)} python -c {shlex.quote(code)}"


def promote(model: str, version, alias: str, requested_by: str, approved_by: str, approval_ref: str, *, store,
            config_path=CONFIG, approvers_path=APPROVERS, today: dt.date | None = None, client=None,
            say=print) -> tuple[int, str]:
    """(exit code, outcome). Every call inserts at least one promotions row. ``client``: an
    MLflow client to use instead of connecting (tests)."""
    def record(outcome: str) -> None:
        store.insert("promotions", {"model_name": model, "version": str(version), "alias": alias,
                                    "requested_by": requested_by, "approved_by": approved_by,
                                    "approval_ref": approval_ref, "at": iso_utc(dt.datetime.now(dt.timezone.utc)),
                                    "outcome": outcome})

    reasons = refusal_reasons(version, alias, requested_by, approved_by, approval_ref, config_path=config_path,
                              approvers_path=approvers_path, today=today)
    if reasons:
        outcome = "refused: " + "; ".join(reasons)
        record(outcome)
        say(f"promotion of {model} version {version} to {alias} refused:\n  - " + "\n  - ".join(reasons))
        return 2, outcome
    where = "a client was given"
    if client is None:
        reachable, where = mlflow_target()
        if reachable:
            from mlflow import MlflowClient
            client = MlflowClient(tracking_uri=where)
    if client is None:
        record(NO_MLFLOW)
        say(f"approved by {approved_by} ({approval_ref}) and recorded; the alias was not moved: {where}.\n"
            f"Move it where the registry is reachable with:\n  {mlflow_command(model, alias, version)}")
        return 0, NO_MLFLOW
    record("approved")
    try:
        client.set_registered_model_alias(model, alias, str(version))
    except Exception as exc:                      # the approval stands; the move did not happen
        outcome = f"failed: alias not moved ({type(exc).__name__}: {exc})"
        record(outcome)
        say(f"approved and recorded, but MLflow refused to move {alias}: {exc}")
        return 1, outcome
    say(f"approved by {approved_by} ({approval_ref}) and recorded; {alias} -> {model} version {version}")
    return 0, "approved"


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--model", required=True, help="registered model name, e.g. pdm-detector-metropt3_apu")
    p.add_argument("--version", required=True, help="the registered version to promote")
    p.add_argument("--alias", default="production")
    p.add_argument("--requested-by", required=True)
    p.add_argument("--approved-by", required=True, help="a team member other than the requester")
    p.add_argument("--approval-ref", required=True, help="where the approval is recorded: minutes, PR or issue")
    p.add_argument("--store", default=None, help="store URL (default $PDM_STORE or sqlite:///runs/pdm.db)")
    p.add_argument("--config", default=str(CONFIG), help="the freeze and its evaluation lock")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    from pdm_common.store import open_store
    store = open_store(args.store)
    code, outcome = promote(args.model, args.version, args.alias, args.requested_by, args.approved_by,
                            args.approval_ref, store=store, config_path=args.config)
    print(f"promotions row recorded in {store.url}: {outcome}")
    return code


if __name__ == "__main__":
    sys.exit(main())
