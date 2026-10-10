"""The agent's six tools, answered as of the incident (T15a; AGT-01 to AGT-05, AGT-09, AGT-10).

``ToolServer`` is the whole tool server without a transport: services/mcp_server/server.py
puts it behind the Model Context Protocol, and the agent's runner can call it in process
(the same code either way). For one agent run (``open_run``) it:

* serves only the allowlisted tools -- get_telemetry, attribute_anomaly,
  get_maintenance_history, search_knowledge, run_check, device_health -- and, in the
  retrieval-only study arm, search_knowledge alone (AGT-12). A call to anything else,
  or with arguments that fail the tool's input schema, is refused and logged (AGT-02);
* answers every tool as of the incident's opening: nothing recorded at or after
  ``as_of`` is read (AGT-04). Telemetry and checks come through pdm_common.asof, the
  maintenance history keeps only records that existed before the opening;
* stores every result in tool_results with a stable id ``R-<run>-<n>`` before the model
  sees it (AGT-03), and stores the run's first result, ``incident_context`` (the incident,
  its status and the prognosis as of the opening), which the brief cites for the
  prognosis (BRF-05);
* returns knowledge-corpus passages as quoted, inert data blocks (AGT-09) with their
  doc_id, offsets, licence, url and SHA-256 (AGT-10);
* dispatches run_check to the asset's registry named in the profile (AGT-05).

Times in results are relative to the opening ("minutes before the opening") unless the
server is told otherwise (``blind_dates=False``). The 2020 dataset is public, so an
absolute date would let a model that has read about it recognise a failure by its date;
the detector-selection notebook's triage test kept dates out of its packets for the
same reason.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pdm_common.asof import evidence_asof, telemetry_frame
from pdm_common.corpus import Corpus, quote
from pdm_common.diagnosis import run_token
from pdm_common.events import event_span
from pdm_common.profile import signal_names
from pdm_common.timeutil import iso_utc, to_utc

log = logging.getLogger("mcp_server")

ALLOWLIST = ("get_telemetry", "attribute_anomaly", "get_maintenance_history", "search_knowledge", "run_check",
             "device_health")
ARMS = {"full": ALLOWLIST, "retrieval_only": ("search_knowledge",)}
CONTEXT_TOOL = "incident_context"
MAX_POINTS = 500
SKILL_BAR = 0.15                       # PRD G2, as in services/prognostics


class ToolRefused(ValueError):
    """A call outside the allowlist or with arguments its schema refuses (AGT-02)."""


# --- input schemas (AGT-02) ----------------------------------------------------------------------

class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetTelemetry(_Args):
    signals: list[str] = Field(min_length=1, max_length=8, description="profile signal names")
    hours_before: float = Field(default=3.0, gt=0, le=168, description="how far back from the opening")
    max_points: int = Field(default=120, ge=10, le=MAX_POINTS, description="points per signal in the series")


class AttributeAnomaly(_Args):
    top: int = Field(default=5, ge=1, le=21)


class GetMaintenanceHistory(_Args):
    days_before: int = Field(default=180, ge=1, le=730)


class SearchKnowledge(_Args):
    query: str = Field(min_length=2, max_length=300)
    k: int = Field(default=3, ge=1, le=5)


class RunCheck(_Args):
    check_ids: Optional[list[str]] = Field(default=None, description="check ids such as C4; none: every check")


class DeviceHealth(_Args):
    pass


INPUTS = {"get_telemetry": GetTelemetry, "attribute_anomaly": AttributeAnomaly,
          "get_maintenance_history": GetMaintenanceHistory, "search_knowledge": SearchKnowledge,
          "run_check": RunCheck, "device_health": DeviceHealth}
DESCRIPTIONS = {
    "get_telemetry": "Summary statistics and a downsampled series of profile signals over the hours before the "
                     "incident opened (never after it).",
    "attribute_anomaly": "Which detector input features carry the anomaly score in the window that opened the "
                         "incident, with each feature's share of the score and its value against healthy operation.",
    "get_maintenance_history": "Failures, repairs, earlier incidents and decisions on this asset recorded before "
                               "the incident opened.",
    "search_knowledge": "BM25 search over the knowledge corpus; passages come back as quoted reference data with "
                        "doc_id, offsets, licence, url and SHA-256.",
    "run_check": "Run the asset's deterministic checks (C1-C11) on the data up to the opening; pass, fail or "
                 "inconclusive with the measured values.",
    "device_health": "The latest decoded status of the asset's drives and controllers before the opening (G8 "
                     "stretch; usually no device data).",
}


def tool_specs(arm: str = "full") -> list[dict]:
    """The tools of an arm as {name, description, input_schema}, for the model and the MCP listing."""
    return [{"name": name, "description": DESCRIPTIONS[name], "input_schema": INPUTS[name].model_json_schema()}
            for name in ARMS[arm]]


# --- one agent run ------------------------------------------------------------------------------

@dataclass
class Run:
    incident: dict
    trace_id: str
    arm: str
    token: str
    as_of: dt.datetime
    asset: str
    n: int = 0
    results: dict = field(default_factory=dict)       # result_id -> tool_result_v1
    refused: list = field(default_factory=list)

    @property
    def tools(self) -> tuple:
        return ARMS[self.arm]


def _num(x, digits: int = 3):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, digits) if math.isfinite(x) else None


class ToolServer:
    """The six tools over one store, for one asset profile."""

    def __init__(self, store, profile: dict, events: dict, *, registry, corpus: Optional[Corpus], frozen=None,
                 blind_dates: bool = True):
        self.store = store
        self.profile = profile
        self.events = events
        self.registry = registry
        self.corpus = corpus
        self.frozen = frozen
        self.blind = blind_dates
        self.asset = profile["asset"]["id"]
        self.signals = signal_names(profile)
        self.units = {s["name"]: s.get("unit") for s in profile["signals"]["analog"]}
        self.ranges = {s["name"]: s.get("range") for s in profile["signals"]["analog"]}

    # -- runs ------------------------------------------------------------------------------------
    def open_run(self, incident_id: str, trace_id: str, arm: str = "full") -> Run:
        """The run of ``trace_id`` on an incident. A run that already stored results (the agent or
        this server restarted mid-run) carries on from them: its ids continue where they stopped."""
        if arm not in ARMS:
            raise ToolRefused(f"unknown arm {arm!r}")
        incident = self.store.one("SELECT * FROM incidents WHERE incident_id = ?", [incident_id], table="incidents")
        if incident is None:
            raise ToolRefused(f"no incident {incident_id}")
        if incident["asset_id"] != self.asset:
            raise ToolRefused(f"{incident_id} is an incident of {incident['asset_id']}, not {self.asset}")
        as_of = to_utc(incident["as_of"] or incident["opened_at"])
        run = Run(incident, trace_id, arm, run_token(trace_id), as_of, self.asset)
        for row in self.store.query("SELECT * FROM tool_results WHERE incident_id = ? AND result_id LIKE ?",
                                    [incident_id, f"R-{run.token}-%"], table="tool_results"):
            run.results[row["result_id"]] = dict(row, schema="tool_result_v1")
            run.n = max(run.n, int(row["result_id"].rsplit("-", 1)[1]))
        return run

    def _when(self, run: Run, moment) -> object:
        """A time as the model sees it: minutes before the opening, or ISO when not blind."""
        moment = to_utc(moment) if not isinstance(moment, dt.datetime) else moment.astimezone(dt.timezone.utc)
        if not self.blind:
            return iso_utc(moment)
        return round((run.as_of - moment).total_seconds() / 60, 1)

    def _store(self, run: Run, tool: str, arguments: dict, output) -> dict:
        run.n += 1
        rid = f"R-{run.token}-{run.n}"
        now = dt.datetime.now(dt.timezone.utc)
        doc = {"schema": "tool_result_v1", "result_id": rid, "incident_id": run.incident["incident_id"],
               "tool": tool, "arguments": arguments, "as_of": iso_utc(run.as_of), "output": output,
               "created_at": iso_utc(now)}
        self.store.insert("tool_results", {"result_id": rid, "incident_id": doc["incident_id"], "tool": tool,
                                           "arguments": arguments, "as_of": doc["as_of"], "output": output,
                                           "created_at": doc["created_at"]})
        run.results[rid] = doc
        return doc

    # -- the context the runtime stores first -------------------------------------------------------
    def context(self, run: Run) -> dict:
        """incident_context: the incident, its status and the prognosis as of the opening (stored, citable).
        Stored once per run, as its first result; asked again, it returns that result."""
        first = run.results.get(f"R-{run.token}-1")
        if first is not None and first["tool"] == CONTEXT_TOOL:
            return first
        if run.n:
            raise ToolRefused(f"run {run.trace_id} has results but no incident_context first")
        inc = run.incident
        gate = inc.get("gate") or {}
        before = iso_utc(run.as_of)
        scores = self.store.query("SELECT window_end, score, threshold, alert FROM scores WHERE asset_id = ? AND "
                                  "window_end <= ? ORDER BY window_end DESC LIMIT 30",
                                  [self.asset, self._db_time("scores", "window_end", before)], table="scores")
        opening = scores[0] if scores else None
        status = self.store.one("SELECT to_status, cause, at FROM status_log WHERE asset_id = ? AND at <= ? "
                                "ORDER BY at DESC LIMIT 1", [self.asset, self._db_time("status_log", "at", before)],
                                table="status_log")
        prognosis = self.store.one("SELECT * FROM prognoses WHERE asset_id = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
                                   [self.asset, self._db_time("prognoses", "ts", before)], table="prognoses")
        if prognosis:
            skill, meets = None, None
            if self.frozen is not None and self.frozen.survival_bundle:
                metrics = self.frozen.survival_bundle.get("metrics") or {}
                skill = {k[len("skill_"):]: _num(v) for k, v in metrics.items() if k.startswith("skill_")}
                if skill:
                    meets = sum(skill.values()) / len(skill) >= SKILL_BAR
            prog = {"available": True, "horizons_h": prognosis["probabilities"],
                    "made_minutes_before_opening" if self.blind else "made_at": self._when(run, prognosis["ts"]),
                    "model_run_id": prognosis["model_run_id"], "interval": 0.9,
                    "brier_skill_vs_constant_hazard": skill, "skill_bar": SKILL_BAR, "meets_skill_bar": meets}
            if meets is False:
                prog["note"] = ("the survival model does not beat a constant hazard by the skill bar; "
                                "report the probabilities as context, not as a forecast")
        else:
            prog = {"available": False, "reason": "no prognosis was recorded before the opening"}
        opened = self.store.one("SELECT cause FROM incident_events WHERE incident_id = ? AND to_state = ? "
                                "ORDER BY id LIMIT 1", [inc["incident_id"], "open"])
        triage = inc.get("triage") or {}
        output = {
            "incident": {"incident_id": inc["incident_id"], "asset": self.asset,
                         "opened": "t = 0, the opening; times are minutes before it" if self.blind
                         else iso_utc(run.as_of),
                         "cause": opened["cause"] if opened else None,
                         "gate": {k: gate.get(k) for k in ("span", "min_fraction", "hold_off", "alerting", "present")},
                         "detector_run_id": inc.get("detector_run_id")},
            "scores_behind_the_gate": {
                "windows": len(scores), "over_threshold": sum(1 for s in scores if s["alert"]),
                "threshold": _num(opening["threshold"]) if opening else None,
                "score_at_opening": _num(opening["score"]) if opening else None,
                "max_score": _num(max(s["score"] for s in scores)) if scores else None,
                "trajectory": [[self._when(run, s["window_end"]), _num(s["score"], 2), bool(s["alert"])]
                               for s in reversed(scores)]},
            "status": {"ne107": status["to_status"], "cause": status["cause"]} if status else None,
            "prognosis": prog,
            # the checks the gate ran at the opening, and what each triage rule would have done
            # (evidence only: no rule removes an alarm)
            "checks_at_opening": [{k: c.get(k) for k in ("id", "check", "kind", "result", "measured")}
                                  for c in triage.get("checks") or []],
            "triage_rules": triage.get("triage"),
        }
        if self.blind:
            output["scores_behind_the_gate"]["trajectory_columns"] = ["minutes_before_opening", "score", "alert"]
        return self._store(run, CONTEXT_TOOL, {}, output)

    def _db_time(self, table: str, column: str, iso: str):
        from pdm_common.store import _to_db
        return _to_db(table, column, iso, self.store.dialect)

    # -- calls -------------------------------------------------------------------------------------
    def call(self, run: Run, tool: str, arguments: Optional[dict] = None) -> dict:
        """Run one tool call and return its stored result; ToolRefused for a refused call."""
        arguments = dict(arguments or {})
        if tool not in run.tools:
            run.refused.append({"tool": tool, "arguments": arguments, "reason": "not in the allowlist"})
            log.warning("refused %s: not in the allowlist of arm %s", tool, run.arm,
                        extra={"incident_id": run.incident["incident_id"], "trace_id": run.trace_id})
            raise ToolRefused(f"{tool} is not a tool of this run; the tools are {', '.join(run.tools)}")
        try:
            args = INPUTS[tool].model_validate(arguments)
        except ValidationError as exc:
            reason = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors())
            run.refused.append({"tool": tool, "arguments": arguments, "reason": reason})
            log.warning("refused %s: %s", tool, reason, extra={"incident_id": run.incident["incident_id"]})
            raise ToolRefused(f"{tool}: {reason}") from None
        output = getattr(self, f"_{tool}")(run, args)
        return self._store(run, tool, args.model_dump(), output)

    # -- the tools ---------------------------------------------------------------------------------
    def _get_telemetry(self, run: Run, a: GetTelemetry) -> dict:
        unknown = [s for s in a.signals if s not in self.signals]
        if unknown:
            raise ToolRefused(f"get_telemetry: unknown signal(s) {', '.join(unknown)}; the asset has "
                              f"{', '.join(self.signals)}")
        start = run.as_of - dt.timedelta(hours=a.hours_before)
        frame = telemetry_frame(self.store, self.profile, self.asset, start, run.as_of)
        out = {"window": {"from_minutes_before_opening": round(a.hours_before * 60, 1), "to": 0}
               if self.blind else {"from": iso_utc(start), "to": iso_utc(run.as_of)},
               "records": int(len(frame)), "signals": {}}
        if frame.empty:
            out["note"] = "no telemetry in this span"
            return out
        times = pd.DatetimeIndex(frame["timestamp"]).tz_localize("UTC")
        minutes = ((pd.Timestamp(run.as_of) - times).total_seconds() / 60).to_numpy()
        gaps = np.diff(times.asi8) / 1e9 if len(times) > 1 else np.array([])
        out["largest_gap_minutes"] = _num(gaps.max() / 60, 1) if len(gaps) else 0.0
        for name in a.signals:
            x = frame[name].to_numpy(dtype=float)
            ok = np.isfinite(x)
            entry = {"unit": self.units.get(name), "plausible_range": self.ranges.get(name), "n": int(ok.sum())}
            if ok.any():
                v = x[ok]
                entry.update(first=_num(v[0]), last=_num(v[-1]), min=_num(v.min()), max=_num(v.max()),
                             mean=_num(v.mean()))
                entry["series"] = self._downsample(minutes[ok], v, a.max_points, run)
            out["signals"][name] = entry
        return out

    def _downsample(self, minutes_before: np.ndarray, values: np.ndarray, points: int, run: Run) -> list:
        """At most ``points`` [time, mean] pairs over equal spans of time, oldest first."""
        if len(values) <= points:
            pairs = zip(minutes_before, values)
        else:
            edges = np.linspace(minutes_before.max(), minutes_before.min(), points + 1)
            idx = np.clip(np.searchsorted(-minutes_before, -edges[1:-1]), 0, len(values))
            parts = np.split(np.arange(len(values)), idx)
            pairs = [(minutes_before[p].mean(), values[p].mean()) for p in parts if len(p)]
        if self.blind:
            return [[_num(m, 1), _num(v)] for m, v in pairs]
        return [[iso_utc(run.as_of - dt.timedelta(minutes=float(m))), _num(v)] for m, v in pairs]

    def _attribute_anomaly(self, run: Run, a: AttributeAnomaly) -> dict:
        row = self.store.one("SELECT * FROM scores WHERE asset_id = ? AND window_end = ?",
                             [self.asset, self._db_time("scores", "window_end", iso_utc(run.as_of))], table="scores")
        if row is None:
            return {"available": False, "reason": "no score recorded for the window that opened the incident"}
        errors = row["errors"] or {}
        total = sum(errors.values()) or 1.0
        features = sorted(errors.items(), key=lambda kv: -kv[1])[: a.top]
        window = self.store.one("SELECT features FROM windows WHERE asset_id = ? AND window_end = ?",
                                [self.asset, self._db_time("windows", "window_end", iso_utc(run.as_of))],
                                table="windows")
        values = (window or {}).get("features") or {}
        scaler = {}
        if self.frozen is not None:
            b = self.frozen.detector_bundle
            scaler = {f: (m, s, med) for f, m, s, med in zip(b["features"], b["scaler"]["mean"], b["scaler"]["std"],
                                                              b["scaler"]["healthy_median"])}
        top = []
        for name, err in features:
            item = {"feature": name, "share_of_score": _num(err / total), "squared_error": _num(err)}
            v = values.get(name)
            if name in scaler:
                mean, std, median = scaler[name]
                item["healthy_median"] = _num(median)
                if v is not None:
                    item["value"] = _num(v)
                    z = (float(v) - mean) / std
                    item["z_vs_healthy"] = _num(z, 1)
                    item["direction"] = "above healthy" if z > 0 else "below healthy"
                else:
                    item["value"] = None
                    item["note"] = "missing in this window; scored at the healthy median"
            top.append(item)
        return {"score": _num(row["score"]), "threshold": _num(row["threshold"]),
                "score_over_threshold": _num(row["score"] / row["threshold"], 2) if row["threshold"] else None,
                "detector_run_id": row["detector_run_id"], "top_features": top,
                "note": "attribution names what does not fit healthy operation, not the cause; "
                        "correlated features share the error"}

    def _get_maintenance_history(self, run: Run, a: GetMaintenanceHistory) -> dict:
        since = run.as_of - dt.timedelta(days=a.days_before)
        records = []

        def add(kind: str, at: dt.datetime, text: str, **extra):
            if since <= at < run.as_of:                     # recorded before the opening only (AGT-04)
                records.append({"kind": kind, "when": self._when(run, at), "description": text, **extra})

        for f in self.events.get("failures") or []:
            start, end = event_span(f)
            hours = (end - start).total_seconds() / 3600
            add("failure_report", end, f"failure reported: {str(f.get('type', 'failure')).replace('_', ' ')}, "
                f"lasting {hours:.1f} h", duration_hours=_num(hours, 1))
            if f.get("maintenance"):
                add("repair", to_utc(f["maintenance"]), "repair carried out after the reported failure")
        for e in self.events.get("expert_reported") or []:
            if e.get("in_failure"):
                continue                                    # the same report as a failure above
            start, end = to_utc(e["start"]), to_utc(e["end"])
            minutes = (end - start).total_seconds() / 60
            add("failure_report", end, f"failure logged by the maintainer, lasting {minutes:.0f} min",
                duration_minutes=_num(minutes, 0))
        before = self._db_time("incidents", "opened_at", iso_utc(run.as_of))
        for inc in self.store.query("SELECT incident_id, opened_at FROM incidents WHERE asset_id = ? AND "
                                    "opened_at < ? ORDER BY opened_at", [self.asset, before], table="incidents"):
            decided = self.store.query("SELECT action, at FROM decisions WHERE incident_id = ? AND at < ? ORDER BY at",
                                       [inc["incident_id"], self._db_time("decisions", "at", iso_utc(run.as_of))],
                                       table="decisions")
            outcome = decided[-1]["action"] if decided else "no decision before this incident"
            add("earlier_incident", to_utc(inc["opened_at"]), f"earlier incident; decision: {outcome}")
        for wo in self.store.query("SELECT wo_id, sent_at, body FROM work_orders WHERE asset_id = ? AND sent_at < ? "
                                   "ORDER BY sent_at", [self.asset, self._db_time("work_orders", "sent_at",
                                                                                   iso_utc(run.as_of))],
                                   table="work_orders"):
            add("work_order", to_utc(wo["sent_at"]), f"work order sent: {(wo['body'] or {}).get('title', wo['wo_id'])}")
        records.sort(key=lambda r: r["when"], reverse=not self.blind)      # the most recent first
        return {"days_before": a.days_before, "records": records,
                "note": "times are minutes before the opening" if self.blind else None}

    def _search_knowledge(self, run: Run, a: SearchKnowledge) -> dict:
        if self.corpus is None:
            return {"hits": [], "note": "no knowledge corpus loaded"}
        hits = self.corpus.search(a.query, k=a.k)
        return {"query": a.query, "hits": [
            {"doc_id": h["doc_id"], "passage_no": h["passage_no"], "start": h["start"], "end": h["end"],
             "licence": h["licence"], "url": h["url"], "sha256": h["sha256"], "score": _num(h["score"]),
             "text": quote(h)} for h in hits]}

    def _run_check(self, run: Run, a: RunCheck) -> dict:
        ids = a.check_ids or None
        if ids:
            unknown = [i for i in ids if i not in self.registry.checks]
            if unknown:
                raise ToolRefused(f"run_check: unknown check(s) {', '.join(unknown)}; the asset has "
                                  f"{', '.join(self.registry.ids)}")
        ev = evidence_asof(self.store, self.profile, self.events, self.asset, iso_utc(run.as_of))
        return {"results": [r.as_dict() for r in self.registry.run(ev, ids)]}

    def _device_health(self, run: Run, a: DeviceHealth) -> dict:
        rows = self.store.query("SELECT * FROM device_events WHERE asset_id = ? AND ts < ? ORDER BY ts",
                                [self.asset, self._db_time("device_events", "ts", iso_utc(run.as_of))],
                                table="device_events")
        if not rows:
            return {"available": False, "note": "no device data recorded for this asset before the opening"}
        latest = {}
        for r in rows:
            latest[(r["device"], r["attribute"])] = r
        return {"available": True, "devices": [
            {"device": r["device"], "attribute": r["attribute"], "decoded": r["decoded"], "severity": r["severity"],
             "ne107_class": r["ne107_class"], "when": self._when(run, r["ts"])} for r in latest.values()]}


def preview(result: dict) -> str:
    """A tool result as the model reads it: its id, the tool and its output as JSON."""
    return json.dumps({"result_id": result["result_id"], "tool": result["tool"], "output": result["output"]},
                      sort_keys=True, ensure_ascii=False)
