"""Structured logs and spans (OPS-01, OPS-03; T24, T26): pdm_common.logs and pdm_common.tracing.

Logs are read back as JSON, one object per line. Tracing runs here with the
OpenTelemetry API only (no SDK, no exporter), which is the no-op path; what a
recording span receives is checked with a stand-in tracer, and the SDK set-up
with stand-in SDK modules. Jaeger and the collector are not run here.
"""
import asyncio
import io
import json
import logging
import re
import sys
import threading
import types
from contextlib import contextmanager

import pytest

from pdm_common import logs, tracing

TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _strict(text):
    """json.loads that refuses NaN and Infinity, as strict log pipelines do."""
    def bad(constant):
        raise ValueError(f"not JSON: {constant}")
    return json.loads(text, parse_constant=bad)


@pytest.fixture()
def json_log():
    """logs.setup into a buffer; the root logger is put back afterwards. Yields a function
    returning the lines written so far, parsed."""
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    buf = io.StringIO()
    logs.setup("svc", "DEBUG", stream=buf)
    yield lambda: [_strict(line) for line in buf.getvalue().splitlines()]
    for h in list(root.handlers):
        if h not in saved:
            root.removeHandler(h)
    for h in saved:
        if h not in root.handlers:
            root.addHandler(h)
    root.setLevel(level)


# --- logs -------------------------------------------------------------------------------------

def test_one_json_object_per_line_with_the_base_fields(json_log):
    log = logging.getLogger("ingest")
    log.info("windows to %s | in=%d", "2020-04-17T00:10:00Z", 61)
    log.debug("second line")
    first, second = json_log()
    assert list(first)[:5] == ["ts", "level", "service", "logger", "message"]
    assert TS.match(first["ts"])
    assert first["level"] == "INFO" and first["service"] == "svc" and first["logger"] == "ingest"
    assert first["message"] == "windows to 2020-04-17T00:10:00Z | in=61"
    assert second["level"] == "DEBUG"


def test_correlation_fields_follow_the_message_in_a_fixed_order(json_log):
    logging.getLogger("agent").info("tool result stored", extra={
        "window_end": "2020-07-15T00:30:00Z", "asset": "a1", "result_id": "R-0012-3", "trace_id": "T-9",
        "incident_id": "INC-a1-20200715T0030Z"})
    doc = json_log()[0]
    assert list(doc) == ["ts", "level", "service", "logger", "message", "incident_id", "trace_id", "result_id",
                         "asset", "window_end"]


def test_context_adds_fields_until_the_block_ends(json_log):
    log = logging.getLogger("gate")
    with logs.context(incident_id="INC-1", trace_id=None):
        log.info("opened")
        with logs.context(trace_id="T-1", asset="a1") as fields:
            assert fields == {"incident_id": "INC-1", "trace_id": "T-1", "asset": "a1"}
            log.info("agent called")
            log.info("explicit wins", extra={"incident_id": "INC-2"})
        log.info("back to the outer block")
    log.info("outside")
    opened, called, explicit, outer, outside = json_log()
    assert opened["incident_id"] == "INC-1" and "trace_id" not in opened      # None is ignored
    assert (called["incident_id"], called["trace_id"], called["asset"]) == ("INC-1", "T-1", "a1")
    assert explicit["incident_id"] == "INC-2"
    assert "trace_id" not in outer and outer["incident_id"] == "INC-1"
    assert "incident_id" not in outside
    assert logs.current() == {}


def test_context_stays_with_its_thread_and_follows_asyncio_tasks(json_log):
    log = logging.getLogger("agent")

    async def task():
        log.info("in a task")

    async def main():
        with logs.context(incident_id="INC-async"):
            await asyncio.create_task(task())

    with logs.context(incident_id="INC-main"):
        t = threading.Thread(target=lambda: log.info("other thread"))
        t.start()
        t.join()
    asyncio.run(main())
    other, in_task = [line for line in json_log() if line["logger"] == "agent"]   # not asyncio's own
    assert "incident_id" not in other
    assert in_task["incident_id"] == "INC-async"


