"""Window features from the 10 s grid (T5; ING-12, DET-04).

``window_features`` turns the masked, bridged grid into one row of features
per window, using only grid points up to the window's last point, so the
streaming ingest service computes exactly what the batch harness evaluated.
Which features exist, what each one computes and with which parameters is
declared in the asset's ``features.yaml`` (``references.features_file`` in
the profile). The code here knows *kinds* of feature -- a share of time, a
deficit against a trailing median, a rate across a gap -- and reads channels
through *roles* (``motor_current``, ``panel_pressure`` ...) that the YAML
maps to the asset's signals, so this file names no asset channel (CFG-03).

Ported from notebooks/metropt3_eda_v4_clean.ipynb Section 8.1
(``window_features``, ``trailing_sum``, ``trailing_mean_of_steps``). The data
test in tests/test_features.py checks that the port reproduces the saved
window table within 1e-6.

Two upstream steps look ahead and are the streaming path's to handle: bridging
in ``pdm_common.quality.to_grid`` reads up to two later points (20 s), and a
logger freeze is only known once it has lasted ``freeze_rule.min_duration``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from pdm_common.profile import ProfileError, read_yaml
from pdm_common.timeutil import parse_duration

SUMMARY_STATS = ("mean", "std", "min", "max", "slope")

# kind -> the parameters it requires (beyond `kind`)
KINDS: dict[str, tuple[str, ...]] = {
    "summary": ("signal", "stat"),
    "share_of_time": ("role", "window", "min_data"),          # plus exactly one of above / below
    "hours_since_below": ("role", "below", "stop_gap"),
    "deficit_vs_median": ("role", "baseline", "min_coverage"),
    "rise": ("role", "lag"),
    "coverage": ("window",),
    "minutes_since_resume": ("stop_gap", "horizon"),
    "gap_hours": ("stop_gap", "horizon", "cap_hours"),
    "value_at_resume": ("role", "stop_gap", "horizon"),
    "refill_rate": ("role", "loaded_above", "below", "window", "min_minutes"),
    "idle_loss": ("role", "loaded_above", "window", "min_minutes"),
    "share_trend": ("of", "role", "above", "window", "min_coverage"),
    "minutes_active": ("role", "window"),
    "gap_loss_rate": ("role", "stop_gap", "horizon"),
    "command_without_motion": ("role", "motor_below"),
    "bridged_points": (),
}
ROLES_READ = {  # kinds that also read the motor current role besides their own
    "refill_rate": "motor_current", "idle_loss": "motor_current",
    "command_without_motion": "motor_current",
}


class FeatureSpecError(ProfileError):
    """features.yaml cannot be read or is inconsistent."""


@dataclass(frozen=True)
class FeatureSpec:
    roles: dict[str, str]
    summary_inputs: list[str]
    features: list[dict]
    window_length: pd.Timedelta
    stride: pd.Timedelta
    grid_step: pd.Timedelta
    path: Path | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def names(self) -> list[str]:
        return [f["name"] for f in self.features]

    @property
    def win_bins(self) -> int:
        return int(self.window_length / self.grid_step)

    def bins(self, duration) -> int:
        """Grid points in a duration ('6h', '1min', a Timedelta)."""
        if not isinstance(duration, pd.Timedelta):
            duration = pd.Timedelta(parse_duration(duration))
        return int(duration / self.grid_step)

    def flagged(self, flag: str = "detector_v1") -> list[str]:
        """Feature names whose ``meta.<flag>`` is true, in declaration order."""
        return [f["name"] for f in self.features if (f.get("meta") or {}).get(flag)]

    def by_role(self, role: str) -> list[str]:
        return [f["name"] for f in self.features if (f.get("meta") or {}).get("role") == role]

    def max_lookback(self) -> pd.Timedelta:
        """The longest look-back any feature reads: the fold look-back (EDA 7.4)."""
        longest = self.window_length
        for f in self.features:
            c = f["compute"]
            for key in ("window", "baseline", "lag", "horizon"):
                if key in c:
                    longest = max(longest, pd.Timedelta(parse_duration(c[key])))
        return longest

    def channel(self, role: str) -> str:
        return self.roles[role]

    def dictionary(self) -> pd.DataFrame:
        """The feature dictionary in the EDA's column layout."""
        rows = []
        for f in self.features:
            meta = f.get("meta") or {}
            rows.append({"feature": f["name"], "definition": f.get("desc", ""),
                         "unit": meta.get("unit", ""), "look-back": meta.get("look_back", ""),
                         "role": meta.get("role", ""), "detector_v1": bool(meta.get("detector_v1", False)),
                         "drop_for_tabular": bool(meta.get("drop_for_tabular", False))})
        return pd.DataFrame(rows)


