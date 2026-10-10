"""Asset profile schema, version 0.3 (CFG-02, CFG-08).

``validate_profile`` checks a loaded profile against ``AssetProfile`` and
then against the cross-field rules pydantic cannot see field by field
(detector inputs must be declared signals, topics must carry the asset id,
and so on). Every problem is reported as ``file:line: key.path: message``,
with the line found in the YAML source, so a malformed profile stops a
service with an error that points at the line to fix.

New keys arrive through a schema version bump: every model forbids keys it
does not declare.

0.3 (Oct 2026) adds what the R1-R2 services read from the profile: the
prognosis, incident and device topics, the feature-declaration file next to
the profile (``references.features_file``), structured consistency checks
with their thresholds (the checks registry, PR-10), the prognosis horizons,
the drift job's windows and the southbound tag mapping (ING-14).
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Annotated, Literal, Optional

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from pdm_common.timeutil import parse_duration, timezone

SCHEMA_VERSION = "0.3"


def _duration(text: str) -> str:
    parse_duration(text)          # raises ValueError with a readable message
    return text


def _timezone(name: str) -> str:
    try:
        timezone(name)
    except Exception as exc:      # ZoneInfoNotFoundError is a KeyError
        raise ValueError(f"unknown timezone {name!r}") from exc
    return name


Duration = Annotated[str, AfterValidator(_duration)]
Fraction = Annotated[float, Field(ge=0, le=1)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Asset(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str
    campaign: tuple[dt.date, dt.date]
    records: int = Field(gt=0)


class Sampling(_Strict):
    step_seconds: float = Field(gt=0)
    rate_hz: float = Field(gt=0)
    jitter_seconds: tuple[float, float]
    resample_rule: Duration

    @model_validator(mode="after")
    def _rate_matches_step(self):
        if abs(self.rate_hz * self.step_seconds - 1) > 1e-6:
            raise ValueError("rate_hz must equal 1 / step_seconds")
        return self


def _clock(text: str) -> str:
    try:
        dt.time.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"not a clock time: {text!r} (use 'HH:MM')") from exc
    return text


Clock = Annotated[str, AfterValidator(_clock)]


class OvernightStop(_Strict):
    min_duration: Duration
    starts_after: Clock
    starts_before: Clock
    ends_before: Clock


class GapLabels(_Strict):
    min_gap: Duration
    long_outage: Duration
    overnight_stop: OvernightStop


class GapPolicy(_Strict):
    max_bridge_seconds: float = Field(ge=0)
    drop_window_if_gap_exceeds: Duration
    gap_labels: GapLabels


class FreezeRule(_Strict):
    min_duration: Duration
    max_step_s: float = Field(gt=0)


class Windowing(_Strict):
    length: Duration
    stride: Duration

    @model_validator(mode="after")
    def _stride_fits(self):
        if parse_duration(self.stride) > parse_duration(self.length):
            raise ValueError("stride must not exceed length")
        return self


class Gate(_Strict):
    span: Duration
    min_fraction: float = Field(gt=0, le=1)
    hold_off: Duration


class NE107(_Strict):
    maintenance_p72_upper: Fraction
    failure_p24_lower: Fraction
    no_data_after: Duration


class Topics(_Strict):
    telemetry: str
    payload_schema: str
    windows: str
    scores: str
    status: str
    prognosis: str
    incidents: str
    device: str


class AnalogSignal(_Strict):
    name: str
    unit: str
    range: tuple[float, float]
    desc: str

    @model_validator(mode="after")
    def _range_ordered(self):
        if not self.range[0] < self.range[1]:
            raise ValueError("range must be [low, high] with low < high")
        return self


class DigitalSignal(_Strict):
    name: str
    active_means: str
    duty: Fraction
    polarity: Literal["verified", "plausible", "suspect_inverted"]
    informative: bool = True
    note: Optional[str] = None


class Signals(_Strict):
    analog: list[AnalogSignal] = Field(min_length=1)
    digital: list[DigitalSignal]


class Features(_Strict):
    pipeline: str


class EngineeredFeature(_Strict):
    name: str
    kind: Literal["share_of_time"]
    signal: str
    above: Optional[float] = None
    below: Optional[float] = None
    window: Duration
    desc: str

    @model_validator(mode="after")
    def _one_condition(self):
        if (self.above is None) == (self.below is None):
            raise ValueError("give exactly one of above or below")
        return self


class Detector(_Strict):
    inputs: list[str] = Field(min_length=1)
    engineered: list[EngineeredFeature] = []
    fit_on: str
    score: str


class ConsistencyCheck(_Strict):
    """One entry of the checks registry (PR-10). Thresholds are data: they are
    recomputed from healthy operation by tools/calibrate_checks.py, never
    typed in to fit an event."""
    name: str
    id: Optional[str] = Field(default=None, pattern=r"^C[0-9]+$")
    kind: Optional[Literal["data_quality", "fault_signature", "instrument"]] = None
    rule: str
    threshold: Optional[float] = None
    unit: Optional[str] = None
    params: dict[str, float | int | str] = {}
    source: Optional[str] = None
    meaning: Optional[str] = None
    note: Optional[str] = None
    status: Optional[str] = None


class References(_Strict):
    events_file: str
    features_file: str
    skills_folder: str
    checks_registry: str


class Prognosis(_Strict):
    """PRG-01, PRG-02: the planner's horizons, the cadence in stream time and
    the interval every probability carries."""
    horizons: list[Duration] = Field(min_length=1)
    cadence: Duration
    history: Duration
    interval: Fraction


class Drift(_Strict):
    """OPS-04: compare the last ``window`` with the healthy reference every ``every``."""
    window: Duration
    every: Duration
    signals: list[str] = Field(min_length=1)


class OpcUaSouth(_Strict):
    endpoint: str
    publishing_interval: Duration
    tags: dict[str, str]              # OPC UA node id -> profile signal


class SparkplugSouth(_Strict):
    group: str
    edge_node: str
    device: str
    metrics: dict[str, str]           # Sparkplug metric name -> profile signal


class South(_Strict):
    """ING-14: how southbound sources map onto the profile's signals."""
    opcua: Optional[OpcUaSouth] = None
    sparkplug: Optional[SparkplugSouth] = None


