"""Leave-one-event-out evaluation harness: the command line (T27; EVL-01 to EVL-04).

    python eval/evaluate.py                                    # the fold plan (CI runs this, no data needed)
    python eval/evaluate.py --processed data/processed_v4 --detector pca
    python eval/evaluate.py --processed data/processed_v4 --detector all     # the full comparison + rule
    python eval/evaluate.py --processed data/processed_v4 --detector pca \\
        --golden data/processed_v4/detector_selection/selection.json         # the T27 done-check
    python eval/evaluate.py --processed DIR --scores scores_all.npz --key PCA   # saved scores, no fitting
    python eval/evaluate.py --processed data/processed_v4 --detector pca --threshold spot   # SPOT (T11)
    python eval/evaluate.py --processed data/processed_v4 --frozen      # the frozen detector, no refit (T30)

With --processed it fits each detector on the fit windows, sets its threshold
on the calibrate weeks at the budget of two gated incidents a week, measures
false alarms on the check weeks (with and without the uncertain periods) and
lead time per event with 90 % intervals from 500 re-drawn thresholds, then
writes eval/out/<detector>/: comparison.csv, lead_times.csv, auroc.csv,
result.json and scores.npz. --detector all adds the selection rule's trace in
eval/out/selection.json. Pooled figures are printed only beside n = 4.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "eval"))

from pdm_common.events import load_events  # noqa: E402

PROFILE = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"
EVENTS = ROOT / "profiles" / "metropt3_apu" / "events.yaml"
OUT = ROOT / "eval" / "out"


def fold_plan(events_path: Path = EVENTS) -> None:
    """The Version Zero printout: what a run will execute, with no data."""
    cfg = load_events(events_path)
    fails = cfg["failures"]
    uncertain = cfg.get("uncertain_periods", [])
    print(f"asset            : {cfg['asset']}")
    print(f"campaign         : {cfg['campaign'][0]} -> {cfg['campaign'][1]}")
    print(f"guard band       : {cfg['guard_band']['before']} before / {cfg['guard_band']['after']} after")
    print(f"healthy buffer   : {cfg['healthy_buffer']['before_failure']} before / "
          f"{cfg['healthy_buffer']['after_repair']} after repair")
    print(f"lead horizons    : {', '.join(str(h) for h in cfg['lead_horizons'])}")
    print(f"uncertain periods: {len(uncertain)} (excluded from healthy; FAR reported with and without)")
    print(f"expert-reported  : {len(cfg.get('expert_reported', []))} failures (scored by eval/triage_eval.py)")
    print()
    for f in fails:
        paired = f" [paired with {f['paired_with']}]" if f.get("paired_with") else ""
        print(f"fold holds out {f['id']} ({f['type']}, {f['start']} -> {f['end']}){paired}")
    print("\nrun with --processed <folder> to fit and score; pooled figures are reported only alongside n=4.")


def run(args) -> int:
    import pandas as pd

    import detectors as dets
    import harness as h
    from pdm_common.features import features_file, load_feature_spec
    from pdm_common.gate import gate_params
    from pdm_common.profile import load_profile

    t0 = time.time()
    profile = load_profile(args.profile)
    data = h.load(args.processed)
    roles = h.assign_roles(data.windows)
    features = h.detector_features(data.feature_dict)
    scaled = h.scale(data.windows, features, roles)
    ctx = h.context(data, roles, gate_params(profile), budget=args.budget)
    print(f"{len(data.windows):,} windows (key {data.windows_key}); {len(features)} features; fitting on "
          f"{scaled.fit_rows.sum():,} of {scaled.is_fit.sum():,} fit windows; read in {time.time() - t0:.0f} s")

    scores, info, models = {}, {}, {}
    if args.scores:
        saved = np.load(args.scores)
        keys = [args.key] if args.key else list(saved.files)
        for k in keys:
            scores[k.replace("_", " ")] = saved[k]
            info[k.replace("_", " ")] = {"family": "saved scores", "attribution": False,
                                         "multivariate": True, "complexity": 9}
    else:
        table = dets.tabular_detectors()
        wanted = list(table) + ["dense_ae_seq"] if args.detector == "all" else args.detector.split(",")
        for key in wanted:
            t1 = time.time()
            if key == "dense_ae_seq":
                spec = load_feature_spec(features_file(profile, args.profile))
                sequences, _ = h.sequence_inputs(data, profile, spec, scaled.is_fit)
                det = dets.sequence_detector()
                rows = np.random.default_rng(dets.SEED).choice(np.flatnonzero(scaled.is_fit), 30_000, replace=False)
                det.fit(sequences(rows).reshape(len(rows), -1))
                flat = sequences(np.arange(1)).reshape(1, -1).shape[1]
                s = np.concatenate([det.score(sequences(np.arange(i, min(i + 20_000, len(data.windows))))
                                              .reshape(-1, flat)) for i in range(0, len(data.windows), 20_000)])
            else:
                det = table[key]
                det.fit(scaled.X[scaled.fit_rows])
                s = det.score(scaled.X)
            scores[det.name], info[det.name], models[det.name] = s, dets.info(det), det
            print(f"  {det.name}: fit and scored in {time.time() - t1:.1f} s")

    results = {}
    for name, s in scores.items():
        results[name] = h.evaluate(s, ctx)
        if args.threshold == "spot":
            spot = spot_threshold(s, ctx, args)
            results[f"{name} + SPOT"] = h.evaluate(s, ctx, threshold=spot["threshold"])
            results[f"{name} + SPOT"]["spot"] = spot
            info[f"{name} + SPOT"] = info[name]
    print(f"evaluated in {time.time() - t0:.0f} s (threshold grid, check weeks, uncertain periods, "
          f"{len(data.failures)} test blocks, 500 threshold re-draws each)")

    failures = list(data.failures)
    comparison = h.comparison_table(results, info, failures)
    leads = h.lead_table(results, failures)
    aurocs = h.auroc_table(results, failures)
    with pd.option_context("display.width", 160, "display.max_columns", 20):
        print("\n" + comparison.to_string())
        print("\nlead in hours: at the chosen threshold, and the median [90 % interval] over 500 re-drawn thresholds")
        print(leads.to_string())
    print(f"\nn = {len(failures)} events; F2 and F3 are a paired case (asymmetric blocks). "
          "No pooled lead is reported.")

    out_root = Path(args.out)
    for name, r in results.items():
        folder = out_root / name.lower().replace(" ", "_").replace("(", "").replace(")", "").replace("+", "plus")
        folder.mkdir(parents=True, exist_ok=True)
        comparison.loc[[name]].to_csv(folder / "comparison.csv")
        leads.loc[[name]].to_csv(folder / "lead_times.csv")
        aurocs.loc[[name]].to_csv(folder / "auroc.csv")
        record = {"detector": name, **h.summary(r, failures), **({"spot": r["spot"]} if "spot" in r else {}),
                  "inputs": {"windows_key": data.windows_key, "fit_key": h.fit_key(scaled.is_fit),
                             "source_sha256": data.manifest["source"]["sha256"], "windows": len(data.windows),
                             "fit_windows": int(scaled.fit_rows.sum())},
                  "gate": ctx.gate.as_dict(), "budget_per_week": args.budget}
        (folder / "result.json").write_text(json.dumps(record, indent=2))
        np.savez_compressed(folder / "scores.npz", scores=np.asarray(scores[name.replace(" + SPOT", "")]))
    spot_rows = [n for n in results if n.endswith("+ SPOT")]
    for name in spot_rows:
        base, r = results[name.replace(" + SPOT", "")], results[name]
        caught = sum(not np.isnan(r[f"{ev} lead"]) for ev in failures)
        verdict = r["far hi"] <= args.budget and caught == len(failures)
        r["spot"]["freeze_rule"] = {"far_interval_within_budget": bool(r["far hi"] <= args.budget),
                                    "caught": caught, "use_spot": bool(verdict)}
        print(f"\n{name}: held-out FAR {r['far']:.2f} [{r['far lo']:.2f}, {r['far hi']:.2f}] against "
              f"{base['far']:.2f} [{base['far lo']:.2f}, {base['far hi']:.2f}] empirical; leads "
              + ", ".join(f"{ev} {h._fmt_lead(r[f'{ev} lead'])}" for ev in failures)
              + f". Freeze rule (interval at or under {args.budget:g} a week and {len(failures)} of "
              f"{len(failures)} caught): {'SPOT' if verdict else 'empirical, SPOT reported as the comparison'}")
        folder = out_root / name.lower().replace(" ", "_").replace("(", "").replace(")", "").replace("+", "plus")
        rec = json.loads((folder / "result.json").read_text())
        rec["spot"] = r["spot"]
        (folder / "result.json").write_text(json.dumps(rec, indent=2))
    if len([n for n in results if not n.endswith("+ SPOT")]) > 1 and not args.scores:
        rule = h.select({n: r for n, r in results.items() if not n.endswith("+ SPOT")}, info, failures,
                        budget=args.budget)
        print("\n1. eligible (held-out interval reaches the budget): " + ", ".join(rule["eligible"]))
        print("2. deep models dropped by EVL-05: " + (", ".join(rule["deep_dropped_evl05"]) or "none"))
        print("3. best median lead per event among the rest, h: "
              + ", ".join(f"{ev} {v:+.1f}" for ev, v in rule["best_median_lead_h"].items() if v is not None))
        print("4. within 1 h of the best on every event: " + ", ".join(rule["frontier"]))
        print("5. tie-break order: " + " > ".join(rule["ranked"]))
        print(f"\nselected: {rule['selected']}")
        (out_root / "selection.json").write_text(json.dumps(rule, indent=2))

    if args.save_bundle:
        name = next(n for n in results if not n.endswith("+ SPOT"))
        spot_name = f"{name} + SPOT"
        use_spot = spot_name in results and results[spot_name]["spot"]["freeze_rule"]["use_spot"]
        bundle = detector_bundle(models[name], name, results, use_spot, scaled, data, ctx, args, failures,
                                 profile["asset"]["id"])
        folder = Path(args.save_bundle)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "bundle.json").write_text(json.dumps(bundle, sort_keys=True, indent=1) + "\n")
        print(f"\nsaved the {name} bundle ({bundle['threshold']['kind']} threshold "
              f"{bundle['threshold']['value']:.4f}) to {folder / 'bundle.json'}")
    if args.golden:
        return golden_check(results, Path(args.golden), failures)
    return 0


def detector_bundle(det, name, results, use_spot, scaled, data, ctx, args, failures, asset) -> dict:
    """detector_bundle_v1: everything the services need to score as the harness did."""
    import platform

    import pandas as pd
    import sklearn

    import harness as h
    if not hasattr(det, "pca"):
        raise SystemExit(f"--save-bundle writes PCA bundles; {name} is not one")
    chosen = results[f"{name} + SPOT"] if use_spot else results[name]
    threshold = {"kind": "spot" if use_spot else "empirical", "value": chosen["threshold"],
                 "percentile_of_calibrate_scores": round(chosen["threshold percentile"], 2),
                 "empirical_value": results[name]["threshold"],
                 "rule": "SPOT if its held-out interval stays at or under the budget and it catches every event; "
                         "otherwise the empirical threshold, with SPOT as the comparison"}
    if f"{name} + SPOT" in results:
        spot = results[f"{name} + SPOT"]["spot"]
        threshold["spot"] = {k: v for k, v in spot.items() if k != "scan"}
    pca = det.pca
    return {
        "schema": "detector_bundle_v1", "asset": asset,
        "detector": name, "features": scaled.features,
        "scaler": {"healthy_median": scaled.healthy_median.tolist(), "mean": scaled.mean.tolist(),
                   "std": scaled.std.tolist()},
        "model": {"kind": "pca", "n_components": int(pca.n_components_), "components": pca.components_.tolist(),
                  "mean": pca.mean_.tolist(), "explained_variance_ratio": pca.explained_variance_ratio_.tolist()},
        "threshold": threshold, "gate": ctx.gate.as_dict(), "budget_per_week": args.budget,
        "metrics": h.summary(chosen, failures),
        "comparison": {k: h.summary(r, failures) for k, r in results.items()},
        "inputs": {"windows_key": data.windows_key, "fit_key": h.fit_key(scaled.is_fit),
                   "source_sha256": data.manifest["source"]["sha256"], "windows": len(data.windows),
                   "fit_windows": int(scaled.fit_rows.sum())},
        "versions": {"python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__,
                     "scikit_learn": sklearn.__version__},
    }


def run_frozen(args) -> int:
    """T30: score with the frozen bundle -- no refit -- and reproduce its logged metrics."""
    import harness as h
    from pdm_common.events import load_events  # noqa: F401
    from pdm_common.features import features_file, load_feature_spec
    from pdm_common.frozen import FrozenError, load_frozen
    from pdm_common.gate import gate_params
    from pdm_common.profile import events_file, load_profile

    profile = load_profile(args.profile)
    spec = load_feature_spec(features_file(profile, args.profile))
    try:
        frozen = load_frozen(args.config, ROOT, run_id=args.run_id, profile=profile,
                             events_path=events_file(profile, args.profile), spec_raw=spec.raw)
    except FrozenError as exc:
        print(f"refused: {exc}")
        return 2
    data = h.load(args.processed)
    want_key = frozen.config["windows_key"]
    if data.windows_key != want_key:
        print(f"refused: {args.processed} has windows_key {data.windows_key}, the freeze used {want_key}")
        return 2
    model = frozen.detector
    scores = model.score(data.windows[model.features].to_numpy(float))
    roles = h.assign_roles(data.windows)
    ctx = h.context(data, roles, gate_params(profile), budget=frozen.detector_bundle["budget_per_week"])
    r = h.evaluate(scores, ctx, threshold=model.threshold)
    failures = list(data.failures)
    got = h.summary(r, failures)
    logged = frozen.detector_bundle["metrics"]
    print(f"frozen detector {frozen.detector_run_id}: {frozen.detector_bundle['detector']} at the "
          f"{frozen.detector_bundle['threshold']['kind']} threshold {model.threshold:.4f}")
    print(f"held-out FAR {r['far']:.2f} [{r['far lo']:.2f}, {r['far hi']:.2f}] a week "
          f"({r['far with uncertain']:.2f} with the uncertain periods); leads "
          + ", ".join(f"{ev} {h._fmt_lead(r[f'{ev} lead'])}" for ev in failures) + " h")
    same = (got["held_out_far"] == logged["held_out_far"] and got["lead_h"] == logged["lead_h"]
            and got["lead_h_median"] == logged["lead_h_median"])
    if not same:
        print("MISMATCH with the metrics logged at the freeze:\n  logged " + json.dumps(
            {k: logged[k] for k in ("held_out_far", "lead_h")}) + "\n  now    " + json.dumps(
            {k: got[k] for k in ("held_out_far", "lead_h")}))
        return 1
    print("matches the metrics logged at the freeze")
    return 0


def spot_threshold(scores, ctx, args) -> dict:
    """T11: fit SPOT on the calibrate scores and choose q at the budget. If the
    fitted shape falls outside [0, 1], refit at the 0.99 quantile and keep both
    fits in the record (the plan's if-then); never adjust it against the events."""
    import harness as h
    from pdm_common.spot import calibrate_q, fit_spot

    end_c, s_c = h.series(ctx, ctx.is_cal, scores)
    fits = [fit_spot(s_c, init_quantile=args.init_quantile)]
    if not 0.0 <= fits[0].gamma <= 1.0 and args.init_quantile < 0.99:
        fits.append(fit_spot(s_c, init_quantile=0.99))
    fit = fits[-1]
    cal = calibrate_q(fit, end_c, s_c, ctx.gate, budget=ctx.budget)
    print(f"\nSPOT on {len(s_c):,} calibrate scores: t = {fit.t:.4f} (the {fit.init_quantile} quantile), "
          f"gamma = {fit.gamma:.3f}, sigma = {fit.sigma:.4f}, n = {fit.n}, n_t = {fit.n_t}; "
          f"q = {cal['q']:.2e} -> threshold {cal['threshold']:.4f} "
          f"({cal['calibrate_rate']:.2f} gated incidents a week on the calibrate weeks)")
    if len(fits) > 1:
        print(f"  (gamma {fits[0].gamma:.3f} at the {fits[0].init_quantile} quantile was outside [0, 1]; refitted)")
    return {"fit": fit.as_dict(), "fits_tried": [f.as_dict() for f in fits], "q": cal["q"],
            "threshold": cal["threshold"], "calibrate_rate": cal["calibrate_rate"], "scan": cal["scan"]}


def golden_check(results: dict, golden_path: Path, failures) -> int:
    """T27 done-check: the harness reproduces the notebook's selection.json."""
    golden = json.loads(golden_path.read_text())
    name = golden["selected"]
    if name not in results:
        print(f"golden check: {name} was not evaluated")
        return 1
    r = results[name]
    problems = []
    if abs(r["threshold"] - golden["threshold"]) > 1e-6 * max(1.0, abs(golden["threshold"])):
        problems.append(f"threshold {r['threshold']:.6f} vs {golden['threshold']:.6f}")
    mine_far = [round(r[k], 3) for k in ("far", "far lo", "far hi")]
    if mine_far != golden["held_out_far"]:
        problems.append(f"held-out FAR {mine_far} vs {golden['held_out_far']}")
    for ev in failures:
        for key, mine in (("lead_h", r[f"{ev} lead"]), ("lead_h_median", r[f"{ev} lead median"])):
            want = golden[key][ev]
            got = None if np.isnan(mine) else round(float(mine), 2)
            if got != want:
                problems.append(f"{ev} {key} {got} vs {want}")
        lo, hi = r[f"{ev} lead lo"], r[f"{ev} lead hi"]
        got = [None if np.isnan(v) else round(float(v), 2) for v in (lo, hi)]
        if got != golden["lead_h_interval_90"][ev]:
            problems.append(f"{ev} interval {got} vs {golden['lead_h_interval_90'][ev]}")
    if problems:
        print("\nGOLDEN CHECK FAILED against " + str(golden_path) + ":\n  " + "\n  ".join(problems))
        return 1
    print(f"\ngolden check passed: {name} matches {golden_path.name} -- threshold {r['threshold']:.4f} "
          f"({r['threshold percentile']:.1f}th percentile of calibrate scores), held-out FAR "
          f"{r['far']:.2f} [{r['far lo']:.2f}, {r['far hi']:.2f}] a week, leads "
          + ", ".join(f"{ev} {r[f'{ev} lead']:+.1f}" for ev in failures) + " h")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--processed", help="processed folder (windows_10min.csv.gz, manifest.json, ...)")
    p.add_argument("--profile", default=str(PROFILE))
    p.add_argument("--detector", default="pca",
                   help="pca, zscore, iforest, robust_mahalanobis, lof, dense_ae, dense_ae_seq, "
                        "a comma-separated list, or all")
    p.add_argument("--scores", help="evaluate saved window scores (.npz) instead of fitting")
    p.add_argument("--key", help="with --scores: the array to evaluate (default: every array)")
    p.add_argument("--budget", type=float, default=2.0, help="false alarms per week (PRD G1)")
    p.add_argument("--threshold", choices=["empirical", "spot"], default="empirical",
                   help="spot: also evaluate each detector at its SPOT threshold (T11)")
    p.add_argument("--init-quantile", type=float, default=0.98, help="SPOT's starting quantile")
    p.add_argument("--golden", help="selection.json to reproduce (the T27 done-check)")
    p.add_argument("--out", default=str(OUT))
    p.add_argument("--save-bundle", help="write the detector bundle (detector_bundle_v1) to this folder; "
                                         "the threshold follows the freeze rule when --threshold spot is given")
    p.add_argument("--frozen", action="store_true", help="score with the frozen bundle in --config, no refit")
    p.add_argument("--config", default=str(ROOT / "eval" / "config.yaml"))
    p.add_argument("--run-id", help="with --frozen: refuse unless this is the pinned detector run (DET-08)")
    args = p.parse_args(argv)
    if not args.processed:
        fold_plan()
        return 0
    if args.frozen:
        return run_frozen(args)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