def test_exception_text_is_kept(json_log):
    log = logging.getLogger("detector")
    try:
        {}["features"]
    except KeyError:
        log.exception("handler failed on a message from %s", "pdm/a1/windows", extra={"incident_id": "INC-3"})
    doc = json_log()[0]
    assert doc["level"] == "ERROR" and doc["incident_id"] == "INC-3"
    assert doc["exception"].startswith("Traceback (most recent call last):")
    assert "KeyError: 'features'" in doc["exception"]


def test_values_json_cannot_hold_are_written_safely(json_log):
    import datetime as dt

    class Odd:
        def __str__(self):
            return "odd"

    logging.getLogger("x").warning("values", extra={
        "score": float("nan"), "upper": float("inf"), "at": dt.datetime(2020, 7, 15, tzinfo=dt.timezone.utc),
        "tags": {"b"}, "pair": (1, 2), "thing": Odd(), "nested": {"v": float("-inf")}})
    doc = json_log()[0]
    assert doc["score"] == "NaN" and doc["upper"] == "Infinity" and doc["nested"] == {"v": "-Infinity"}
    assert doc["at"] == "2020-07-15T00:00:00+00:00"
    assert doc["tags"] == ["b"] and doc["pair"] == [1, 2] and doc["thing"] == "odd"


def test_base_fields_are_never_overwritten(json_log):
    with logs.context(level="context"):
        logging.getLogger("x").info("m", extra={"service": "other", "ts": "then"})
    doc = json_log()[0]
    assert doc["service"] == "svc" and TS.match(doc["ts"]) and doc["level"] == "INFO"
    assert (doc["extra_service"], doc["extra_ts"], doc["extra_level"]) == ("other", "then", "context")


def test_setup_replaces_a_basic_config_handler_and_keeps_others(json_log, caplog):
    root = logging.getLogger()
    stray = logging.StreamHandler(sys.stderr)          # what logging.basicConfig() would leave
    root.addHandler(stray)
    buf = io.StringIO()
    logs.setup("svc", "INFO", stream=buf)
    logs.setup("svc", "INFO", stream=buf)              # twice: still one handler
    assert stray not in root.handlers
    assert sum(1 for h in root.handlers if getattr(h, "_pdm_json", False)) == 1
    with caplog.at_level(logging.INFO):
        logging.getLogger("ingest").info("still captured by pytest")
    assert "still captured by pytest" in caplog.text
    assert [_strict(line)["message"] for line in buf.getvalue().splitlines()] == ["still captured by pytest"]


def test_level_by_name_number_or_environment(json_log, monkeypatch):
    buf = io.StringIO()
    logs.setup("svc", "warning", stream=buf)
    logging.getLogger("x").info("dropped")
    logging.getLogger("x").warning("kept")
    assert [_strict(line)["message"] for line in buf.getvalue().splitlines()] == ["kept"]
    monkeypatch.setenv("PDM_LOG_LEVEL", "ERROR")
    logs.setup("svc", stream=buf)
    assert logging.getLogger().level == logging.ERROR
    logs.setup("svc", logging.DEBUG, stream=buf)
    assert logging.getLogger().level == logging.DEBUG
    with pytest.raises(ValueError, match="not a log level"):
        logs.setup("svc", "chatty", stream=buf)


def test_lines_carry_the_opentelemetry_ids_of_a_valid_current_span(json_log):
    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

    log = logging.getLogger("mcp_server")
    log.info("no span")
    ctx = SpanContext(trace_id=0xABC, span_id=0xDEF, is_remote=True, trace_flags=TraceFlags(1))
    with trace.use_span(NonRecordingSpan(ctx)):
        log.info("in a span")
    plain, in_span = json_log()
    assert "otel_trace_id" not in plain
    assert in_span["otel_trace_id"] == f"{0xABC:032x}" and in_span["otel_span_id"] == f"{0xDEF:016x}"


# --- tracing: set-up ------------------------------------------------------------------------------

