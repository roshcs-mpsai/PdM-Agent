"""The Week-7 freeze (T30; PR-09, DET-08, NFR-05).

    python tools/freeze.py --processed data/processed_v4
    git tag -a r1-freeze -m "Week-7 freeze: PCA with the SPOT threshold; survival model"

1. Fits the selected detector (PCA) on the fit windows, evaluates it at the
   empirical and the SPOT threshold, and writes the detector bundle with the
   threshold the pre-registered rule picks (SPOT if its held-out interval stays
   at or under two a week and it catches 4 of 4; otherwise the empirical one).
2. Builds the survival model's hourly table with that threshold and writes the
   frozen survival bundle (logistic, 200 bootstrap refits over whole days).
3. Writes eval/config.yaml: both bundles by path and SHA-256, the threshold,
   and the hashes of the events file, the profile's detector sections, the
   feature declarations, the window table and the raw file. Run IDs stay null
   until tools/register_bundle.py registers the bundles in MLflow.

Afterwards ``python eval/evaluate.py --processed data/processed_v4 --frozen``
must reproduce the logged metrics, and any other bundle or run ID is refused.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from pdm_common.features import features_file, load_feature_spec  # noqa: E402
from pdm_common.frozen import features_sha256, profile_detector_sha256  # noqa: E402
from pdm_common.hashing import sha256_file  # noqa: E402
from pdm_common.profile import events_file, load_profile  # noqa: E402

CONFIG_HEADER = """# eval/config.yaml -- the Week-7 freeze (T30; PR-09, DET-08)
# Written by tools/freeze.py; edit only through it. eval/evaluate.py --frozen, the detector
# service and the prognostics service load exactly these bundles and refuse anything else.
# run_id fields are filled when tools/register_bundle.py registers each bundle in MLflow:
#   python tools/register_bundle.py eval/frozen/<asset>/detector --name pdm-detector-<asset> --alias production
#   python tools/register_bundle.py eval/frozen/<asset>/survival --name pdm-survival-<asset> --experiment prognostics
# evaluation_lock: eval/promote.py refuses to move a production alias while it is on (OPS-07).
"""


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--processed", required=True)
    p.add_argument("--profile", default=str(ROOT / "profiles/metropt3_apu/asset_profile.yaml"))
    p.add_argument("--config", default=str(ROOT / "eval" / "config.yaml"))
    p.add_argument("--lock-until", default="2026-11-22")
    args = p.parse_args(argv)
    profile = load_profile(args.profile)
    asset = profile["asset"]["id"]
    det_dir = ROOT / "eval" / "frozen" / asset / "detector"
    surv_dir = ROOT / "eval" / "frozen" / asset / "survival"
    out = ROOT / "eval" / "out" / "freeze"
    py = sys.executable

    subprocess.run([py, str(ROOT / "eval/evaluate.py"), "--processed", args.processed, "--profile", args.profile,
                    "--detector", "pca", "--threshold", "spot", "--save-bundle", str(det_dir), "--out", str(out)],
                   check=True)
    det = json.loads((det_dir / "bundle.json").read_text())
    scores = out / "pca" / "scores.npz"
    subprocess.run([py, str(ROOT / "eval/prognostics.py"), "--processed", args.processed, "--profile", args.profile,
                    "--threshold", repr(det["threshold"]["value"]), "--scores", str(scores),
                    "--out", str(out / "prognostics"), "--save-bundle", str(surv_dir)], check=True)

    spec = load_feature_spec(features_file(profile, args.profile))
    manifest = json.loads((Path(args.processed) / "manifest.json").read_text())
    rel = lambda path: str(path.relative_to(ROOT))  # noqa: E731
    frozen = {
        "frozen_on": dt.date.today().isoformat(),
        "asset": asset,
        "detector": {"bundle": rel(det_dir / "bundle.json"), "bundle_sha256": sha256_file(det_dir / "bundle.json"),
                     "run_id": None, "detector": det["detector"],
                     "threshold": {"kind": det["threshold"]["kind"], "value": det["threshold"]["value"]}},
        "survival": {"bundle": rel(surv_dir / "bundle.json"), "bundle_sha256": sha256_file(surv_dir / "bundle.json"),
                     "run_id": None, "model": "logistic"},
        "events_sha256": sha256_file(events_file(profile, args.profile)),
        "profile_detector_sha256": profile_detector_sha256(profile),
        "features_sha256": features_sha256(spec.raw),
        "windows_key": det["inputs"]["windows_key"],
        "source_sha256": manifest["source"]["sha256"],
    }
    import yaml
    body = yaml.safe_dump({"frozen": frozen, "evaluation_lock": {"locked": True, "until": args.lock_until}},
                          sort_keys=False)
    Path(args.config).write_text(CONFIG_HEADER + body)
    print(f"\nwrote {args.config}: detector {frozen['detector']['bundle_sha256'][:12]}, "
          f"survival {frozen['survival']['bundle_sha256'][:12]}, threshold {det['threshold']['kind']} "
          f"{det['threshold']['value']:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