class AssetProfile(_Strict):
    schema_version: Literal["0.3"]
    asset: Asset
    source_timezone: Annotated[str, AfterValidator(_timezone)]
    sampling: Sampling
    gap_policy: GapPolicy
    freeze_rule: FreezeRule
    windowing: Windowing
    gate: Gate
    ne107: NE107
    topics: Topics
    signals: Signals
    features: Features
    detector: Detector
    consistency_checks: list[ConsistencyCheck] = []
    references: References
    prognosis: Optional[Prognosis] = None
    drift: Optional[Drift] = None
    south: Optional[South] = None


def _items(data, *keys) -> list:
    """data[k1][k2]... as a list, or [] when any step is missing or mistyped."""
    for key in keys:
        if not isinstance(data, dict):
            return []
        data = data.get(key)
    return data if isinstance(data, list) else []


def cross_check(data: dict) -> list[tuple[tuple, str]]:
    """Rules that span fields. Returns (key path, message) pairs.

    Runs on the raw mapping, defensively, so these problems are reported in
    the same pass as field-level ones instead of after them are fixed. Values
    of the wrong type are skipped here; the field-level check reports them.
    """
    problems = []
    names = {}
    for kind in ("analog", "digital"):
        for i, s in enumerate(_items(data, "signals", kind)):
            name = s.get("name") if isinstance(s, dict) else None
            if not isinstance(name, str):
                continue
            if name in names:
                problems.append((("signals", kind, i, "name"), f"duplicate signal name {name!r}"))
            names.setdefault(name, kind)
    analog = {n for n, kind in names.items() if kind == "analog"}

    inputs = _items(data, "detector", "inputs")
    for i, name in enumerate(inputs):
        if not isinstance(name, str):
            continue
        if name not in analog:
            problems.append((("detector", "inputs", i), f"{name!r} is not an analog signal of this profile"))
        elif inputs.index(name) != i:
            problems.append((("detector", "inputs", i), f"{name!r} is listed twice"))
    for i, feature in enumerate(_items(data, "detector", "engineered")):
        if not isinstance(feature, dict):
            continue
        signal, name = feature.get("signal"), feature.get("name")
        if isinstance(signal, str) and signal not in analog:
            problems.append((("detector", "engineered", i, "signal"),
                             f"{signal!r} is not an analog signal of this profile"))
        if isinstance(name, str) and name in names:
            problems.append((("detector", "engineered", i, "name"),
                             f"{name!r} clashes with a signal name"))

    asset = data.get("asset")
    asset_id = asset.get("id") if isinstance(asset, dict) else None
    topics = data.get("topics") if isinstance(data.get("topics"), dict) else {}
    for key in ("telemetry", "windows", "scores", "status", "prognosis", "incidents", "device"):
        topic = topics.get(key)
        if isinstance(asset_id, str) and isinstance(topic, str) and asset_id not in topic.split("/"):
            problems.append((("topics", key), f"topic {topic!r} does not name asset {asset_id!r}"))
    for i, check in enumerate(_items(data, "consistency_checks")):
        cid = check.get("id") if isinstance(check, dict) else None
        if isinstance(cid, str) and [c.get("id") for c in _items(data, "consistency_checks")
                                     if isinstance(c, dict)].count(cid) > 1:
            problems.append((("consistency_checks", i, "id"), f"check id {cid!r} is used twice"))

    drift = data.get("drift") if isinstance(data.get("drift"), dict) else {}
    for i, name in enumerate(drift.get("signals") or []):
        if isinstance(name, str) and name not in names:
            problems.append((("drift", "signals", i), f"{name!r} is not a signal of this profile"))

    south = data.get("south") if isinstance(data.get("south"), dict) else {}
    for source, mapping_key in (("opcua", "tags"), ("sparkplug", "metrics")):
        block = south.get(source) if isinstance(south.get(source), dict) else {}
        mapping = block.get(mapping_key) if isinstance(block.get(mapping_key), dict) else {}
        for tag, signal in mapping.items():
            if isinstance(signal, str) and signal not in names:
                problems.append((("south", source, mapping_key, tag),
                                 f"{signal!r} is not a signal of this profile"))
    return problems