@pytest.fixture()
def fresh_tracing(monkeypatch):
    """tracing as if not yet configured, without a cached tracer, and with OpenTelemetry's global
    tracer provider back to its default (the API alone): whether the SDK is installed, or an
    earlier test configured it, changes nothing here, and nothing set here outlives the test."""
    monkeypatch.setattr(tracing, "_configured", None)
    monkeypatch.setattr(tracing, "_tracer", None)
    for var in ("OTEL_SDK_DISABLED", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                "OTEL_SERVICE_NAME"):
        monkeypatch.delenv(var, raising=False)
    try:
        from opentelemetry import trace as otel_trace
        from opentelemetry.util._once import Once
    except ImportError:
        return monkeypatch
    monkeypatch.setattr(otel_trace, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(otel_trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(otel_trace, "_PROXY_TRACER_PROVIDER", otel_trace.ProxyTracerProvider())
    return monkeypatch


def _without_the_sdk(monkeypatch):
    """As if opentelemetry-sdk and the OTLP exporter were not installed (CI installs them)."""
    for name in ("opentelemetry.sdk", "opentelemetry.sdk.resources", "opentelemetry.sdk.trace",
                 "opentelemetry.sdk.trace.export", "opentelemetry.exporter.otlp.proto.http.trace_exporter"):
        monkeypatch.setitem(sys.modules, name, None)


def test_configure_without_the_sdk_logs_once_that_tracing_is_off(fresh_tracing, caplog):
    _without_the_sdk(fresh_tracing)
    with caplog.at_level(logging.INFO, logger="pdm.tracing"):
        assert tracing.configure("agent") is False
        assert tracing.configure("agent") is False
    off = [r for r in caplog.records if r.name == "pdm.tracing"]
    assert len(off) == 1 and "tracing off for agent" in off[0].getMessage()
    assert "opentelemetry-sdk" in off[0].getMessage()
    assert tracing.enabled() is False


def test_configure_respects_otel_sdk_disabled(fresh_tracing, caplog):
    fresh_tracing.setenv("OTEL_SDK_DISABLED", "true")
    with caplog.at_level(logging.INFO, logger="pdm.tracing"):
        assert tracing.configure("mcp_server") is False
    assert "OTEL_SDK_DISABLED" in caplog.text


class _FakeApi:
    """Stands in for opentelemetry.trace so no global provider is set in this process."""
    def __init__(self):
        self.provider = None

    def set_tracer_provider(self, provider):
        self.provider = provider


def _fake_sdk(monkeypatch):
    """Minimal stand-ins for opentelemetry-sdk and the OTLP/HTTP exporter."""
    made = {}

    class Resource:
        @staticmethod
        def create(attrs):
            return dict(attrs)

    class TracerProvider:
        def __init__(self, resource):
            self.resource, self.processors = resource, []

        def add_span_processor(self, processor):
            self.processors.append(processor)

    class BatchSpanProcessor:
        def __init__(self, exporter):
            self.exporter = exporter

    class OTLPSpanExporter:
        def __init__(self, endpoint):
            self.endpoint = endpoint
            made["endpoint"] = endpoint

    modules = {
        "opentelemetry.sdk": types.ModuleType("opentelemetry.sdk"),
        "opentelemetry.sdk.resources": types.SimpleNamespace(Resource=Resource),
        "opentelemetry.sdk.trace": types.SimpleNamespace(TracerProvider=TracerProvider),
        "opentelemetry.sdk.trace.export": types.SimpleNamespace(BatchSpanProcessor=BatchSpanProcessor),
        "opentelemetry.exporter": types.ModuleType("opentelemetry.exporter"),
        "opentelemetry.exporter.otlp": types.ModuleType("opentelemetry.exporter.otlp"),
        "opentelemetry.exporter.otlp.proto": types.ModuleType("opentelemetry.exporter.otlp.proto"),
        "opentelemetry.exporter.otlp.proto.http": types.ModuleType("opentelemetry.exporter.otlp.proto.http"),
        "opentelemetry.exporter.otlp.proto.http.trace_exporter": types.SimpleNamespace(
            OTLPSpanExporter=OTLPSpanExporter),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return made


@pytest.mark.parametrize("env, expected", [
    ({}, "http://otel-collector:4318/v1/traces"),
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318/"}, "http://localhost:4318/v1/traces"),
    ({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://elsewhere:4318/custom"}, "http://elsewhere:4318/custom"),
])
def test_configure_with_the_sdk_exports_to_the_collector(fresh_tracing, env, expected):
    for key, val in env.items():
        fresh_tracing.setenv(key, val)
    api = _FakeApi()
    fresh_tracing.setattr(tracing, "_TRACE", api)
    made = _fake_sdk(fresh_tracing)
    assert tracing.configure("agent") is True
    assert made["endpoint"] == expected
    assert api.provider.resource == {"service.name": "agent"}
    assert api.provider.processors[0].exporter.endpoint == expected
    assert tracing.enabled() is True


def test_service_name_can_come_from_the_environment(fresh_tracing):
    fresh_tracing.setenv("OTEL_SERVICE_NAME", "agent-retrieval-arm")
    api = _FakeApi()
    fresh_tracing.setattr(tracing, "_TRACE", api)
    _fake_sdk(fresh_tracing)
    assert tracing.configure("agent") is True
    assert api.provider.resource == {"service.name": "agent-retrieval-arm"}


# --- tracing: the no-op paths -------------------------------------------------------------------------

def test_without_opentelemetry_everything_is_a_no_op(fresh_tracing, json_log):
    fresh_tracing.setattr(logs, "_OTEL", None)                         # logs: no span ids either
    fresh_tracing.setitem(sys.modules, "opentelemetry", None)          # as if not installed
    assert tracing._load_trace_api() is None
    fresh_tracing.setattr(tracing, "_TRACE", None)
    with tracing.incident_trace("INC-7", trace_id="T-7") as root:
        logging.getLogger("agent").info("run started")
        with tracing.tool_call("get_telemetry", {"from": "x"}, incident_id="INC-7") as s:
            s.set(result_ids=["R-7-1"])
            s.add_event("stored", result_id="R-7-1")
            s.record_exception(RuntimeError("ignored"))
        assert root.raw is None and not root.is_recording() and root.trace_id is None
        assert tracing.current_ids() == (None, None)
    headers = {"x": "1"}
    assert tracing.inject(headers) == {"x": "1"}
    assert tracing.extract(headers) is None
    line = json_log()[0]
    assert (line["incident_id"], line["trace_id"]) == ("INC-7", "T-7")   # log correlation still works


def test_with_the_api_only_spans_do_not_record(fresh_tracing):
    with tracing.span("check", incident_id="INC-8", arguments={"window": 3}, result_ids=["R-8-1"]) as s:
        assert s.raw is not None and not s.is_recording()
        s.set(arguments={"nested": {"a": [1, 2]}}, nothing=None)
        assert tracing.current_ids() == (None, None)
    with pytest.raises(ValueError, match="span kind"):
        with tracing.span("bad", kind="sideways"):
            pass


def test_attribute_values_become_what_opentelemetry_accepts():
    long_text = "x" * (tracing.MAX_ATTRIBUTE_CHARS + 100)
    converted = tracing.attributes({
        "incident_id": "INC-9", "trace_id": "T-9", "result_ids": ["R-9-1", "R-9-2"],
        "arguments": {"to": "2020-07-15T00:30:00Z", "from": "2020-07-14T00:30:00Z", "fields": ["a", "b"]},
        "budget_left": 11, "share": 0.25, "valid": False, "skipped": None, "empty": [],
        "mixed_numbers": [1, 2.5], "mixed": [1, "a"], "codes": {"b", "a"}, "long": long_text, "obj": object,
    })
    assert converted["incident_id"] == "INC-9" and converted["trace_id"] == "T-9"
    assert converted["result_ids"] == ("R-9-1", "R-9-2")
    assert converted["arguments"] == ('{"fields":["a","b"],"from":"2020-07-14T00:30:00Z",'
                                      '"to":"2020-07-15T00:30:00Z"}')                  # canonical JSON
    assert (converted["budget_left"], converted["share"], converted["valid"]) == (11, 0.25, False)
    assert "skipped" not in converted and converted["empty"] == ()
    assert converted["mixed_numbers"] == (1.0, 2.5) and converted["mixed"] == '[1,"a"]'
    assert converted["codes"] == ("a", "b")
    assert len(converted["long"]) == tracing.MAX_ATTRIBUTE_CHARS and converted["long"].endswith("(truncated)")
    assert converted["obj"] == str(object)


# --- tracing: what a recording span receives (stand-in tracer) ---------------------------------------

class _FakeSpan:
    def __init__(self, name, attributes, context, kind):
        self.name, self.attributes, self.context, self.kind = name, dict(attributes or {}), context, kind
        self.events = []

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def add_event(self, name, attributes=None):
        self.events.append((name, attributes))

    def record_exception(self, exc):
        self.events.append(("exception", exc))

    def is_recording(self):
        return True

    def get_span_context(self):
        from opentelemetry.trace import SpanContext, TraceFlags
        return SpanContext(trace_id=0x1F, span_id=0x2F, is_remote=False, trace_flags=TraceFlags(1))


class _FakeTracer:
    def __init__(self):
        self.spans = []

    @contextmanager
    def start_as_current_span(self, name, attributes=None, context=None, kind=None):
        s = _FakeSpan(name, attributes, context, kind)
        self.spans.append(s)
        yield s


def test_incident_trace_and_tool_calls_carry_ids_arguments_and_results(fresh_tracing, json_log):
    from opentelemetry.trace import SpanKind

    fake = _FakeTracer()
    fresh_tracing.setattr(tracing, "_tracer", fake)
    with tracing.incident_trace("INC-a1-20200715T0030Z", trace_id="T-42", arm="full") as root:
        logging.getLogger("agent").info("run started")
        with tracing.tool_call("run_check", {"check": "C4", "as_of": "2020-07-15T00:30:00Z"},
                               incident_id="INC-a1-20200715T0030Z") as call:
            call.set(result_ids=["R-0042-1"])
        with tracing.tool_call("get_telemetry", None, kind="server"):
            pass
    assert root.is_recording() and root.trace_id == f"{0x1F:032x}"
    incident, check, telemetry = fake.spans
    assert incident.name == "incident"
    assert incident.attributes == {"incident_id": "INC-a1-20200715T0030Z", "trace_id": "T-42", "arm": "full"}
    assert incident.context is not None and len(incident.context) == 0      # an empty context: a new trace
    assert check.name == "tool run_check" and check.kind == SpanKind.CLIENT
    assert check.attributes == {"tool": "run_check", "incident_id": "INC-a1-20200715T0030Z",
                                "arguments": '{"as_of":"2020-07-15T00:30:00Z","check":"C4"}',
                                "result_ids": ("R-0042-1",)}
    assert telemetry.kind == SpanKind.SERVER and telemetry.attributes["arguments"] == "{}"
    line = json_log()[0]
    assert (line["incident_id"], line["trace_id"]) == ("INC-a1-20200715T0030Z", "T-42")


def test_incident_trace_does_not_continue_the_callers_trace(fresh_tracing):
    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

    caller = SpanContext(trace_id=0x77, span_id=0x88, is_remote=False, trace_flags=TraceFlags(1))
    with trace.use_span(NonRecordingSpan(caller)):
        assert tracing.current_ids()[0] == f"{0x77:032x}"
        with tracing.incident_trace("INC-10"):
            assert tracing.current_ids() == (None, None)      # API only: no parent, and no ids of its own


def test_trace_context_travels_in_headers_to_the_tool_server(fresh_tracing):
    """The agent injects traceparent into the MCP request; the server's span continues that trace."""
    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

    agent_side = SpanContext(trace_id=0x1234ABCD, span_id=0x42, is_remote=False, trace_flags=TraceFlags(1))
    with trace.use_span(NonRecordingSpan(agent_side)):
        headers = tracing.inject({"content-type": "application/json"})
    assert headers["traceparent"] == f"00-{0x1234ABCD:032x}-{0x42:016x}-01"
    with tracing.span("tool get_telemetry", context=tracing.extract(headers), kind="server") as s:
        assert tracing.current_ids()[0] == f"{0x1234ABCD:032x}"
        assert s.trace_id == f"{0x1234ABCD:032x}"
    with tracing.tool_call("get_telemetry", {"hours": 24}, kind="server", context=tracing.extract(headers)) as s:
        assert s.trace_id == f"{0x1234ABCD:032x}"         # context is used, not turned into an attribute
