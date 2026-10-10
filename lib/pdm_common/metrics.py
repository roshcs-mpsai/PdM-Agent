"""Prometheus metrics for every PdM-Agent service (OPS-02, T24).

A small, thread-safe client, so no service depends on prometheus_client:

* ``Registry`` holding ``Counter``, ``Gauge`` and ``Histogram`` metrics, each
  with optional labels (``.labels(topic=...)``);
* ``Registry.exposition()``: the Prometheus text format 0.0.4 -- HELP and TYPE
  lines, escaped label values, and ``_bucket{le=...}`` (cumulative, ending at
  ``+Inf``), ``_sum`` and ``_count`` for histograms;
* ``serve(registry, port)``: /metrics and /health on a daemon thread
  (http.server), so a service needs no web framework to be scraped.

The catalogue below (``STANDARD`` and ``SERVICE``) is the one list of metric
names. Every service shares the standard set; a service's own metrics come with
it from ``service_metrics(name)``. The Grafana dashboards and the alert rules in
ops/ use only these names (tests/test_metrics.py checks it), so a metric is
added here first, then wired into the service, then put on a panel.

    from pdm_common import metrics
    M = metrics.service_metrics("detector")             # standard set + detector's own
    metrics.serve(metrics.REGISTRY, metrics.port_for("detector"))
    with M.scoring_seconds.labels(asset=asset).time():
        ...
    M.windows_scored_total.labels(asset=asset).inc()

Wall-clock times belong here and in logs, never in payloads (FRD section 9).
"""
from __future__ import annotations

import bisect
import functools
import json
import logging
import math
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Iterator, Optional

log = logging.getLogger("pdm.metrics")

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# 1 ms to 10 s. 2.0 is a bucket edge on purpose: the NFR-01 target is p95 scoring
# latency under 2 s, and with an edge there "under 2 s" is read off a bucket
# count instead of being interpolated.
DEFAULT_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0)
# One agent run: the wall-time budget is 180 s (AGT-08).
AGENT_RUN_BUCKETS = (1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 45.0, 60.0, 90.0, 120.0, 180.0, 240.0, 300.0)

# Metrics port per compose service; ops/prometheus/prometheus.yml scrapes these.
PORTS = {
    "ingest": 9101, "detector": 9102, "gate": 9103, "prognostics": 9104, "mcp_server": 9105, "agent": 9106,
    "api": 9107, "connector": 9108, "opcua_north": 9109, "drift": 9110, "south": 9111,
}

# Set when this module is first imported, which is at service start-up.
PROCESS_START = time.time()

_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


# --- the catalogue --------------------------------------------------------------------------

@dataclass(frozen=True)
class Spec:
    """One catalogue entry: kind (counter, gauge or histogram), help text, label names and,
    for a histogram, its bucket upper bounds (DEFAULT_BUCKETS when None)."""
    kind: str
    help: str
    labels: tuple = ()
    buckets: Optional[tuple] = None


# Every service: what run_service (or the service's own loop) records per message.
STANDARD: dict[str, Spec] = {
    "pdm_up": Spec("gauge", "1 while the service runs and can do its work, 0 while it is up but cannot "
                            "(for example, no broker connection)", ("service",)),
    "pdm_process_start_time_seconds": Spec("gauge", "Unix time the service process started; a change is a restart"),
    "pdm_messages_in_total": Spec("counter", "Messages received, by topic", ("topic",)),
    "pdm_messages_out_total": Spec("counter", "Messages published, by topic", ("topic",)),
    "pdm_handler_errors_total": Spec("counter", "Messages whose handler raised and were skipped, by topic", ("topic",)),
    "pdm_handler_seconds": Spec("histogram", "Wall time to handle one message, by topic", ("topic",)),
    "pdm_queue_depth": Spec("gauge", "Items waiting in a named queue of the service", ("queue",)),
}

_BRIEF_REJECTIONS = Spec("counter", "Briefs the validator rejected, by stage: draft (a repair turn follows), "
                                    "repair (the fallback brief is stored), api (refused on submission)", ("stage",))