# --- line numbers -----------------------------------------------------------

def _line_of(root: Optional[yaml.Node], loc: tuple) -> int:
    """1-based line of the deepest YAML node on ``loc`` that exists.

    Keys report the line the key is written on; a missing key reports its
    parent's line.
    """
    if root is None:
        return 1
    node, line = root, root.start_mark.line + 1
    for part in loc:
        if isinstance(node, yaml.MappingNode):
            # PyYAML keeps the last of duplicate keys, so point at the last one
            match = next(((k, v) for k, v in reversed(node.value) if k.value == str(part)), None)
            if match is None:
                break
            node, line = match[1], match[0].start_mark.line + 1
        elif isinstance(node, yaml.SequenceNode) and isinstance(part, int):
            if part >= len(node.value):
                break
            node = node.value[part]
            line = node.start_mark.line + 1
        else:
            break
    return line


def _path(loc: tuple) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "(top level)"


def duplicate_keys(root: Optional[yaml.Node]) -> list[tuple[int, str]]:
    """(line, message) for every key repeated in a mapping. YAML forbids them
    and PyYAML silently keeps the last, so a pasted block can hide an edit."""
    found, stack = [], [root] if root is not None else []
    while stack:
        node = stack.pop()
        if isinstance(node, yaml.MappingNode):
            seen = {}
            for key, value in node.value:
                if isinstance(key, yaml.ScalarNode):
                    line = key.start_mark.line + 1
                    if key.value in seen:
                        found.append((line, f"duplicate key {key.value!r} (first on line "
                                            f"{seen[key.value]}); YAML keeps only the last"))
                    seen.setdefault(key.value, line)
                stack.append(value)
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(node.value)
    return found


def validate_profile(data: dict, text: str, path: str | Path, error=ValueError) -> AssetProfile:
    """Validate a parsed profile; raise ``error`` listing every problem by line."""
    problems = []
    model = None
    try:
        model = AssetProfile.model_validate(data)
    except ValidationError as exc:
        problems = [(tuple(e["loc"]), e["msg"].removeprefix("Value error, ")) for e in exc.errors()]
    problems += cross_check(data)
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    lines = [(_line_of(root, loc), _path(loc), msg) for loc, msg in problems]
    lines += [(line, "(document)", msg) for line, msg in duplicate_keys(root)]
    if lines:
        lines.sort()
        detail = "\n".join(f"  {path}:{line}: {where}: {msg}" for line, where, msg in lines)
        raise error(f"{path}: asset profile does not match schema {SCHEMA_VERSION} "
                    f"({len(lines)} problem{'s' if len(lines) > 1 else ''}):\n{detail}")
    return model
