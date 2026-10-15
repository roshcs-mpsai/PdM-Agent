"""Deterministic checks: the shared machinery (T15b; AGT-05, PR-10).

A check is a pure function from an ``Evidence`` object to a ``CheckResult``:
pass, fail or inconclusive, with the measured value in words and numbers,
the rule and what a failure points to. The functions live per asset in
``checks/<asset>/`` (the profile's ``references.checks_registry`` names the
folder) and may name that asset's signals; this module holds what every asset
shares:

* ``Evidence``: what the checks of one alarm may read -- the 10 s grid up to
  the moment the incident opens (``pdm_common.quality.to_grid`` output), the
  opening window's features, the gaps in the data before it, and the
  thresholds of the profile's ``consistency_checks``. Nothing at or after the
  opening time is visible, so the checks are as-of reads by construction.
* ``load_registry``: imports ``checks/<asset>/`` and returns its ``REGISTRY``
  (check id -> function) and ``triage`` module.
* ``run_checks``: runs some or all checks and returns their results.

The evidence spans come from the profile and events.yaml, never from code:
the data behind an alarm is the gate span plus one window (40 min for
MetroPT-3), a gap is a run of ``gap_edges.after_gaps_of`` (30 min) without a
valid grid point, and a restart is data resuming less than the evidence span
plus ``gap_edges.length`` (55 min) before the alarm. Ported from
notebooks/detector_selection_v7.2.ipynb Section 8.2 (``evidence_packet``).
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import asdict, dataclass
from functools import cached_property
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

from pdm_common.gate import gate_params
from pdm_common.timeutil import parse_duration

RESULTS = ("pass", "fail", "inconclusive")
KINDS = ("data_quality", "fault_signature", "instrument")


@dataclass(frozen=True)
class CheckResult:
    id: str
    check: str
    kind: str
    result: str
    measured: str
    value: Optional[float] = None
    threshold: Optional[float] = None
    unit: Optional[str] = None
    rule: str = ""
    points_to: str = ""

    def __post_init__(self):
        if self.result not in RESULTS:
            raise ValueError(f"{self.id}: result must be one of {RESULTS}, not {self.result!r}")

    def as_dict(self) -> dict:
        d = asdict(self)
        if d["value"] is not None and not np.isfinite(d["value"]):
            d["value"] = None
        return d


@dataclass(frozen=True)
class Gap:
    before: pd.Timestamp        # last valid grid point before the gap
    after: pd.Timestamp         # first valid grid point after it: data resumed
    hours: float                # from ``before`` to ``after``
    before_pos: int
    after_pos: int


def _td(text) -> pd.Timedelta:
    return text if isinstance(text, pd.Timedelta) else pd.Timedelta(parse_duration(text))


class Evidence:
    """What the checks of the alarm opening at ``opens`` may read.

    ``grid``: the masked, bridged 10 s grid (signals plus ``observed`` and
    ``bridged``), indexed by aware UTC times; rows at or after ``opens`` are
    dropped here. ``window``: the opening window's features (the window that
    ends at ``opens``). ``profile`` and ``events``: thresholds, gate and gap
    rules.
    """

    def __init__(self, opens, grid: pd.DataFrame, window: dict, *, profile: dict, events: dict):
        opens = pd.Timestamp(opens)
        self.opens = opens.tz_localize("UTC") if opens.tz is None else opens.tz_convert("UTC")
        index = grid.index if grid.index.tz is not None else grid.index.tz_localize("UTC")
        keep = index < self.opens
        self.grid = grid.loc[keep]
        self.t = index[keep]
        self.window = dict(window)
        self.profile = profile
        self.checks = {c["id"]: c for c in profile.get("consistency_checks") or [] if c.get("id")}
        gate = gate_params(profile)
        self.span = pd.Timedelta(gate.span_ns)
        self.hold_off = pd.Timedelta(gate.hold_off_ns)
        self.min_present = gate.min_present
        self.stride = _td(profile["windowing"]["stride"])
        self.window_length = _td(profile["windowing"]["length"])
        self.step = _td(profile["sampling"]["resample_rule"])
        self.behind_span = self.span + self.window_length            # the data behind the gate: 40 min
        edges = events.get("gap_edges") or {}
        self.gap_min = _td(edges.get("after_gaps_of", "30min"))
        self.restart_edge = _td(edges.get("length", "15min"))
        self.restart_within = self.behind_span + self.restart_edge    # 55 min

    # -- the grid -------------------------------------------------------------------------
    @cached_property
    def valid(self) -> np.ndarray:
        return (self.grid["observed"] | self.grid["bridged"]).to_numpy()

    def channel(self, name: str) -> np.ndarray:
        """A signal with NaN wherever the grid holds no data."""
        x = self.grid[name].to_numpy(dtype=float).copy()
        x[~self.valid] = np.nan
        return x

    def rows(self, start, end=None) -> slice:
        """Grid rows in [start, end) (end defaults to the opening)."""
        end = self.opens if end is None else end
        return slice(int(self.t.searchsorted(start)), int(self.t.searchsorted(end)))

    def behind(self) -> slice:
        """The rows behind the gate: [opens - span - one window, opens)."""
        return self.rows(self.opens - self.behind_span)

    @property
    def points_per_minute(self) -> int:
        return int(pd.Timedelta(minutes=1) / self.step)

    # -- gaps -------------------------------------------------------------------------------
    @cached_property
    def gaps(self) -> list[Gap]:
        """Interior runs of at least ``gap_min`` without a valid point, oldest first.
        A logger freeze is masked off the grid, so it counts as a gap here."""
        invalid = (~self.valid).astype(np.int8)
        edges = np.diff(np.r_[0, invalid, 0])
        first, end = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
        long = ((end - first) * self.step >= self.gap_min) & (first > 0) & (end < len(invalid))
        out = []
        for a, b in zip(first[long] - 1, end[long]):
            out.append(Gap(self.t[a], self.t[b], float((b - a) * self.step / pd.Timedelta(hours=1)), int(a), int(b)))
        return out

    def last_gap(self) -> Optional[Gap]:
        """The last gap after which data resumed at or before the opening."""
        return self.gaps[-1] if self.gaps else None

    # -- thresholds -------------------------------------------------------------------------
    def entry(self, check_id: str) -> dict:
        try:
            return self.checks[check_id]
        except KeyError:
            raise KeyError(f"the profile declares no consistency check {check_id}") from None

    def threshold(self, check_id: str) -> Optional[float]:
        value = self.entry(check_id).get("threshold")
        return None if value is None else float(value)

    def param(self, check_id: str, key: str, default=None):
        return (self.entry(check_id).get("params") or {}).get(key, default)

    def feature(self, name: str) -> float:
        v = self.window.get(name)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else float("nan")

    def result(self, check_id: str, result: str, measured: str, *, value=None, threshold=None) -> CheckResult:
        """A result carrying the registry entry's name, kind, unit, rule and meaning."""
        e = self.entry(check_id)
        return CheckResult(id=check_id, check=e["name"], kind=e.get("kind") or "data_quality", result=result,
                           measured=measured, value=None if value is None else float(value),
                           threshold=self.threshold(check_id) if threshold is None else float(threshold),
                           unit=e.get("unit"), rule=e.get("rule", ""), points_to=e.get("meaning") or "")


