"""Prognostics -- P(failure within 24, 72 and 168 h), every stream hour (T13, T14; PRG-01 to PRG-06).

Subscribes to the profile's windows and scores topics. On every hour of
stream time whose window (ending on the hour) is complete and scored, it builds
the hourly row the frozen survival model was trained on -- exactly the row of
eval/prognostics.py's ``hourly_table``:

* from the detector over the last ``prognosis.history`` (24 h): the maximum
  and mean score and the share of windows over the detector's threshold;
* from the window ending on the hour: the 6-h loaded and stopped shares and
  the hour's oil-temperature rise;

and publishes prognosis_v1 (retained) with each horizon's probability and its
``prognosis.interval`` (90 %) bootstrap interval, the inputs and their
contributions to P(72 h) (PRG-06), the constant-hazard baseline and the
model's measured skill against it. The skill travels with every prognosis:
the frozen model does not meet the PRD's bar of 0.15 (its leave-one-event-out
Brier skill is negative at every horizon), and the dashboard says so.

The service loads only the bundle eval/config.yaml pins and refuses to start
if it, the profile's detector sections, events.yaml or features.yaml differ
from the freeze (DET-08); the alert share uses the threshold the model was
trained with.

    python services/prognostics/prognosis.py --profile profiles/metropt3_apu/asset_profile.yaml
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from pdm_common.features import features_file  # noqa: E402
from pdm_common.frozen import Frozen, FrozenError, load_frozen  # noqa: E402
from pdm_common.hashing import canonical_json  # noqa: E402
from pdm_common.mqtt import Out, run_service  # noqa: E402
from pdm_common.profile import ProfileError, events_file, load_profile, profile_hash, read_yaml  # noqa: E402
from pdm_common.timeutil import iso_utc, parse_duration, to_utc  # noqa: E402

log = logging.getLogger("prognostics")

PROGNOSIS_SCHEMA = "prognosis_v1"
SKILL_BAR = 0.15                     # PRD G2: Brier skill against the constant hazard


class Prognostics:
    """The service minus MQTT: windows and scores in, one prognosis_v1 per stream hour out."""

    def __init__(self, profile: dict, frozen: Frozen, profile_sha256: str, store=None):
        self.asset = profile["asset"]["id"]
        self.topic = profile["topics"]["prognosis"]
        self.model = frozen.survival
        self.run_id = frozen.survival_run_id
        bundle = frozen.survival_bundle
        self.threshold = float(bundle["threshold_used"])
        meta = bundle.get("inputs_meta") or {}
        settings = profile.get("prognosis") or {}
        self.history = dt.timedelta(seconds=parse_duration(settings.get("history", meta.get("history", "24h")))
                                    .total_seconds())
        if meta.get("history") and parse_duration(meta["history"]) != self.history:
            raise FrozenError(f"the profile's prognosis history {settings.get('history')} is not the model's "
                              f"{meta['history']}")
        wanted = [int(parse_duration(h).total_seconds() // 3600) for h in settings.get("horizons", [])]
        if wanted and wanted != self.model.horizons:
            raise FrozenError(f"the profile asks for horizons {wanted} h, the frozen model has {self.model.horizons}")
        self.interval = float(settings.get("interval", 0.9))
        self.window_inputs = list(meta.get("window_inputs") or [])
        self.skill = {k: float(v) for k, v in (bundle.get("metrics") or {}).items() if k.startswith("skill_")}
        self.profile_sha256 = profile_sha256
        self.store = store
        self.scores: deque = deque()                 # (window_end, score) within the history
        self.windows: dict[str, dict] = {}           # window_end -> features, for windows ending on the hour
        self.counts = {"windows": 0, "scores": 0, "prognoses": 0}

    # -- inputs -------------------------------------------------------------------------------
    def handle_window(self, payload) -> list[Out]:
        try:
            w = json.loads(payload)
        except ValueError:
            return []
        if w.get("schema") != "window_v1" or w.get("asset") != self.asset:
            return []
        self.counts["windows"] += 1
        end = to_utc(w["end"])
        if end.minute == 0 and end.second == 0:
            self.windows[iso_utc(end)] = w.get("features") or {}
            while len(self.windows) > 48:
                self.windows.pop(next(iter(self.windows)))
        return self._ready(iso_utc(end))

    def handle_score(self, payload) -> list[Out]:
        try:
            s = json.loads(payload)
        except ValueError:
            return []
        if s.get("schema") != "score_v1" or s.get("asset") != self.asset:
            return []
        self.counts["scores"] += 1
        end = to_utc(s["window_end"])
        self.scores.append((end, float(s["score"])))
        while self.scores and self.scores[0][0] <= end - self.history:
            self.scores.popleft()
        return self._ready(iso_utc(end))

    def _ready(self, end_iso: str) -> list[Out]:
        """Predict once both the hour's window and its score have arrived (either order)."""
        if end_iso not in self.windows or not self.scores or iso_utc(self.scores[-1][0]) != end_iso:
            return []
        features = self.windows.pop(end_iso)
        return [self._predict(to_utc(end_iso), features)]

    # -- the hourly row --------------------------------------------------------------------------
    def row(self, hour: dt.datetime, features: dict) -> tuple[dict, int]:
        """The model's inputs at ``hour``: the scores of windows ending in (hour - history, hour]."""
        values = [sc for t, sc in self.scores if hour - self.history < t <= hour]
        n = len(values)
        row = {"score_max_24h": max(values), "score_mean_24h": sum(values) / n,
               "alert_share_24h": sum(v > self.threshold for v in values) / n}
        for name in self.window_inputs:
            v = features.get(name)
            row[name] = None if v is None else float(v)
        return row, n

    def _predict(self, hour: dt.datetime, features: dict) -> Out:
        row, n = self.row(hour, features)
        horizons = self.model.predict(row, self.interval)
        attribution = self.model.attribution(row, horizon=72 if 72 in self.model.horizons else self.model.horizons[0])
        mean_skill = sum(self.skill.values()) / len(self.skill) if self.skill else None
        doc = {
            "schema": PROGNOSIS_SCHEMA, "asset": self.asset, "ts": iso_utc(hour),
            "horizons": horizons, "interval": self.interval,
            "inputs": row, "windows_in_history": n,
            "input_from": iso_utc(hour - self.history), "input_to": iso_utc(hour),
            "attribution": attribution,
            "constant_hazard": self.model.constant_hazard(),
            "skill": {"brier_skill_by_horizon": self.skill, "mean": mean_skill, "bar": SKILL_BAR,
                      "meets_bar": bool(mean_skill is not None and mean_skill >= SKILL_BAR)},
            "model": self.model.bundle.get("kind", "logistic"), "model_run_id": self.run_id,
            "profile_sha256": self.profile_sha256,
        }
        self.counts["prognoses"] += 1
        if self.store is not None:
            self.store.insert("prognoses", {
                "asset_id": self.asset, "ts": doc["ts"], "probabilities": horizons, "attribution": attribution,
                "inputs": row, "model_run_id": self.run_id, "input_from": doc["input_from"],
                "input_to": doc["input_to"]}, ignore_conflict=True)
        p = {h: v["p"] for h, v in horizons.items()}
        log.info("prognosis %s P24 %.3f P72 %.3f P168 %.3f (%d windows)", doc["ts"], p.get("24", float("nan")),
                 p.get("72", float("nan")), p.get("168", float("nan")), n)
        return Out(self.topic, canonical_json(doc), retain=True)


