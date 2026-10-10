"""The frozen models and what they were frozen against (T30; DET-08, PR-09, NFR-05).

``eval/config.yaml`` holds the ``frozen:`` block: the detector and survival
bundles by path and SHA-256 (and their MLflow run IDs once registered), the
detector threshold, and the hashes of everything that determines the
detector's inputs -- the events file, the profile's detector-relevant
sections, the feature declarations, the window table's key and the raw
file's SHA-256. ``load_frozen`` checks every one of them and refuses to load
anything else, so the services and ``eval/evaluate.py --frozen`` score with
exactly the models that were evaluated (DET-08).

The detector bundle is plain JSON (``detector_bundle_v1``): the feature list,
the scaler (healthy median for missing values, mean and standard deviation),
the PCA subspace and the threshold. Scoring it needs numpy only, so a bundle
loads the same in the harness, the detector service and the agent's tools.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

from pdm_common.hashing import canonical_json, sha256_file

DETECTOR_SCHEMA = "detector_bundle_v1"
SURVIVAL_SCHEMA = "survival_bundle_v1"
PROFILE_SECTIONS = ("sampling", "gap_policy", "freeze_rule", "windowing", "gate", "detector", "signals")


class FrozenError(RuntimeError):
    """A frozen model is missing, altered, or not the one the configuration pins."""


def sha256_text(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


def profile_detector_sha256(profile: dict) -> str:
    """Hash of the profile sections that decide the detector's inputs and alarms;
    edits elsewhere in the profile (checks, southbound tags) leave it unchanged."""
    return sha256_text(canonical_json({k: profile.get(k) for k in PROFILE_SECTIONS}))


def features_sha256(spec_raw: dict) -> str:
    """Hash of what features.yaml computes: roles, window, inputs and each
    feature's name and compute block (descriptions may change)."""
    core = {"roles": spec_raw.get("roles"), "window": spec_raw.get("window"), "summary": spec_raw.get("summary"),
            "features": [{"name": f["name"], "compute": f["compute"]} for f in spec_raw.get("features", [])]}
    return sha256_text(canonical_json(core))


@dataclass
class DetectorModel:
    """Scores window features with a frozen detector bundle (PCA reconstruction)."""
    bundle: dict

    def __post_init__(self):
        b = self.bundle
        if b.get("schema") != DETECTOR_SCHEMA:
            raise FrozenError(f"not a {DETECTOR_SCHEMA} bundle")
        self.features = list(b["features"])
        self.median = np.array(b["scaler"]["healthy_median"], dtype=float)
        self.mean = np.array(b["scaler"]["mean"], dtype=float)
        self.std = np.array(b["scaler"]["std"], dtype=float)
        self.components = np.array(b["model"]["components"], dtype=float)
        self.center = np.array(b["model"]["mean"], dtype=float)
        self.threshold = float(b["threshold"]["value"])

    def standardize(self, X_raw) -> np.ndarray:
        X_raw = np.atleast_2d(np.asarray(X_raw, dtype=float))
        return (np.where(np.isnan(X_raw), self.median, X_raw) - self.mean) / self.std

    def errors(self, X_raw) -> np.ndarray:
        """Per-feature squared reconstruction error (DET-07): the attribution.
        Same operations, in the same order, as scikit-learn's PCA
        inverse_transform(transform(Z)), so the scores agree with the harness."""
        Z = self.standardize(X_raw)
        projected = Z @ self.components.T
        projected -= self.center.reshape(1, -1) @ self.components.T
        recon = projected @ self.components + self.center
        return (Z - recon) ** 2

    def score(self, X_raw) -> np.ndarray:
        return self.errors(X_raw).sum(axis=1)

    def row(self, features: dict) -> np.ndarray:
        """One window's features (a window_v1 ``features`` mapping) in bundle order;
        a missing or non-finite value becomes NaN and takes the healthy median."""
        out = []
        for name in self.features:
            v = features.get(name)
            out.append(float(v) if isinstance(v, (int, float)) and not isinstance(v, bool)
                       and np.isfinite(v) else np.nan)
        return np.array(out, dtype=float)


@dataclass
class Frozen:
    config: dict
    root: Path
    detector_bundle: dict
    survival_bundle: dict | None

    @property
    def detector(self) -> DetectorModel:
        return DetectorModel(self.detector_bundle)

    @property
    def detector_run_id(self) -> str:
        """The MLflow run ID when registered, else the bundle hash: the ID every
        score record carries (DET-06)."""
        d = self.config["detector"]
        return d.get("run_id") or "bundle-" + d["bundle_sha256"][:12]

    @property
    def survival_run_id(self) -> str | None:
        s = self.config.get("survival")
        if not s:
            return None
        return s.get("run_id") or "bundle-" + s["bundle_sha256"][:12]


def read_config(path) -> dict:
    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict) or "frozen" not in data:
        raise FrozenError(f"{path}: no frozen: block")
    return data


def _check_bundle(root: Path, entry: dict, schema: str, what: str) -> dict:
    path = root / entry["bundle"]
    if not path.exists():
        raise FrozenError(f"the frozen {what} bundle {path} is missing")
    actual = sha256_file(path)
    if actual != entry["bundle_sha256"]:
        raise FrozenError(f"the {what} bundle {path} has SHA-256 {actual[:12]}..., the freeze pinned "
                          f"{entry['bundle_sha256'][:12]}...; refusing to load an unpinned model (DET-08)")
    bundle = json.loads(path.read_text())
    if bundle.get("schema") != schema:
        raise FrozenError(f"{path}: expected a {schema} bundle")
    return bundle


def load_frozen(config_path, root=None, *, run_id: str | None = None, profile: dict | None = None,
                events_path=None, spec_raw: dict | None = None) -> Frozen:
    """Load and verify the frozen models.

    ``run_id``: refuse unless it is the pinned detector's run ID (DET-08).
    ``profile`` / ``events_path`` / ``spec_raw``: when given, refuse unless their
    hashes match what the freeze recorded (the inputs the detector was fitted on).
    """
    config_path = Path(config_path)
    root = Path(root) if root else config_path.resolve().parents[1]
    config = read_config(config_path)["frozen"]
    detector = _check_bundle(root, config["detector"], DETECTOR_SCHEMA, "detector")
    survival = _check_bundle(root, config["survival"], SURVIVAL_SCHEMA, "survival") if config.get("survival") else None
    frozen = Frozen(config, root, detector, survival)
    if run_id is not None and run_id not in {frozen.detector_run_id, config["detector"].get("run_id")}:
        raise FrozenError(f"run {run_id} is not the frozen detector ({frozen.detector_run_id}); "
                          "evaluation and the services load only the pinned run (DET-08)")
    if profile is not None and profile_detector_sha256(profile) != config["profile_detector_sha256"]:
        raise FrozenError("the profile's detector sections changed since the freeze")
    if events_path is not None and sha256_file(events_path) != config["events_sha256"]:
        raise FrozenError(f"{events_path} changed since the freeze")
    if spec_raw is not None and features_sha256(spec_raw) != config["features_sha256"]:
        raise FrozenError("features.yaml computes different features than at the freeze")
    return frozen
