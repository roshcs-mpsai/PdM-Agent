"""Detector -- score each window with the frozen model (DET-01, DET-03 to DET-08).

Subscribes to the profile's windows topic. Each window_v1 is scored with the
detector bundle that eval/config.yaml pins (the Week-7 freeze: PCA at the SPOT
threshold) and becomes one score_v1 on the scores topic and one row in the
store's scores table:

* ``score``: the sum of the per-feature squared reconstruction errors on the
  standardised features, exactly as the harness scored them;
* ``errors``: the per-feature terms, the attribution the agent and the
  dashboard read (DET-07);
* ``threshold`` and ``alert`` (score > threshold); the persistence gate
  decides what an alert means (services/gate);
* ``detector_run_id``: the MLflow run of the frozen bundle, or its hash until
  it is registered (DET-06).

The service loads only the pinned bundle and refuses to start when the bundle,
the profile's detector sections, events.yaml or the feature declarations
differ from what was frozen (DET-08, pdm_common.frozen). A window whose
``features_sha256`` is not the frozen one is refused and counted, never
scored: those features were computed by different code. A missing feature
value takes the healthy median, as in evaluation, and the score lists it
under ``imputed``.

    python services/detector/detector.py --profile profiles/metropt3_apu/asset_profile.yaml
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

from pdm_common.features import features_file
from pdm_common.frozen import Frozen, FrozenError, load_frozen
from pdm_common.hashing import canonical_json
from pdm_common.mqtt import Out, run_service
from pdm_common.profile import ProfileError, events_file, load_profile, profile_hash, read_yaml

log = logging.getLogger("detector")

ROOT = Path(__file__).resolve().parents[2]
SCORE_SCHEMA = "score_v1"
TOP_N = 5


class Detector:
    """The service minus MQTT: window_v1 in, score_v1 out."""

    def __init__(self, profile: dict, frozen: Frozen, profile_sha256: str, store=None):
        self.asset = profile["asset"]["id"]
        if frozen.config.get("asset") not in (None, self.asset):
            raise FrozenError(f"the frozen models are for {frozen.config['asset']}, not {self.asset}")
        self.scores_topic = profile["topics"]["scores"]
        self.model = frozen.detector
        self.detector = frozen.config["detector"].get("detector", "PCA")
        self.run_id = frozen.detector_run_id
        self.features_sha256 = frozen.config["features_sha256"]
        self.profile_sha256 = profile_sha256
        self.store = store
        self.counts = Counter({"windows": 0, "scored": 0, "refused": 0, "alerts": 0})
        self.refused_by: Counter = Counter()

    def _refuse(self, reason: str, detail: str) -> list[Out]:
        self.counts["refused"] += 1
        self.refused_by[reason] += 1
        log.warning("refused window (%s): %s", reason, detail)
        return []

    def score(self, window: dict) -> dict:
        """The score_v1 document for one window_v1 document."""
        features = window["features"]
        row = self.model.row(features)
        errors = self.model.errors(row)[0]
        total = float(errors.sum())
        threshold = self.model.threshold
        imputed = [n for n, v in zip(self.model.features, row) if math.isnan(v)]
        per_feature = {n: float(e) for n, e in zip(self.model.features, errors)}
        top = sorted(per_feature.items(), key=lambda kv: -kv[1])[:TOP_N]
        return {
            "schema": SCORE_SCHEMA,
            "asset": self.asset,
            "window_start": window["start"],
            "window_end": window["end"],
            "score": total,
            "threshold": threshold,
            "alert": total > threshold,
            "errors": per_feature,
            "top": [[n, (e / total) if total > 0 else 0.0] for n, e in top],
            "imputed": imputed,
            "detector": self.detector,
            "detector_run_id": self.run_id,
            "profile_sha256": self.profile_sha256,
            "features_sha256": self.features_sha256,
        }

    def handle(self, payload: bytes | str) -> list[Out]:
        self.counts["windows"] += 1
        try:
            window = json.loads(payload)
        except ValueError as exc:
            return self._refuse("bad_json", str(exc))
        if not isinstance(window, dict) or window.get("schema") != "window_v1":
            return self._refuse("wrong_schema", str(window)[:80])
        if window.get("asset") != self.asset:
            return self._refuse("unknown_asset", str(window.get("asset")))
        if window.get("features_sha256") != self.features_sha256:
            return self._refuse("features_changed", f"window features {str(window.get('features_sha256'))[:12]}, "
                                                    f"frozen {self.features_sha256[:12]}")
        if not isinstance(window.get("features"), dict) or "start" not in window or "end" not in window:
            return self._refuse("incomplete", "no features or bounds")
        doc = self.score(window)
        self.counts["scored"] += 1
        self.counts["alerts"] += int(doc["alert"])
        if self.store is not None:
            self.store.insert("scores", {
                "asset_id": self.asset, "window_start": doc["window_start"], "window_end": doc["window_end"],
                "score": doc["score"], "errors": doc["errors"], "threshold": doc["threshold"],
                "alert": doc["alert"], "detector_run_id": self.run_id, "profile_sha256": self.profile_sha256},
                ignore_conflict=True)
        log.info("score %s %.2f / %.2f (top %s) alert=%s", doc["window_end"], doc["score"], doc["threshold"],
                 doc["top"][0][0] if doc["top"] else "-", doc["alert"])
        return [Out(self.scores_topic, canonical_json(doc))]

    def report(self) -> dict:
        return dict(self.counts, asset=self.asset, detector_run_id=self.run_id,
                    refused_by=dict(sorted(self.refused_by.items())))


def load_for_profile(profile: dict, profile_path, config_path=None, run_id: Optional[str] = None) -> Frozen:
    """The frozen models, verified against this profile, events file and feature declarations."""
    spec_raw = read_yaml(features_file(profile, profile_path), what="features file")
    return load_frozen(config_path or ROOT / "eval" / "config.yaml", ROOT, run_id=run_id, profile=profile,
                       events_path=events_file(profile, profile_path), spec_raw=spec_raw)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile", required=True, help="asset profile YAML")
    p.add_argument("--config", default=None, help="the freeze (default eval/config.yaml)")
    p.add_argument("--run-id", default=None, help="refuse to start unless this is the frozen detector run")
    p.add_argument("--broker", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--store", default=None, help="store URL (default $PDM_STORE or sqlite:///runs/pdm.db)")
    p.add_argument("--no-store", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        profile = load_profile(args.profile)
        frozen = load_for_profile(profile, args.profile, args.config, args.run_id)
    except (ProfileError, FrozenError) as exc:
        raise SystemExit(f"detector: {exc}")
    store = None
    if not args.no_store:
        from pdm_common.store import open_store
        store = open_store(args.store)
    detector = Detector(profile, frozen, profile_hash(args.profile), store)
    log.info("asset %s: %s -> %s, %s run %s, threshold %.4f", detector.asset, profile["topics"]["windows"],
             detector.scores_topic, detector.detector, detector.run_id, detector.model.threshold)
    run_service("detector", {profile["topics"]["windows"]: detector.handle}, host=args.broker, port=args.port)
    print(json.dumps(detector.report(), sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
