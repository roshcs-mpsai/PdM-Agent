"""OpenTelemetry spans for the agent and the MCP tool server (OPS-03, T26).

The spans are for live debugging in Jaeger; the agent_traces table stays the
audit record (FRD section 8). Everything here goes through the OpenTelemetry
API (``opentelemetry.trace``) and is a no-op when that is not installed, so the
services and the tests run the same code either way.

* ``configure(service)``: with ``opentelemetry-sdk`` and
  ``opentelemetry-exporter-otlp-proto-http`` installed, export spans over
  OTLP/HTTP to ``$OTEL_EXPORTER_OTLP_ENDPOINT`` (default
  http://otel-collector:4318); without them, log once that tracing is off.
* ``incident_trace(incident_id)``: the root span of one agent run. It starts a
  new trace and carries ``incident_id`` (and ``trace_id``, the agent_traces
  key, when given); log lines inside it carry the same fields
  (pdm_common.logs).
* ``span(name, **attributes)`` and ``tool_call(tool, arguments)``: child spans;
  attributes ``incident_id``, ``trace_id``, ``result_ids`` and ``arguments``
  are converted to what OpenTelemetry accepts.
* ``inject(headers)`` / ``extract(headers)``: the W3C ``traceparent`` header,
  so the MCP server's spans join the agent's trace.

A trace id cannot be derived from the incident id with the API alone (the SDK
generates ids), so an incident's trace is found by its tag: in Jaeger, search
service ``agent`` with the tag ``incident_id=<id>`` (ops/README.md).

    tracing.configure("agent")
    with tracing.incident_trace(iid, trace_id=tid, arm="full"):
        with tracing.tool_call("get_telemetry", args, incident_id=iid) as s:
            result = call_tool(...)
            s.set(result_ids=[result["result_id"]])
"""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, MutableMapping, Optional

from pdm_common import logs

log = logging.getLogger("pdm.tracing")

DEFAULT_ENDPOINT = "http://otel-collector:4318"
MAX_ATTRIBUTE_CHARS = 4096                  # longer values are cut; spans are for debugging, not storage
INSTRUMENTATION = "pdm_common.tracing"

_UNSET = object()
_TRACE: Any = _UNSET                         # the opentelemetry.trace module, None when not installed
_tracer: Any = None
_configured: Optional[bool] = None
_lock = threading.Lock()


def _load_trace_api():
    try:
        from opentelemetry import trace as otel_trace
    except ImportError:
        return None
    return otel_trace


def _trace_api():
    global _TRACE
    if _TRACE is _UNSET:
        _TRACE = _load_trace_api()
    return _TRACE


def _get_tracer():
    """The API's tracer, or None without OpenTelemetry. Obtained before ``configure`` it
    still exports once a provider is set (the API's proxy tracer)."""
    global _tracer
    api = _trace_api()
    if api is None:
        return None
    if _tracer is None:
        _tracer = api.get_tracer(INSTRUMENTATION)
    return _tracer


def enabled() -> bool:
    """True once ``configure`` has set up span export."""
    return bool(_configured)


# --- set-up ------------------------------------------------------------------------------------

def _traces_url(endpoint: Optional[str]) -> str:
    """OTLP/HTTP traces URL: $OTEL_EXPORTER_OTLP_TRACES_ENDPOINT as is, else the base endpoint
    (``endpoint``, $OTEL_EXPORTER_OTLP_ENDPOINT or DEFAULT_ENDPOINT) + /v1/traces."""
    if endpoint is None and os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        return os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"]
    base = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or DEFAULT_ENDPOINT
    return base.rstrip("/") + "/v1/traces"


def configure(service: str, *, endpoint: Optional[str] = None) -> bool:
    """Export this process's spans over OTLP/HTTP, named ``service`` ($OTEL_SERVICE_NAME wins).

    Returns True when spans are exported. Without the SDK or the exporter, or with
    OTEL_SDK_DISABLED=true, it logs once that tracing is off and returns False; spans
    then cost next to nothing. Only the first call does anything.
    """
    global _configured
    with _lock:
        if _configured is None:
            _configured = _setup(service, endpoint)
        return _configured


def _setup(service: str, endpoint: Optional[str]) -> bool:
    if os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        log.info("tracing off for %s: OTEL_SDK_DISABLED is set", service)
        return False
    api = _trace_api()
    try:
        if api is None:
            raise ImportError("opentelemetry-api is not installed", name="opentelemetry")
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError as exc:
        log.info("tracing off for %s: %s is not installed (pip install opentelemetry-sdk "
                 "opentelemetry-exporter-otlp-proto-http); spans are not exported", service, exc.name or exc)
        return False
    url = _traces_url(endpoint)
    name = os.environ.get("OTEL_SERVICE_NAME") or service
    provider = TracerProvider(resource=Resource.create({"service.name": name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=url)))
    api.set_tracer_provider(provider)
    log.info("tracing on for %s: spans to %s", name, url)
    return True


# --- attributes --------------------------------------------------------------------------------

def _clip(text: str) -> str:
    return text if len(text) <= MAX_ATTRIBUTE_CHARS else text[:MAX_ATTRIBUTE_CHARS - 14] + "...(truncated)"


def _to_json(value: Any) -> str:
    return _clip(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str))