def features_file(profile: dict, profile_path) -> Path:
    """The features file named in the profile, resolved next to the profile."""
    name = (profile.get("references") or {}).get("features_file", "features.yaml")
    return Path(profile_path).resolve().parent / name


def load_feature_spec(path) -> FeatureSpec:
    """Read and check features.yaml: every kind known, its parameters present,
    every role mapped, names unique."""
    path = Path(path)
    data = read_yaml(path, error=FeatureSpecError, what="features file")
    problems = []
    roles = data.get("roles") or {}
    window = data.get("window") or {}
    summary = data.get("summary") or {}
    feats = data.get("features") or []
    names = [f.get("name") for f in feats if isinstance(f, dict)]
    for name in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"feature {name!r} is declared twice")
    for i, f in enumerate(feats):
        if not isinstance(f, dict) or "name" not in f or not isinstance(f.get("compute"), dict):
            problems.append(f"features[{i}]: needs a name and a compute mapping")
            continue
        c = f["compute"]
        kind = c.get("kind")
        if kind not in KINDS:
            problems.append(f"{f['name']}: unknown kind {kind!r}")
            continue
        for key in KINDS[kind]:
            if key not in c:
                problems.append(f"{f['name']}: kind {kind} needs {key!r}")
        if kind == "share_of_time" and ("above" in c) == ("below" in c):
            problems.append(f"{f['name']}: give exactly one of above or below")
        for role in [c.get("role"), ROLES_READ.get(kind)]:
            if role is not None and role not in roles:
                problems.append(f"{f['name']}: role {role!r} is not mapped in roles")
        if kind == "summary":
            if c.get("signal") not in (summary.get("inputs") or []):
                problems.append(f"{f['name']}: {c.get('signal')!r} is not a summary input")
            if c.get("stat") not in SUMMARY_STATS:
                problems.append(f"{f['name']}: unknown statistic {c.get('stat')!r}")
        if kind == "share_trend" and c.get("of") not in names[:i]:
            problems.append(f"{f['name']}: 'of' must name an earlier feature")
    for key in ("length", "stride", "grid_step"):
        if key not in window:
            problems.append(f"window.{key} is missing")
    if problems:
        raise FeatureSpecError(f"{path}: features file is inconsistent:\n  " + "\n  ".join(problems))
    td = lambda text: pd.Timedelta(parse_duration(text))   # noqa: E731
    return FeatureSpec(roles=dict(roles), summary_inputs=list(summary.get("inputs") or []),
                       features=feats, window_length=td(window["length"]), stride=td(window["stride"]),
                       grid_step=td(window["grid_step"]), path=path, raw=data)


# --- trailing sums ---------------------------------------------------------------

def trailing_sum(values, n: int) -> np.ndarray:
    """Per point, the sum of ``values`` over the trailing n points (the point
    itself included); NaN counts as 0."""
    c = np.r_[0.0, np.cumsum(np.nan_to_num(np.asarray(values, dtype=float), nan=0.0))]
    i = np.arange(1, len(c))
    return c[i] - c[np.maximum(i - n, 0)]


def trailing_mean_of_steps(per_step, steps, n: int, min_steps: int) -> np.ndarray:
    """Per point, the mean of ``per_step`` over the chosen steps among the
    trailing n; NaN with fewer than min_steps such steps. A step joins points
    i and i + 1, so the result is shifted onto the points."""
    total = trailing_sum(np.where(steps, per_step, 0.0), n)
    count = trailing_sum(np.asarray(steps, dtype=float), n)
    return np.r_[np.nan, np.where(count >= min_steps, total / np.maximum(count, 1), np.nan)]


# --- the features ---------------------------------------------------------------

