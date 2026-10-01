"""z-score scorer -- the R0 baseline detector (R0.4, DET-02).

Fit: per detector input, the mean and standard deviation of the one-minute
window means over one healthy candidate from events.yaml (for MetroPT-3,
H4: Mar 21-23, clean in the EDA check). Training rows go through the same
pdm_common MinuteWindower the ingest stub uses, so the scorer is fitted on
exactly the representation it scores. Rows inside the candidate's exclude
spans, the freeze masks, the uncertain periods and the grey periods are
left out.

Score: each window_v1 gets one score_v1 -- the largest absolute z across
channels, the per-channel |z| (DET-06, DET-07), the threshold and the alert
flag -- and a retained status_v1 with its NE 107 state. The threshold is a
placeholder (|z| > 4 by default); SPOT replaces it in R1b.

    # fit once; writes models/<asset_id>/zscore.json (gitignored)
    python services/detector/zscore.py --profile P --fit --data CSV --candidate H4
    # score the stream
    python services/detector/zscore.py --profile P

Payloads are documented in services/detector/README.md.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import math
import sys
from collections import Counter
from pathlib import Path
from statistics import fmean, stdev
from typing import Optional

from pdm_common.events import event_span, events_hash, load_events
from pdm_common.hashing import canonical_json, sha256_file
from pdm_common.mqtt import Out, run_service
from pdm_common.profile import ProfileError, events_file, load_profile, profile_hash
from pdm_common.status import r0_status
from pdm_common.timeutil import iso_utc, to_utc
from pdm_common.windows import MinuteWindower

log = logging.getLogger("detector")

MODEL_SCHEMA = "zscore_model_v1"
SCORE_SCHEMA = "score_v1"
STATUS_SCHEMA = "status_v1"
DEFAULT_THRESHOLD = 4.0


class FitError(ValueError):
    pass


# --- fit -----------------------------------------------------------------

def _blocked_spans(events: dict, candidate: dict) -> list[tuple[dt.datetime, dt.datetime]]:
    spans = [event_span(x) for x in candidate.get("exclude", [])]
    for key in ("data_quality_masks", "uncertain_periods", "grey_periods"):
        spans += [event_span(x) for x in events.get(key, [])]
    return spans


def fit(profile: dict, events: dict, data_path: str | Path, candidate_id: str,
        threshold: float = DEFAULT_THRESHOLD) -> dict:
    """Fit the z-score model on one healthy candidate; returns the model dict."""
    candidates = {c["id"]: c for c in events.get("healthy_candidates", [])}
    if candidate_id not in candidates:
        raise FitError(f"no healthy candidate {candidate_id!r} in events.yaml "
                       f"(have: {', '.join(candidates) or 'none'})")
    candidate = candidates[candidate_id]
    start, end = event_span(candidate)
    blocked = _blocked_spans(events, candidate)
    inputs = list(profile["detector"]["inputs"])
    tz = profile.get("source_timezone", "UTC")

    windower = MinuteWindower(inputs)
    windows, rows, excluded = [], 0, 0
    with open(data_path, newline="") as f:
        for row in csv.DictReader(f):
            ts = to_utc(dt.datetime.fromisoformat(row["timestamp"]), tz=tz)
            if ts < start:
                continue
            if ts >= end:
                break                     # the file is in time order (the replayer relies on it too)
            if any(s <= ts < e for s, e in blocked):
                excluded += 1
                continue
            rows += 1
            windows += windower.add(ts, {name: float(row[name]) for name in inputs})
    windows += windower.flush()
    if len(windows) < 2:
        raise FitError(f"candidate {candidate_id} holds {len(windows)} windows in {data_path}")

    channels = {}
    for name in inputs:
        values = [w.means[name] for w in windows]
        sd = stdev(values)
        if not sd > 0:
            raise FitError(f"{name} is constant over candidate {candidate_id}; cannot scale it")
        channels[name] = {"mean": fmean(values), "std": sd}

    scores = sorted(max(abs(w.means[n] - c["mean"]) / c["std"] for n, c in channels.items())
                    for w in windows)

    def q(p):
        return scores[min(len(scores) - 1, int(p * len(scores)))]

    return {
        "schema": MODEL_SCHEMA,
        "detector": "zscore",
        "asset": profile["asset"]["id"],
        "channels": channels,
        "threshold": threshold,
        "threshold_rule": "placeholder: alert when any channel's |z| exceeds the threshold; "
                          "SPOT replaces it in R1b",
        "fit": {
            "candidate": candidate_id,
            "start": iso_utc(start),
            "end": iso_utc(end),
            "rows": rows,
            "rows_excluded": excluded,
            "windows": len(windows),
            "window": "one-minute mean (pdm_common.windows.MinuteWindower)",
        },
        "in_sample_score": {"p50": q(0.5), "p99": q(0.99), "p999": q(0.999), "max": scores[-1]},
    }


def model_id(model: dict) -> str:
    """Stable id for a fitted model: stands in for the MLflow run ID until R1b."""
    from hashlib import sha256

    return "zscore-" + sha256(canonical_json(model).encode()).hexdigest()[:12]


# --- score ---------------------------------------------------------------

class Scorer:
    """The service minus MQTT: window_v1 in, score_v1 and status_v1 out."""

    def __init__(self, profile: dict, model: dict, profile_sha256: str,
                 events_sha256: Optional[str] = None):
        if model.get("schema") != MODEL_SCHEMA:
            raise ValueError(f"not a {MODEL_SCHEMA} file")
        if model["asset"] != profile["asset"]["id"]:
            raise ValueError(f"model is for asset {model['asset']!r}, profile is "
                             f"{profile['asset']['id']!r}")
        self.asset = profile["asset"]["id"]
        self.scores_topic = profile["topics"]["scores"]
        self.status_topic = profile["topics"]["status"]
        self.channels = model["channels"]
        self.threshold = float(model["threshold"])
        self.run_id = model_id(model)
        self.profile_sha256 = profile_sha256
        self.events_sha256 = events_sha256
        self.counts = Counter({"windows": 0, "scored": 0, "dropped": 0, "alerts": 0})

    def handle(self, payload: bytes | str) -> list[Out]:
        self.counts["windows"] += 1
        try:
            window = json.loads(payload)
            if window.get("schema") != "window_v1" or window.get("asset") != self.asset:
                raise ValueError(f"not a window_v1 for {self.asset}")
            features = window["features"]
            errors = {}
            for name, c in self.channels.items():
                value = features[name]
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                    raise ValueError(f"{name} is not a finite number")
                errors[name] = abs(value - c["mean"]) / c["std"]
            start, end = window["start"], window["end"]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            self.counts["dropped"] += 1
            log.warning("dropped window: %s", exc)
            return []

        score = max(errors.values())
        alert = score > self.threshold
        state = r0_status(alert)
        self.counts["scored"] += 1
        self.counts["alerts"] += int(alert)
        common = {"asset": self.asset, "profile_sha256": self.profile_sha256,
                  "detector_run": self.run_id, "threshold": self.threshold,
                  "alert": alert, "score": score}
        score_msg = dict(common, schema=SCORE_SCHEMA, detector="zscore",
                         window_start=start, window_end=end, errors=errors,
                         events_sha256=self.events_sha256)
        status_msg = dict(common, schema=STATUS_SCHEMA, ts=end, ne107=state)
        top = max(errors, key=errors.get)
        log.info("score %s %.2f / %.2f (top %s) alert=%s -> %s",
                 end, score, self.threshold, top, alert, state)
        return [Out(self.scores_topic, canonical_json(score_msg)),
                Out(self.status_topic, canonical_json(status_msg), retain=True)]


# --- CLI -----------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile", required=True, help="asset profile YAML")
    p.add_argument("--model", default=None,
                   help="model file (default models/<asset_id>/zscore.json)")
    p.add_argument("--broker", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    fit_args = p.add_argument_group("fit (writes the model file, then exits)")
    fit_args.add_argument("--fit", action="store_true")
    fit_args.add_argument("--data", help="telemetry CSV holding the candidate span")
    fit_args.add_argument("--candidate", help="healthy candidate id from events.yaml")
    fit_args.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                          help=f"placeholder |z| threshold (default {DEFAULT_THRESHOLD})")
    args = p.parse_args(argv)
    if args.fit and not (args.data and args.candidate):
        p.error("--fit needs --data and --candidate")
    return args


def main(argv=None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        profile = load_profile(args.profile)
    except ProfileError as exc:
        raise SystemExit(str(exc))
    asset = profile["asset"]["id"]
    model_path = Path(args.model or f"models/{asset}/zscore.json")
    events_path = events_file(profile, args.profile)

    if args.fit:
        events = load_events(events_path)
        try:
            model = fit(profile, events, args.data, args.candidate, args.threshold)
        except FitError as exc:
            raise SystemExit(str(exc))
        model["fit"].update(data_sha256=sha256_file(args.data),
                            profile_sha256=profile_hash(args.profile),
                            events_sha256=events_hash(events_path))
        model_path.parent.mkdir(parents=True, exist_ok=True)
        model_path.write_text(json.dumps(model, indent=2, sort_keys=True) + "\n")
        f = model["fit"]
        print(f"fitted {model_id(model)} on {f['candidate']} ({f['start']} to {f['end']}): "
              f"{f['windows']} windows from {f['rows']} rows, {f['rows_excluded']} excluded; "
              f"threshold {model['threshold']}, in-sample p999 "
              f"{model['in_sample_score']['p999']:.2f} -> {model_path}")
        return

    if not model_path.exists():
        raise SystemExit(f"{model_path}: no model. Fit one first:\n  python {sys.argv[0]} "
                         f"--profile {args.profile} --fit --data <csv> --candidate <id>")
    model = json.loads(model_path.read_text())
    if model["fit"].get("profile_sha256") != profile_hash(args.profile):
        log.warning("the profile changed since this model was fitted; refit when inputs change")
    scorer = Scorer(profile, model, profile_hash(args.profile), events_hash(events_path))
    log.info("asset %s: %s -> %s + %s (retained), model %s, threshold %.2f", asset,
             profile["topics"]["windows"], scorer.scores_topic, scorer.status_topic,
             scorer.run_id, scorer.threshold)
    run_service("detector", {profile["topics"]["windows"]: scorer.handle},
                host=args.broker, port=args.port)
    print(json.dumps(dict(scorer.counts, asset=asset, detector_run=scorer.run_id), sort_keys=True))


if __name__ == "__main__":
    main()