def attribute_value(value: Any) -> Any:
    """A value OpenTelemetry accepts: str, bool, int, float, or a tuple of one of them.
    None is dropped (returns None); a mapping (tool arguments) becomes canonical JSON; a
    list of strings (result ids) a tuple of strings; a mixed list JSON; anything else str()."""
    if value is None:
        return None
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clip(value)
    if isinstance(value, Mapping):
        return _to_json(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else list(value)
        if not items:
            return ()
        for kind in (str, bool, int, float):
            if all(type(v) is kind for v in items):
                return tuple(_clip(v) if kind is str else v for v in items)
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in items):
            return tuple(float(v) for v in items)
        return _to_json(items)
    return _clip(str(value))


def attributes(fields: Mapping[str, Any]) -> dict:
    """``attribute_value`` applied to every field, None values left out."""
    out = {}
    for key, value in fields.items():
        converted = attribute_value(value)
        if converted is not None:
            out[str(key)] = converted
    return out


# --- spans -------------------------------------------------------------------------------------

class Span:
    """What ``span()`` yields: the OpenTelemetry span (``raw``), or nothing when tracing is
    not installed, behind the same few methods. Attribute values are converted as in
    ``attribute_value``."""
    __slots__ = ("raw",)

    def __init__(self, raw: Any = None):
        self.raw = raw

    def set(self, **fields) -> None:
        if self.raw is not None:
            for key, value in attributes(fields).items():
                self.raw.set_attribute(key, value)

    def set_attribute(self, key: str, value: Any) -> None:
        self.set(**{key: value})

    def add_event(self, name: str, **fields) -> None:
        if self.raw is not None:
            self.raw.add_event(name, attributes=attributes(fields))

    def record_exception(self, exc: BaseException) -> None:
        if self.raw is not None:
            self.raw.record_exception(exc)

    def is_recording(self) -> bool:
        return self.raw is not None and bool(self.raw.is_recording())

    @property
    def trace_id(self) -> Optional[str]:
        """The trace id in hex (what Jaeger shows), or None when there is no valid context."""
        if self.raw is None:
            return None
        ctx = self.raw.get_span_context()
        return format(ctx.trace_id, "032x") if ctx.is_valid else None


_KINDS = ("internal", "server", "client", "producer", "consumer")


@contextmanager
def span(name: str, *, context: Any = None, kind: Optional[str] = None, **fields) -> Iterator[Span]:
    """A span named ``name``, current inside the block and ended on exit. An exception
    leaving the block is recorded on the span and the span marked as an error.

    ``context``: an OpenTelemetry context to start from (``extract(headers)``, or an empty
    context for a new trace); default the current one. ``kind``: internal (default),
    server, client, producer or consumer. ``fields`` become attributes."""
    tracer = _get_tracer()
    if tracer is None:
        yield Span()
        return
    options: dict = {"attributes": attributes(fields)}
    if context is not None:
        options["context"] = context
    if kind is not None:
        if kind not in _KINDS:
            raise ValueError(f"span kind is one of {', '.join(_KINDS)}, not {kind!r}")
        options["kind"] = getattr(_trace_api().SpanKind, kind.upper())
    with tracer.start_as_current_span(name, **options) as raw:
        yield Span(raw)


@contextmanager
def incident_trace(incident_id: str, *, name: str = "incident", trace_id: Optional[str] = None,
                   **fields) -> Iterator[Span]:
    """The root span of one agent run on one incident: always a new trace (never a child of
    whatever span is current), with ``incident_id`` and, when given, ``trace_id`` (the
    agent_traces key) as attributes and as log context. A second run on the same incident
    is a second trace with the same tag."""
    root = None
    if _trace_api() is not None:
        from opentelemetry.context import Context
        root = Context()                      # no parent span: a new trace id
    with logs.context(incident_id=incident_id, trace_id=trace_id):
        with span(name, context=root, incident_id=incident_id, trace_id=trace_id, **fields) as s:
            yield s


@contextmanager
def tool_call(tool: str, arguments: Optional[Mapping[str, Any]] = None, *, kind: str = "client",
              context: Any = None, **fields) -> Iterator[Span]:
    """One tool call: span ``tool <name>`` with ``tool`` and ``arguments`` (canonical JSON).
    The agent side uses kind client; the MCP server kind server with
    ``context=extract(headers)``, so its span joins the agent's trace. Set ``result_ids``
    once the result is stored: ``s.set(result_ids=[...])``."""
    with span(f"tool {tool}", context=context, kind=kind, tool=tool,
              arguments=arguments if arguments is not None else {}, **fields) as s:
        yield s


def current_ids() -> tuple[Optional[str], Optional[str]]:
    """(trace id, span id) in hex of the current span, or (None, None) without a valid one."""
    api = _trace_api()
    if api is None:
        return None, None
    ctx = api.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None, None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


# --- propagation -----------------------------------------------------------------------------

def inject(headers: Optional[MutableMapping[str, str]] = None) -> MutableMapping[str, str]:
    """Add the current trace context (``traceparent``) to ``headers`` for an outgoing request;
    unchanged without OpenTelemetry or a valid current span."""
    headers = {} if headers is None else headers
    if _trace_api() is None:
        return headers
    from opentelemetry import propagate
    propagate.inject(headers)
    return headers


def extract(headers: Mapping[str, str]) -> Any:
    """The context carried by an incoming request's headers, for ``span(..., context=...)``;
    None without OpenTelemetry."""
    if _trace_api() is None:
        return None
    from opentelemetry import propagate
    return propagate.extract(headers)


__all__ = ["DEFAULT_ENDPOINT", "MAX_ATTRIBUTE_CHARS", "Span", "attribute_value", "attributes", "configure",
           "current_ids", "enabled", "extract", "incident_trace", "inject", "span", "tool_call"]