# Each service's own metrics. Label values are bounded sets: asset ids, error codes, tool names,
# outcomes. Free text (validator messages, arguments) goes to logs and spans, never to labels.
SERVICE: dict[str, dict[str, Spec]] = {
    "ingest": {
        "pdm_records_in_total": Spec("counter", "Telemetry records received; in = accepted + quarantined",
                                     ("asset",)),
        "pdm_records_accepted_total": Spec("counter", "Records that passed validation", ("asset",)),
        "pdm_records_quarantined_total": Spec("counter", "Records quarantined, by error code", ("asset", "code")),
        "pdm_records_lost_total": Spec("counter", "Records missing from the sequence (jumps in seq)", ("asset",)),
        "pdm_records_out_of_range_total": Spec("counter", "Accepted records with a value outside its profile range",
                                               ("asset",)),
        "pdm_windows_published_total": Spec("counter", "Complete windows published", ("asset",)),
        "pdm_windows_dropped_total": Spec("counter", "Incomplete windows: counted, not published", ("asset",)),
    },
    "detector": {
        "pdm_windows_scored_total": Spec("counter", "Windows scored", ("asset",)),
        "pdm_windows_refused_total": Spec("counter", "Windows refused without a score, by reason",
                                          ("asset", "reason")),
        "pdm_alerts_total": Spec("counter", "Scored windows over the threshold", ("asset",)),
        "pdm_scoring_seconds": Spec("histogram", "Wall time from receiving a window to its score being ready to "
                                                 "publish (stored included)", ("asset",)),
    },
    "gate": {
        "pdm_gate_decisions_total": Spec("counter", "Gate decisions (open, attach, suppress, close)",
                                         ("asset", "decision")),
        "pdm_incidents_opened_total": Spec("counter", "Incidents opened by the gate", ("asset",)),
        "pdm_ne107_status": Spec("gauge", "1 for the asset's current NE 107 status, 0 for the other four",
                                 ("asset", "status")),
        "pdm_ne107_status_changes_total": Spec("counter", "NE 107 status changes, by the status changed to",
                                               ("asset", "to")),
    },
    "prognostics": {
        "pdm_prognoses_total": Spec("counter", "Prognoses published, one per stream hour", ("asset",)),
    },
    "drift": {
        "pdm_drift_checks_total": Spec("counter", "Drift checks run", ("asset",)),
        "pdm_drift_alerts_total": Spec("counter", "Drift checks that ended with an alert active (an alert holds "
                                                  "until a check clears it)", ("asset",)),
    },
    "mcp_server": {
        "pdm_tool_calls_total": Spec("counter", "Tool calls, by tool and outcome (ok, rejected, error)",
                                     ("tool", "outcome")),
        "pdm_tool_call_seconds": Spec("histogram", "Wall time of one tool call, by tool", ("tool",)),
    },
    "agent": {
        "pdm_agent_runs_total": Spec("counter", "Agent runs finished, by arm and outcome (sufficient, insufficient, "
                                                "budget_exhausted, fallback, model_unavailable, claimed_elsewhere, "
                                                "error)", ("arm", "outcome")),
        "pdm_agent_run_seconds": Spec("histogram", "Wall time of one agent run, by arm", ("arm",), AGENT_RUN_BUCKETS),
        "pdm_agent_runs_in_progress": Spec("gauge", "Agent runs in progress"),
        "pdm_brief_rejections_total": _BRIEF_REJECTIONS,
    },
    "api": {
        "pdm_http_requests_total": Spec("counter", "HTTP requests, by route template, method and status code",
                                        ("route", "method", "code")),
        "pdm_http_request_seconds": Spec("histogram", "HTTP request time, by route template and method",
                                         ("route", "method")),
        "pdm_decisions_total": Spec("counter", "Reviewer decisions, by action (approve, edit, reject)", ("action",)),
        "pdm_brief_rejections_total": _BRIEF_REJECTIONS,
    },
    "connector": {
        "pdm_work_order_posts_total": Spec("counter", "Work-order POST attempts, by outcome (sent, duplicate, "
                                                      "retry, failed)", ("outcome",)),
        "pdm_work_orders_blocked_total": Spec("counter", "Work orders the connector refused to send, by reason "
                                                         "(no_approval, tampered, conflict, rejected_by_endpoint)",
                                              ("reason",)),
        "pdm_work_orders_pending": Spec("gauge", "Work orders queued or waiting to retry"),
    },
    "opcua_north": {},
    "south": {},
}