class _Grid:
    """Shared intermediate arrays, computed once per call and cached by parameter."""

    def __init__(self, grid: pd.DataFrame, spec: FeatureSpec):
        self.grid, self.spec = grid, spec
        self.t = grid.index
        self.valid = (grid["observed"] | grid["bridged"]).to_numpy()
        self.n = len(grid)
        self._masked: dict[str, np.ndarray] = {}
        self._cache: dict = {}

    def masked(self, channel: str) -> np.ndarray:
        """The channel with every point that is neither observed nor bridged set to NaN."""
        if channel not in self._masked:
            x = self.grid[channel].to_numpy(dtype=float).copy()
            x[~self.valid] = np.nan
            self._masked[channel] = x
        return self._masked[channel]

    def raw(self, channel: str) -> np.ndarray:
        return self.grid[channel].to_numpy(dtype=float)

    def cached(self, key, make):
        if key not in self._cache:
            self._cache[key] = make()
        return self._cache[key]

    def have(self, n: int) -> np.ndarray:
        return self.cached(("have", n), lambda: trailing_sum(self.valid.astype(float), n))

    def missing_runs(self):
        def make():
            missing = ~self.valid
            run_id = np.cumsum(np.r_[True, missing[1:] != missing[:-1]])
            return missing, np.bincount(run_id)[run_id]
        return self.cached("runs", make)

    def long_gap(self, stop_bins: int) -> np.ndarray:
        def make():
            missing, run_len = self.missing_runs()
            return missing & (run_len >= stop_bins)
        return self.cached(("long_gap", stop_bins), make)

    def resumes(self, stop_bins: int, horizon: int):
        """Resumption after each missing run of ``stop_bins`` or more: per point,
        the index of the last resumption, points since it (capped at the horizon)
        and whether it is recent."""
        def make():
            long_gap = self.long_gap(stop_bins)
            _, run_len = self.missing_runs()
            idx = np.arange(self.n)
            resume_pos = np.flatnonzero(np.r_[False, long_gap[:-1]] & self.valid)
            if len(resume_pos) == 0:
                k = np.full(self.n, -1)
                return resume_pos, np.empty(0, int), k, np.zeros(self.n, int), \
                    np.full(self.n, horizon), np.zeros(self.n, bool)
            gap_len = run_len[np.maximum(resume_pos - 1, 0)]
            k = np.searchsorted(resume_pos, idx, side="right") - 1
            k_safe = np.maximum(k, 0)
            since = np.where(k >= 0, idx - resume_pos[k_safe], horizon)
            return resume_pos, gap_len, k, k_safe, since, since < horizon
        return self.cached(("resumes", stop_bins, horizon), make)


def _share_state(c: dict, current: np.ndarray) -> np.ndarray:
    return current > float(c["above"]) if "above" in c else current < float(c["below"])