def load_for_profile(profile: dict, profile_path, config_path=None) -> Frozen:
    spec_raw = read_yaml(features_file(profile, profile_path), what="features file")
    return load_frozen(config_path or ROOT / "eval" / "config.yaml", ROOT, profile=profile,
                       events_path=events_file(profile, profile_path), spec_raw=spec_raw)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile", required=True, help="asset profile YAML")
    p.add_argument("--config", default=None, help="the freeze (default eval/config.yaml)")
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
        frozen = load_for_profile(profile, args.profile, args.config)
    except (ProfileError, FrozenError) as exc:
        raise SystemExit(f"prognostics: {exc}")
    store = None
    if not args.no_store:
        from pdm_common.store import open_store
        store = open_store(args.store)
    service = Prognostics(profile, frozen, profile_hash(args.profile), store)
    topics = profile["topics"]
    log.info("asset %s: %s + %s -> %s (retained), %s run %s", service.asset, topics["windows"], topics["scores"],
             service.topic, service.model.bundle.get("kind"), service.run_id)
    run_service("prognostics", {topics["windows"]: service.handle_window, topics["scores"]: service.handle_score},
                host=args.broker, port=args.port)
    print(json.dumps(dict(service.counts, asset=service.asset, model_run_id=service.run_id), sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