def _catalogue() -> dict[str, Spec]:
    merged = dict(STANDARD)
    for service, specs in SERVICE.items():
        for name, spec in specs.items():
            if merged.get(name, spec) != spec:
                raise ValueError(f"{name} is declared twice with different definitions ({service})")
            merged[name] = spec
    return merged


CATALOGUE: dict[str, Spec] = _catalogue()


# --- formatting -----------------------------------------------------------------------------

def _fmt(value: float) -> str:
    """A sample value or bucket bound as Prometheus writes it (Go float formatting):
    +Inf, -Inf, NaN, '1.0', '0.005', '1.2345678e+07'."""
    v = float(value)
    if v == math.inf:
        return "+Inf"
    if v == -math.inf:
        return "-Inf"
    if math.isnan(v):
        return "NaN"
    s = repr(v)
    dot = s.find(".")
    if v > 0 and dot > 6:                      # Go switches to an exponent sooner than Python
        mantissa = f"{s[0]}.{s[1:dot]}{s[dot + 1:]}".rstrip("0.")
        return f"{mantissa}e+0{dot - 1}"
    return s


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _labels_text(pairs) -> str:
    if not pairs:
        return ""
    return "{" + ",".join(f'{k}="{_escape_label(v)}"' for k, v in pairs) + "}"


# --- children: one per label set --------------------------------------------------------------

class _ValueChild:
    __slots__ = ("_lock", "_value", "_fn", "_warned")

    def __init__(self):
        self._lock = threading.Lock()
        self._value = 0.0
        self._fn: Optional[Callable[[], float]] = None
        self._warned = False

    @property
    def value(self) -> float:
        fn = self._fn
        if fn is not None:
            return float(fn())
        with self._lock:
            return self._value

    def _exported(self, name: str) -> float:
        """The value for the exposition; a failing gauge function gives NaN, logged once."""
        try:
            return self.value
        except Exception:
            if not self._warned:
                self._warned = True
                log.warning("gauge function of %s failed; exporting NaN", name, exc_info=True)
            return math.nan


class CounterChild(_ValueChild):
    __slots__ = ()

    def inc(self, amount: float = 1.0) -> None:
        amount = float(amount)
        if amount < 0:
            raise ValueError("a counter only goes up; use a Gauge for values that go down")
        with self._lock:
            self._value += amount