def ago(minutes: float) -> str:
    """A duration in words: minutes under two hours, hours beyond."""
    return f"{minutes:.0f} min" if minutes < 120 else f"{minutes / 60:.1f} h"


# --- the per-asset registry -------------------------------------------------------------------

@dataclass
class Registry:
    asset: str
    folder: Path
    checks: dict[str, Callable[[Evidence], CheckResult]]
    module: object

    @property
    def ids(self) -> list[str]:
        return list(self.checks)

    def run(self, evidence: Evidence, ids=None) -> list[CheckResult]:
        """The named checks (default: all, in registry order). An unknown id raises."""
        ids = list(ids) if ids else self.ids
        unknown = [i for i in ids if i not in self.checks]
        if unknown:
            raise KeyError(f"unknown check(s) {', '.join(unknown)}; the registry has {', '.join(self.ids)}")
        return [self.checks[i](evidence) for i in ids]

    def triage(self, results: list[CheckResult]) -> dict:
        """The asset's triage rules as evidence (checks/<asset>/triage.py); {} if it has none."""
        fn = getattr(self.module, "triage_evidence", None)
        return fn(results) if fn else {}


def registry_folder(profile: dict, root: Path) -> Path:
    return Path(root) / profile["references"]["checks_registry"]


def load_registry(profile: dict, root: Path) -> Registry:
    """Import ``checks/<asset>/`` (the profile's references.checks_registry) as a package."""
    folder = registry_folder(profile, root)
    init = folder / "__init__.py"
    if not init.exists():
        raise FileNotFoundError(f"{folder}: no checks registry (expected __init__.py with REGISTRY)")
    name = f"pdm_checks_{folder.name}"
    if name in sys.modules:
        module = sys.modules[name]
    else:
        spec = importlib.util.spec_from_file_location(name, init, submodule_search_locations=[str(folder)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    declared = {c["id"] for c in profile.get("consistency_checks") or [] if c.get("id")}
    missing = [i for i in module.REGISTRY if i not in declared]
    if missing:
        raise KeyError(f"{folder}: check(s) {', '.join(missing)} have no consistency_checks entry in the profile")
    return Registry(profile["asset"]["id"], folder, dict(module.REGISTRY), module)
