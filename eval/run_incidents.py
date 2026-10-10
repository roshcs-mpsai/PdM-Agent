"""Run the agent on the scripted-incident library (T28; EVL-08, PR-12, PR-13).

    python eval/run_incidents.py --store sqlite:///runs/campaign.db \\
        [--model rules|anthropic] [--arm full] [--only S04 S13] [--out eval/out/incidents/<name>]
    python eval/audit.py eval/out/incidents/<name>

The base store holds the replayed campaign (``python eval/pipeline.py --data ... --store
sqlite:///runs/campaign.db``: telemetry, windows, scores, prognoses and the 34 incidents the gate
opens). Each run works on a copy (``<out>/store.db``), so every incident starts open and the base
stays untouched. Incidents with injected faults replay their own span with the faults file into
``<out>/faults.db``.

For each entry of eval/incidents.yaml the runner finds the incident (the gate's opening at
``as_of``, the first opening after ``fault_start``, or one opened at ``as_of`` the way the gate
opens one), runs the agent on it with the real runner, tool server and store, and records the
outcome. ``--model rules`` is eval/policy.py (no key needed); ``--model anthropic`` is the pinned
model of services/agent/config.yaml ($ANTHROPIC_API_KEY). The runaway entry always uses the
runaway script, as PR-12 asks. The runs' traces and briefs stay in the copies; eval/audit.py
scores them without calling the model again (AGT-11).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

import yaml

ROOT = Path(__file__).resolve().parents[1]
for sub in ("eval", "services/agent", "services/mcp_server"):
    sys.path.insert(0, str(ROOT / sub))

from pdm_common import incidents as lifecycle  # noqa: E402
from pdm_common.asof import evidence_asof  # noqa: E402
from pdm_common.corpus import Corpus  # noqa: E402
from pdm_common.gate import gate_params  # noqa: E402
from pdm_common.profile import analog_names, profile_hash  # noqa: E402
from pdm_common.skills import load_skills  # noqa: E402
from pdm_common.store import Store  # noqa: E402
from pdm_common.timeutil import iso_utc, to_utc  # noqa: E402

LIBRARY = ROOT / "eval" / "incidents.yaml"
PROFILE = ROOT / "profiles" / "metropt3_apu" / "asset_profile.yaml"


def load_library(path=LIBRARY) -> dict:
    doc = yaml.safe_load(Path(path).read_text())
    if doc.get("schema") != "incidents_v1":
        raise ValueError(f"{path}: expected schema incidents_v1")
    ids = [i["id"] for i in doc["incidents"]]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path}: incident ids repeat")
    return doc


def copy_store(src_url: str, dst: Path) -> Store:
    """A copy of a SQLite store (the base stays untouched)."""
    if not src_url.startswith("sqlite:///"):
        raise ValueError("the base store must be a SQLite file (sqlite:///path); copy a PostgreSQL store with pg_dump")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    src = sqlite3.connect(f"file:{src_url[len('sqlite:///'):]}?mode=ro", uri=True)
    out = sqlite3.connect(str(dst))
    with out:
        src.backup(out)
    src.close()
    out.close()
    store = Store(f"sqlite:///{dst}")
    store.init_schema()
    return store


def open_as_gate(store, profile: dict, events: dict, registry, at, cause: Optional[str] = None) -> str:
    """An incident at ``at`` as the gate opens one: its parameters and the checks as of the opening."""
    asset = profile["asset"]["id"]
    at = to_utc(at)
    iid = f"INC-{asset}-{at:%Y%m%dT%H%MZ}"
    if store.select("incidents", {"incident_id": iid}, columns="incident_id"):
        return iid
    results = registry.run(evidence_asof(store, profile, events, asset, iso_utc(at)))
    score = store.latest("scores", "window_end", asset, before=iso_utc(at))
    params = gate_params(profile).as_dict()
    lifecycle.open_incident(store, {
        "incident_id": iid, "asset_id": asset, "opened_at": at, "as_of": at,
        "gate": dict(params, alerting=None, present=None),
        "triage": {"checks": [r.as_dict() for r in results], "triage": registry.triage(results)},
        "detector_run_id": score["detector_run_id"] if score else None,
        "profile_sha256": profile_hash(PROFILE)},
        cause=cause or "opened for evaluation at a time the gate did not alarm", at=at)
    return iid


def find_incident(store, entry: dict, profile: dict, events: dict, registry) -> tuple[Optional[str], str]:
    """(incident id, how it was found)."""
    asset = profile["asset"]["id"]
    if entry.get("fault_start"):
        start = to_utc(entry["fault_start"])
        rows = store.query("SELECT incident_id, opened_at FROM incidents WHERE asset_id = ? ORDER BY opened_at",
                           [asset], table="incidents")
        after = [r for r in rows if start <= to_utc(r["opened_at"]) <= start + dt.timedelta(hours=8)]
        if after:
            return after[0]["incident_id"], "the gate's opening after the fault started"
        return open_as_gate(store, profile, events, registry, start + dt.timedelta(hours=1),
                            "opened for evaluation: the gate did not alarm on the injected fault"), \
            "opened one hour into the fault (the gate did not alarm)"
    at = to_utc(entry["as_of"])
    if entry.get("open") == "as_gate":
        return open_as_gate(store, profile, events, registry, at), "opened at as_of as the gate would"
    rows = store.query("SELECT incident_id, opened_at FROM incidents WHERE asset_id = ?", [asset], table="incidents")
    near = sorted(rows, key=lambda r: abs((to_utc(r["opened_at"]) - at).total_seconds()))
    if near and abs((to_utc(near[0]["opened_at"]) - at).total_seconds()) <= 120:
        return near[0]["incident_id"], "the gate's opening"
    return None, f"the replay opened no incident within 2 min of {iso_utc(at)}"


def make_model(kind: str, entry: dict, profile: dict, cfg: dict):
    from policy import RulesModel
    if entry.get("model") == "runaway":
        return RulesModel(signals=analog_names(profile), runaway=True, name="runaway")
    if kind == "rules":
        return RulesModel(signals=analog_names(profile), query=entry.get("query"))
    if kind == "anthropic":
        import llm
        m = cfg["model"]
        return llm.AnthropicModel(m["name"], max_tokens=m.get("max_tokens", 8000), effort=m.get("effort", "medium"),
                                  timeout_s=m.get("timeout_s", 60), retries=m.get("retries", 2))
    raise ValueError(f"unknown model {kind!r}")


def run(args) -> dict:
    import runner as runner_mod
    import sinks
    import toolsets
    from pipeline import Pipeline
    from agent import load_config

    library = load_library(args.library)
    entries = [e for e in library["incidents"] if not args.only or e["id"] in args.only]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cfg = load_config(args.config)
    base = copy_store(args.store, out / "store.db")
    pipe = Pipeline(PROFILE, base, compute_every=360)               # the profile, events, registry and models
    stores = {"base": base}
    if any(e.get("faults") for e in entries):
        spans = {(e["faults"], e["replay"]["from"], e["replay"]["to"]) for e in entries if e.get("faults")}
        if len(spans) != 1:
            raise ValueError("the fault entries must share one faults file and span")
        faults, start, end = spans.pop()
        fault_db = out / "faults.db"
        if fault_db.exists():
            fault_db.unlink()
        fstore = Store(f"sqlite:///{fault_db}")
        fstore.init_schema()
        fpipe = Pipeline(PROFILE, fstore)
        import replayer
        t0 = time.monotonic()
        fpipe.replay(args.data, from_ts=to_utc(start).replace(tzinfo=None), to_ts=to_utc(end).replace(tzinfo=None),
                     faults=replayer.Faults.read(ROOT / faults, analog_names(pipe.profile) +
                                                 [s["name"] for s in pipe.profile["signals"]["digital"]]))
        print(f"replayed {start} - {end} with {faults} in {time.monotonic() - t0:.0f} s", file=sys.stderr)
        stores["faults"] = fstore
    skills = load_skills(pipe.profile, ROOT)
    corpus = Corpus.load(ROOT / "corpus")
    rows = []
    for e in entries:
        store = stores["faults" if e.get("faults") else "base"]
        server = toolsets.toolserver.ToolServer(store, pipe.profile, pipe.events, registry=pipe.registry,
                                                corpus=corpus, frozen=pipe.frozen)
        iid, how = find_incident(store, e, pipe.profile, pipe.events, pipe.registry)
        row = {"id": e["id"], "kind": e["kind"], "incident_id": iid, "found": how,
               "store": "faults.db" if e.get("faults") else "store.db"}
        if iid is None:
            rows.append(dict(row, outcome="not_found"))
            print(f"{e['id']}: {how}", file=sys.stderr)
            continue
        model = make_model(args.model, e, pipe.profile, cfg)
        r = runner_mod.Runner(tools=toolsets.InProcessTools(server), model=model, sink=sinks.DirectSink(store),
                              profile=pipe.profile, profile_sha256=profile_hash(PROFILE), skills=skills,
                              budget=runner_mod.Budget.from_config(cfg.get("budget") or {}), arm=args.arm,
                              strict_tools=bool(cfg["model"].get("strict_tools", True)),
                              survival_run=pipe.frozen.survival_run_id)
        state = r.run(iid, trace_id=f"T-{args.name}-{e['id']}")
        rows.append(dict(row, outcome=state["outcome"], trace_id=state["trace_id"], brief_id=state["brief_id"],
                         tool_calls=state["tool_calls"], turns=state["turns"], tokens=state["tokens"]["counted"],
                         seconds=state["elapsed_s"], budget_hit=state["budget_hit"]))
        print(f"{e['id']} {e['kind']:16s} {iid}: {state['outcome']} ({state['tool_calls']} calls)", file=sys.stderr)
    report = {"name": args.name, "model": args.model, "arm": args.arm, "library": str(args.library),
              "base_store": args.store, "at": iso_utc(dt.datetime.now(dt.timezone.utc)), "runs": rows}
    (out / "runs.json").write_text(json.dumps(report, indent=2, sort_keys=True))
    return report


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--store", default="sqlite:///runs/campaign.db", help="the replayed campaign (SQLite)")
    p.add_argument("--data", default=str(ROOT / "data" / "MetroPT3(AirCompressor).csv"),
                   help="the dataset, for the fault replays")
    p.add_argument("--library", default=str(LIBRARY))
    p.add_argument("--model", choices=("rules", "anthropic"), default="rules")
    p.add_argument("--arm", choices=("full", "retrieval_only"), default="full")
    p.add_argument("--config", default=None, help="agent config (default services/agent/config.yaml)")
    p.add_argument("--only", nargs="*", default=None, help="incident ids to run (default: all)")
    p.add_argument("--name", default=None, help="the run's name (default: <model>-<date>)")
    p.add_argument("--out", default=None, help="default eval/out/incidents/<name>")
    args = p.parse_args(argv)
    args.name = args.name or f"{args.model}-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M}"
    args.out = args.out or str(ROOT / "eval" / "out" / "incidents" / args.name)
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    report = run(args)
    missing = [r["id"] for r in report["runs"] if r["outcome"] == "not_found"]
    print(json.dumps({"out": args.out, "runs": len(report["runs"]), "not_found": missing}, sort_keys=True))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
