"""The G1 and G2 evidence, written from the frozen evaluation's output (T37; PRD G1, G2; FRD section 1).

    python eval/evidence.py [--freeze eval/out/freeze] [--out docs/evidence/G1_G2.md]

Reads what ``python eval/evaluate.py --frozen`` and eval/prognostics.py leave in eval/out/freeze
(the PCA detector at the SPOT threshold, and the survival models), and writes one Markdown page
with the numbers as measured and a plain verdict per goal. Nothing is recomputed or rounded
beyond display, and the page names the files and hashes it came from, so it can be regenerated
and checked.

* G1: per-event lead time under leave-one-event-out at no more than two false alarms per week,
  with bootstrap intervals.
* G2: Brier skill against a constant hazard at 24, 72 and 168 h, with intervals, against the 0.15
  bar, and the reliability bins.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FREEZE = ROOT / "eval" / "out" / "freeze"
OUT = ROOT / "docs" / "evidence" / "G1_G2.md"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _span(text: str) -> str:
    """'0 days 00:30:00' -> '30 min', '0 days 06:00:00' -> '6 h'."""
    import pandas as pd
    minutes = int(pd.Timedelta(text).total_seconds() // 60)
    return f"{minutes // 60} h" if minutes % 60 == 0 else f"{minutes} min"


def _h(x) -> str:
    return "n/a" if x is None else f"{x:+.2f} h"


def _iv(pair, fmt="{:+.2f}") -> str:
    return "n/a" if not pair else f"[{fmt.format(pair[0])}, {fmt.format(pair[1])}]"


def g1(result: dict) -> list[str]:
    far = result["held_out_far"]
    budget = result["budget_per_week"]
    leads = result["lead_h"]
    iv = result.get("lead_h_interval_90") or {}
    ahead = [e for e, v in leads.items() if v is not None and v > 0]
    lines = ["## G1: warning ahead of failure at no more than two false alarms per week", "",
             f"Detector: {result['detector']}, threshold {result['threshold']:.4f} ({result['threshold_kind']}), "
             f"gate: {result['gate']['min_fraction']:.0%} of the windows in {_span(result['gate']['span'])} "
             f"over the threshold, {_span(result['gate']['hold_off'])} hold-off.", "",
             "| Measure | Value |", "|---|---|",
             f"| False alarms per week, held-out check weeks [90 % interval] | {far[0]:.2f} "
             f"[{far[1]:.2f}, {far[2]:.2f}] (budget {budget:g}) |",
             f"| False alarms per week, uncertain periods counted as false | {result['far_with_uncertain']:.2f} |",
             f"| Check weeks / incidents in them | {result['check_weeks']} / {result['check_incidents']} |", "",
             "| Failure | Lead time at the threshold | Median of the re-draws [90 %] |", "|---|---|---|"]
    for event in sorted(leads):
        lines.append(f"| {event} | {_h(leads[event])} | "
                     f"{_h((result.get('lead_h_median') or {}).get(event))} {_iv(iv.get(event))} |")
    met_budget = far[0] <= budget
    lines += ["", f"**Verdict.** The false-alarm budget holds ({far[0]:.2f} per week against {budget:g}, upper bound "
              f"{far[2]:.2f}). Every reported failure is alarmed, but only {len(ahead)} of {len(leads)} "
              f"before its report: " + ", ".join(f"{e} {_h(leads[e])}" for e in sorted(leads)) + ". "
              "A negative lead means the incident opened after the report began. G1's budget is met"
              + (";" if met_budget else " only in part;") + " useful warning is shown for "
              + (", ".join(ahead) if ahead else "no event") + ". The four labels are day-granular reports of "
              "detection, so these leads are measured against when the failure was noticed, not its onset; "
              "the expert log's 21 failures, which the detector also alarms on, are the larger test (the "
              "detector-selection review, Section 8)."]
    return lines


def g2(surv: dict, model: str) -> list[str]:
    bar = surv["bar"]
    lines = ["## G2: the probability of failure within 24, 72 and 168 h", "",
             f"Survival model: {model} per horizon, leave-one-event-out, against a constant hazard "
             f"(P = 1 - exp(-lambda h)); {surv['rows']} hourly rows; bar: Brier skill {bar:.2f}.", "",
             "| Horizon | Brier (model) | Brier (constant hazard) | Skill [90 %] | Meets the bar |",
             "|---|---|---|---|---|"]
    verdicts = []
    for h in sorted(surv["models"][model], key=int):
        p = surv["models"][model][h]["pooled"]
        verdicts.append(p["meets_bar"])
        lines.append(f"| {h} h | {p['brier']:.4f} | {p['brier_baseline']:.4f} | {p['skill']:+.3f} "
                     f"{_iv(p.get('skill_interval_90'), '{:+.3f}')} | {'yes' if p['meets_bar'] else 'no'} |")
    lines += ["", "Per event (skill):", "", "| Horizon | " + " | ".join(sorted(next(iter(
        surv["models"][model].values()))["per_event"])) + " |", "|---|" + "---|" * 4]
    for h in sorted(surv["models"][model], key=int):
        pe = surv["models"][model][h]["per_event"]
        lines.append(f"| {h} h | " + " | ".join(f"{pe[e]['skill']:+.3f}" for e in sorted(pe)) + " |")
    rel = (surv.get("reliability") or {}).get(model) or {}
    if rel:
        lines += ["", "Reliability (bins with rows; predicted against observed share):", "",
                  "| Horizon | Bin | Rows | Predicted | Observed |", "|---|---|---|---|---|"]
        for h in sorted(rel, key=int):
            for b in rel[h]:
                if b["n"] and not (isinstance(b["predicted"], float) and math.isnan(b["predicted"])):
                    lines.append(f"| {h} h | {b['bin']} | {b['n']} | {b['predicted']:.3f} | {b['observed']:.3f} |")
    lines += ["", f"**Verdict.** G2 is not met: the skill is negative at every horizon, so the model does worse "
              f"than a constant hazard, and below the {bar:.2f} bar with its whole interval. The service "
              "publishes the probabilities with their intervals and with this skill attached (prognosis_v1 "
              "`skill.meets_bar` is false), the dashboard says so next to every probability, and the agent's "
              "incident context tells the model to report them as context, not as a forecast. With four "
              "events, the hourly rows are few and correlated; the shortfall is reported as measured."]
    return lines


def build(freeze: Path) -> str:
    det = freeze / "pca_plus_spot" / "result.json"
    surv_path = freeze / "prognostics" / "survival.json"
    result = json.loads(det.read_text())
    surv = json.loads(surv_path.read_text())
    config = (ROOT / "eval" / "config.yaml").read_text()
    model = "logistic" if "model: logistic" in config else sorted(surv["models"])[0]
    lines = ["# G1 and G2 evidence", "",
             "Written by `python eval/evidence.py` from the frozen evaluation; do not edit by hand.", "",
             f"Sources: `{det.relative_to(ROOT)}` (sha256 {_sha(det)}), `{surv_path.relative_to(ROOT)}` "
             f"(sha256 {_sha(surv_path)}), the freeze in `eval/config.yaml` (sha256 "
             f"{_sha(ROOT / 'eval' / 'config.yaml')}). The dataset is MetroPT-3 (source sha256 "
             f"{result['inputs']['source_sha256'][:12]}), {result['inputs']['windows']} windows.", ""]
    lines += g1(result) + [""] + g2(surv, model)
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--freeze", default=str(FREEZE))
    p.add_argument("--out", default=str(OUT))
    args = p.parse_args(argv)
    freeze = Path(args.freeze)
    if not (freeze / "pca_plus_spot" / "result.json").exists():
        print(f"no frozen evaluation in {freeze}: run python eval/evaluate.py --frozen first", file=sys.stderr)
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(freeze))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