class GaugeChild(_ValueChild):
    __slots__ = ()

    def set(self, value: float) -> None:
        with self._lock:
            self._fn = None
            self._value = float(value)

    def inc(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value += float(amount)

    def dec(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value -= float(amount)

    def set_function(self, fn: Callable[[], float]) -> None:
        """Read the value from ``fn`` at every scrape (queue lengths, buffer sizes)."""
        self._fn = fn

    def track_inprogress(self) -> "_InProgress":
        """Context manager or decorator: +1 on entry, -1 on exit."""
        return _InProgress(self)


class _InProgress:
    def __init__(self, child: GaugeChild):
        self._child = child

    def __enter__(self):
        self._child.inc()
        return self

    def __exit__(self, *exc) -> bool:
        self._child.dec()
        return False

    def __call__(self, fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with _InProgress(self._child):
                return fn(*args, **kwargs)
        return wrapper


class _Timer:
    """``with h.time():`` or ``@h.time()``: observes the elapsed wall time, also when
    the block raises. ``elapsed`` holds the seconds once the block is done."""

    def __init__(self, observe: Callable[[float], None]):
        self._observe = observe
        self._t0 = 0.0
        self.elapsed: Optional[float] = None

    def __enter__(self) -> "_Timer":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc) -> bool:
        self.elapsed = time.perf_counter() - self._t0
        self._observe(self.elapsed)
        return False

    def __call__(self, fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with _Timer(self._observe):          # a fresh timer per call: safe across threads
                return fn(*args, **kwargs)
        return wrapper


class HistogramChild:
    __slots__ = ("_lock", "_upper", "_counts", "_sum", "_count")

    def __init__(self, upper: tuple):
        self._lock = threading.Lock()
        self._upper = upper                      # finite bounds, then +Inf
        self._counts = [0] * len(upper)          # per bucket, not cumulative
        self._sum = 0.0
        self._count = 0

    def observe(self, value: float) -> None:
        v = float(value)
        if math.isnan(v):
            raise ValueError("cannot observe NaN")
        i = bisect.bisect_left(self._upper, v)   # first bucket whose bound is >= v (le is inclusive)
        with self._lock:
            self._counts[i] += 1
            self._sum += v
            self._count += 1

    def time(self) -> _Timer:
        return _Timer(self.observe)

    def snapshot(self) -> tuple[list[tuple[float, int]], float, int]:
        """([(upper bound, cumulative count)], sum, count), read together."""
        with self._lock:
            counts, total, n = list(self._counts), self._sum, self._count
        cumulative, running = [], 0
        for bound, c in zip(self._upper, counts):
            running += c
            cumulative.append((bound, running))
        return cumulative, total, n

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    @property
    def sum(self) -> float:
        with self._lock:
            return self._sum


# --- metrics ----------------------------------------------------------------------------------

_DEFAULT = object()          # "register in REGISTRY"


class _Metric:
    kind = ""

    def __init__(self, name: str, help: str, labelnames=(), *, registry=_DEFAULT):
        if not _NAME.match(name):
            raise ValueError(f"not a valid metric name: {name!r}")
        labelnames = tuple(labelnames)
        for label in labelnames:
            if not _LABEL.match(label) or label.startswith("__"):
                raise ValueError(f"not a valid label name for {name}: {label!r}")
        if len(set(labelnames)) != len(labelnames):
            raise ValueError(f"{name}: a label name appears twice in {labelnames}")
        self.name = name
        self.help = help
        self.labelnames = labelnames
        self._lock = threading.Lock()
        self._children: dict[tuple, object] = {}
        if not labelnames:
            self._children[()] = self._new_child()
        if registry is _DEFAULT:
            registry = REGISTRY
        if registry is not None:
            registry.register(self)

    def _new_child(self):
        raise NotImplementedError

    def labels(self, *values, **labels):
        """The child for one label set, created on first use (so it exports 0 from then on).
        Values by position (in ``labelnames`` order) or by name; they become strings."""
        if not self.labelnames:
            raise ValueError(f"{self.name} has no labels")
        if values and labels:
            raise ValueError(f"{self.name}: give label values by position or by name, not both")
        if labels:
            if set(labels) != set(self.labelnames):
                raise ValueError(f"{self.name} takes labels ({', '.join(self.labelnames)}), "
                                 f"got ({', '.join(sorted(labels))})")
            key = tuple(str(labels[n]) for n in self.labelnames)
        else:
            if len(values) != len(self.labelnames):
                raise ValueError(f"{self.name} takes {len(self.labelnames)} label value(s) "
                                 f"({', '.join(self.labelnames)}), got {len(values)}")
            key = tuple(str(v) for v in values)
        child = self._children.get(key)
        if child is None:
            with self._lock:
                child = self._children.get(key)
                if child is None:
                    child = self._children[key] = self._new_child()
        return child

    def _only(self):
        if self.labelnames:
            raise ValueError(f"{self.name} has labels ({', '.join(self.labelnames)}): call .labels(...) first")
        return self._children[()]

    def _items(self) -> list[tuple[tuple, object]]:
        with self._lock:
            items = list(self._children.items())
        return sorted(items, key=lambda kv: kv[0])

    def child(self, **labels):
        """The existing child for these labels, or None (does not create one)."""
        if not self.labelnames:
            return self._children[()] if not labels else None
        if set(labels) != set(self.labelnames):
            return None
        return self._children.get(tuple(str(labels[n]) for n in self.labelnames))

    def samples(self) -> list[tuple[str, tuple, float]]:
        """(sample name, ((label, value), ...), value) for every series of this metric."""
        out = []
        for key, child in self._items():
            out.append((self.name, tuple(zip(self.labelnames, key)), child._exported(self.name)))
        return out


class Counter(_Metric):
    """Only goes up. The name must end in ``_total`` (Prometheus convention)."""
    kind = "counter"

    def __init__(self, name: str, help: str, labelnames=(), *, registry=_DEFAULT):
        if not name.endswith("_total"):
            raise ValueError(f"counter names end in _total: {name!r}")
        super().__init__(name, help, labelnames, registry=registry)

    def _new_child(self) -> CounterChild:
        return CounterChild()

    def inc(self, amount: float = 1.0) -> None:
        self._only().inc(amount)

    @property
    def value(self) -> float:
        return self._only().value


class Gauge(_Metric):
    kind = "gauge"

    def _new_child(self) -> GaugeChild:
        return GaugeChild()

    def set(self, value: float) -> None:
        self._only().set(value)

    def inc(self, amount: float = 1.0) -> None:
        self._only().inc(amount)

    def dec(self, amount: float = 1.0) -> None:
        self._only().dec(amount)

    def set_function(self, fn: Callable[[], float]) -> None:
        self._only().set_function(fn)

    def track_inprogress(self) -> _InProgress:
        return self._only().track_inprogress()

    @property
    def value(self) -> float:
        return self._only().value


def _bucket_bounds(name: str, buckets) -> tuple:
    """Sorted, distinct upper bounds ending in +Inf."""
    bounds = sorted(float(b) for b in buckets)
    if not bounds:
        raise ValueError(f"{name}: a histogram needs at least one bucket")
    if any(math.isnan(b) for b in bounds) or len(set(bounds)) != len(bounds):
        raise ValueError(f"{name}: buckets must be distinct numbers, got {tuple(buckets)}")
    if bounds[-1] != math.inf:
        bounds.append(math.inf)
    return tuple(bounds)


class Histogram(_Metric):
    """Buckets are upper bounds (``le``, inclusive); +Inf is added if missing."""
    kind = "histogram"

    def __init__(self, name: str, help: str, labelnames=(), *, buckets=DEFAULT_BUCKETS, registry=_DEFAULT):
        self.buckets = _bucket_bounds(name, buckets)
        if "le" in tuple(labelnames):
            raise ValueError(f"{name}: 'le' is reserved for histogram buckets")
        super().__init__(name, help, labelnames, registry=registry)

    def _new_child(self) -> HistogramChild:
        return HistogramChild(self.buckets)

    def observe(self, value: float) -> None:
        self._only().observe(value)

    def time(self) -> _Timer:
        return self._only().time()

    def samples(self) -> list[tuple[str, tuple, float]]:
        out = []
        for key, child in self._items():
            pairs = tuple(zip(self.labelnames, key))
            cumulative, total, n = child.snapshot()
            for bound, c in cumulative:
                out.append((self.name + "_bucket", pairs + (("le", _fmt(bound)),), float(c)))
            out.append((self.name + "_sum", pairs, total))
            out.append((self.name + "_count", pairs, float(n)))
        return out


_KINDS = {"counter": Counter, "gauge": Gauge, "histogram": Histogram}


# --- the registry -----------------------------------------------------------------------------

class Registry:
    """The metrics one process exports. ``service`` names it in /health."""

    def __init__(self, service: Optional[str] = None):
        self.service = service
        self._lock = threading.Lock()
        self._metrics: dict[str, _Metric] = {}

    def register(self, metric: _Metric) -> _Metric:
        with self._lock:
            if metric.name in self._metrics:
                raise ValueError(f"metric {metric.name} is already registered")
            self._metrics[metric.name] = metric
        return metric

    def unregister(self, name: str) -> None:
        with self._lock:
            self._metrics.pop(name, None)

    def get(self, name: str) -> Optional[_Metric]:
        with self._lock:
            return self._metrics.get(name)

    def names(self) -> list[str]:
        with self._lock:
            return list(self._metrics)

    def _get_or_create(self, cls, name: str, help: str, labelnames, **kwargs) -> _Metric:
        labelnames = tuple(labelnames)
        with self._lock:
            existing = self._metrics.get(name)
            if existing is None:
                metric = cls(name, help, labelnames, registry=None, **kwargs)
                self._metrics[name] = metric
                return metric
        same = type(existing) is cls and existing.labelnames == labelnames
        if same and cls is Histogram:
            same = existing.buckets == _bucket_bounds(name, kwargs["buckets"])
        if not same:
            raise ValueError(f"{name} is already registered as a {existing.kind} with labels "
                             f"({', '.join(existing.labelnames)})")
        return existing

    def counter(self, name: str, help: str, labelnames=()) -> Counter:
        """The counter ``name``, created on first call; later calls must agree on its labels."""
        return self._get_or_create(Counter, name, help, labelnames)

    def gauge(self, name: str, help: str, labelnames=()) -> Gauge:
        return self._get_or_create(Gauge, name, help, labelnames)

    def histogram(self, name: str, help: str, labelnames=(), buckets=DEFAULT_BUCKETS) -> Histogram:
        return self._get_or_create(Histogram, name, help, labelnames, buckets=buckets)

    def exposition(self) -> str:
        """Every metric in the text format 0.0.4, in registration order."""
        with self._lock:
            metrics = list(self._metrics.values())
        lines = []
        for m in metrics:
            lines.append(f"# HELP {m.name} {_escape_help(m.help)}")
            lines.append(f"# TYPE {m.name} {m.kind}")
            for sample, pairs, value in m.samples():
                lines.append(f"{sample}{_labels_text(pairs)} {_fmt(value)}")
        return "\n".join(lines) + "\n" if lines else ""

    def value(self, name: str, **labels) -> Optional[float]:
        """One sample's current value, or None if that series does not exist (yet).
        ``name`` is a counter or gauge, or a histogram's ``_count`` or ``_sum``; a
        histogram's own name gives its count."""
        metric, part = self.get(name), None
        if metric is None:
            for suffix in ("_count", "_sum"):
                base = self.get(name[: -len(suffix)]) if name.endswith(suffix) else None
                if isinstance(base, Histogram):
                    metric, part = base, suffix
                    break
        if metric is None:
            return None
        child = metric.child(**labels)
        if child is None:
            return None
        if isinstance(child, HistogramChild):
            return child.sum if part == "_sum" else float(child.count)
        return child.value


REGISTRY = Registry()


# --- the catalogue, bound to a service -------------------------------------------------------------

def metric(name: str, registry: Optional[Registry] = None) -> _Metric:
    """The catalogue metric ``name`` in ``registry`` (default REGISTRY), created on first use."""
    spec = CATALOGUE.get(name)
    if spec is None:
        raise KeyError(f"{name} is not in the metrics catalogue (pdm_common.metrics.STANDARD / SERVICE)")
    registry = REGISTRY if registry is None else registry
    if spec.kind == "histogram":
        return registry.histogram(name, spec.help, spec.labels, buckets=spec.buckets or DEFAULT_BUCKETS)
    if spec.kind == "counter":
        return registry.counter(name, spec.help, spec.labels)
    return registry.gauge(name, spec.help, spec.labels)


class ServiceMetrics:
    """The metrics one service exports. Each is an attribute named as the metric without
    ``pdm_``: ``M.messages_in_total``, ``M.handler_seconds``, ``M.windows_scored_total``."""

    def __init__(self, service: str, registry: Registry, found: dict[str, _Metric]):
        self.service = service
        self.registry = registry
        self._found = found
        for name, m in found.items():
            setattr(self, name[len("pdm_"):], m)

    def __getitem__(self, name: str) -> _Metric:
        return self._found[name]

    def __contains__(self, name: str) -> bool:
        return name in self._found

    @property
    def names(self) -> list[str]:
        return list(self._found)

    def set_up(self, ok: bool) -> None:
        """pdm_up for this service: 1 when it can work, 0 when it cannot (e.g. broker lost)."""
        self._found["pdm_up"].labels(service=self.service).set(1 if ok else 0)

    @contextmanager
    def handling(self, topic: str) -> Iterator[None]:
        """Around one handler call: counts the message in, times it, counts an exception
        (and re-raises it, so the caller's own handling still runs)."""
        self._found["pdm_messages_in_total"].labels(topic=topic).inc()
        timer = self._found["pdm_handler_seconds"].labels(topic=topic)
        t0 = time.perf_counter()
        try:
            yield
        except Exception:
            self._found["pdm_handler_errors_total"].labels(topic=topic).inc()
            raise
        finally:
            timer.observe(time.perf_counter() - t0)


def service_metrics(service: str, registry: Optional[Registry] = None) -> ServiceMetrics:
    """The standard set every service shares, plus the service's own metrics from the
    catalogue (none for a name not in SERVICE). Calling it again returns the same metrics.
    Sets ``pdm_up{service}`` to 1 and the process start time."""
    registry = REGISTRY if registry is None else registry
    if registry.service is None:
        registry.service = service
    names = list(STANDARD) + [n for n in SERVICE.get(service, {}) if n not in STANDARD]
    found = {name: metric(name, registry) for name in names}
    found["pdm_up"].labels(service=service).set(1)
    found["pdm_process_start_time_seconds"].set(PROCESS_START)
    return ServiceMetrics(service, registry, found)


def port_for(service: str) -> Optional[int]:
    """The metrics port: $PDM_METRICS_PORT if set ('off' disables), else PORTS[service]."""
    text = os.environ.get("PDM_METRICS_PORT", "").strip()
    if text.lower() in ("off", "none", "no", "false"):
        return None
    if text:
        return int(text)
    return PORTS.get(service)


# --- the HTTP endpoint ------------------------------------------------------------------------

class MetricsServer(ThreadingHTTPServer):
    """/metrics and /health; ``port`` is the bound port (useful with port 0)."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, registry: Registry, service: Optional[str],
                 health: Optional[Callable[[], bool]]):
        super().__init__(address, _Handler)
        self.registry = registry
        self.service = service
        self.health = health
        self.thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self.server_address[1]

    def health_doc(self) -> tuple[int, dict]:
        doc = {"status": "ok", "service": self.service, "uptime_s": round(time.time() - PROCESS_START, 3)}
        if self.health is not None:
            try:
                ok = bool(self.health())
            except Exception as exc:
                ok = False
                doc["error"] = f"{type(exc).__name__}: {exc}"
            if not ok:
                doc["status"] = "unavailable"
                return 503, doc
        return 200, doc

    def close(self) -> None:
        self.shutdown()
        self.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)


class _Handler(BaseHTTPRequestHandler):
    server: MetricsServer

    def do_GET(self) -> None:  # noqa: N802 (http.server's naming)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/metrics":
            self._send(200, CONTENT_TYPE, self.server.registry.exposition().encode("utf-8"))
        elif path == "/health":
            code, doc = self.server.health_doc()
            self._send(code, "application/json", json.dumps(doc).encode("utf-8"))
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found; try /metrics or /health\n")

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:  # noqa: A002
        pass                                       # a scrape every 5 s would flood the service log


def serve(registry: Optional[Registry], port: Optional[int], host: str = "0.0.0.0", *,
          service: Optional[str] = None, health: Optional[Callable[[], bool]] = None) -> Optional[MetricsServer]:
    """Serve /metrics and /health on a daemon thread; returns the server (``.port``,
    ``.close()``), or None when ``port`` is None (metrics switched off).

    /health answers 200 ``{"status": "ok", "service": ..., "uptime_s": ...}``, or 503 with
    status ``unavailable`` when ``health`` returns False or raises. Port 0 picks a free port.
    """
    if port is None:
        return None
    registry = REGISTRY if registry is None else registry
    server = MetricsServer((host, int(port)), registry, service or registry.service, health)
    server.thread = threading.Thread(target=server.serve_forever, name=f"metrics:{server.port}", daemon=True)
    server.thread.start()
    log.info("metrics on http://%s:%d/metrics", host, server.port)
    return server


__all__ = [
    "AGENT_RUN_BUCKETS", "CATALOGUE", "CONTENT_TYPE", "DEFAULT_BUCKETS", "PORTS", "PROCESS_START", "REGISTRY",
    "SERVICE", "STANDARD", "Counter", "Gauge", "Histogram", "MetricsServer", "Registry", "ServiceMetrics", "Spec",
    "metric", "port_for", "serve", "service_metrics",
]