def window_features(grid: pd.DataFrame, ends: np.ndarray, spec: FeatureSpec) -> pd.DataFrame:
    """Features per window, in the order features.yaml declares them.

    ``grid`` is the output of ``pdm_common.quality.to_grid``: a regular,
    sorted DatetimeIndex with the signal columns plus ``observed`` and
    ``bridged``. ``ends`` are the grid positions of each window's last point.
    """
    g = _Grid(grid, spec)
    ends = np.asarray(ends, dtype=int)
    wb = spec.win_bins
    per_min, per_hour = spec.bins("1min"), spec.bins("1h")
    starts = g.t[ends - (wb - 1)]
    out: dict[str, np.ndarray] = {}

    for f in spec.features:
        name, c = f["name"], f["compute"]
        kind = c["kind"]
        channel = spec.roles.get(c.get("role")) if c.get("role") else None

        if kind == "summary":
            x = pd.Series(g.masked(c["signal"]))
            stat = c["stat"]
            if stat == "slope":
                out[name] = (x - x.shift(wb - 1)).to_numpy()[ends]
            else:
                out[name] = getattr(x.rolling(wb, min_periods=wb), stat)().to_numpy()[ends]

        elif kind == "share_of_time":
            n, need = spec.bins(c["window"]), spec.bins(c["min_data"])
            have = g.have(n)
            state = _share_state(c, g.masked(channel)) & g.valid
            share = trailing_sum(state.astype(float), n) / np.maximum(have, 1)
            out[name] = np.where(have >= need, share, np.nan)[ends]

        elif kind == "hours_since_below":
            stopped = (g.masked(channel) < float(c["below"])) | g.long_gap(spec.bins(c["stop_gap"]))
            last_stop = pd.Series(g.t.where(stopped), index=g.t).ffill()
            since = (pd.Series(g.t, index=g.t) - last_stop).dt.total_seconds() / 3600
            out[name] = since.where(g.valid).to_numpy()[ends]

        elif kind == "deficit_vs_median":
            baseline_td = pd.Timedelta(parse_duration(c["baseline"]))
            minutes = pd.Series(g.masked(channel), index=g.t).resample("1min").mean()
            need = int(float(c["min_coverage"]) * baseline_td / pd.Timedelta(minutes=1))
            baseline = minutes.rolling(baseline_td, min_periods=need).median()
            mean_name = _summary_name(spec, channel, "mean")
            window_mean = out[mean_name] if mean_name in out else \
                pd.Series(g.masked(channel)).rolling(wb, min_periods=wb).mean().to_numpy()[ends]
            out[name] = window_mean - baseline.reindex(starts - pd.Timedelta(minutes=1)).to_numpy()

        elif kind == "rise":
            mean = pd.Series(g.masked(channel)).rolling(wb, min_periods=wb).mean()
            out[name] = (mean - mean.shift(spec.bins(c["lag"]))).to_numpy()[ends]

        elif kind == "coverage":
            n = spec.bins(c["window"])
            out[name] = (g.have(n) / n)[ends]

        elif kind in ("minutes_since_resume", "gap_hours", "value_at_resume", "gap_loss_rate"):
            stop_bins, horizon = spec.bins(c["stop_gap"]), spec.bins(c["horizon"])
            resume_pos, gap_len, k, k_safe, since, recent = g.resumes(stop_bins, horizon)
            if kind == "minutes_since_resume":
                out[name] = (np.minimum(since, horizon) / per_min)[ends]
            elif len(resume_pos) == 0:
                out[name] = (np.zeros(len(ends)) if kind == "gap_hours" else np.full(len(ends), np.nan))
            elif kind == "gap_hours":
                out[name] = np.where(recent, np.minimum(gap_len[k_safe] / per_hour,
                                                        float(c["cap_hours"])), 0.0)[ends]
            elif kind == "value_at_resume":
                out[name] = np.where(recent, g.raw(channel)[resume_pos[k_safe]], np.nan)[ends]
            else:   # gap_loss_rate: the role's value lost per hour across the last gap
                raw = g.raw(channel)
                before_pos = resume_pos - gap_len - 1
                ok = before_pos >= 0
                loss = np.full(len(resume_pos), np.nan)
                loss[ok] = (raw[before_pos[ok]] - raw[resume_pos[ok]]) / (gap_len[ok] / per_hour)
                out[name] = np.where(recent, loss[k_safe], np.nan)[ends]

        elif kind in ("refill_rate", "idle_loss"):
            value = g.masked(channel)
            current = g.masked(spec.roles["motor_current"])
            step_ok = g.valid[1:] & g.valid[:-1]
            d = np.diff(value)
            loaded = float(c["loaded_above"])
            n, need = spec.bins(c["window"]), int(float(c["min_minutes"]) * per_min)
            if kind == "refill_rate":
                steps = step_ok & (current[1:] > loaded) & (current[:-1] > loaded) & \
                    (value[:-1] < float(c["below"]))
                out[name] = trailing_mean_of_steps(d * per_min, steps, n, need)[ends]
            else:
                steps = step_ok & (current[1:] < loaded) & (current[:-1] < loaded)
                out[name] = trailing_mean_of_steps(-d * per_min * 60, steps, n, need)[ends]

        elif kind == "share_trend":
            n = spec.bins(c["window"])
            have = g.have(n)
            state = (g.masked(channel) > float(c["above"])) & g.valid
            share = trailing_sum(state.astype(float), n) / np.maximum(have, 1)
            share = np.where(have >= float(c["min_coverage"]) * n, share, np.nan)
            out[name] = out[c["of"]] - share[ends]

        elif kind == "minutes_active":
            on = (g.raw(channel) > 0.5) & g.valid
            out[name] = trailing_sum(on.astype(float), spec.bins(c["window"]))[ends] / per_min

        elif kind == "command_without_motion":
            motor = spec.roles["motor_current"]
            cmd = ((grid[channel] == 1) & (grid[motor] < float(c["motor_below"])) & g.valid).astype(float)
            out[name] = cmd.rolling(wb, min_periods=1).sum().to_numpy()[ends]

        elif kind == "bridged_points":
            out[name] = grid["bridged"].astype(float).rolling(wb, min_periods=1).sum().to_numpy()[ends]

    return pd.DataFrame({name: out[name] for name in spec.names})


def _summary_name(spec: FeatureSpec, signal: str, stat: str) -> str:
    for f in spec.features:
        c = f["compute"]
        if c["kind"] == "summary" and c["signal"] == signal and c["stat"] == stat:
            return f["name"]
    return ""
