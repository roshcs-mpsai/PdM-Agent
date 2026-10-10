"""Structured JSON logs (OPS-01, T24).

``setup(service)`` puts one handler on the root logger that writes every record
as one JSON object per line:

    {"ts": "2026-10-10T13:45:01.123Z", "level": "INFO", "service": "gate", "logger": "gate",
     "message": "incident INC-<asset>-20200715T0030Z opened at ...", "incident_id": "INC-<asset>-20200715T0030Z"}

* ``ts`` is wall-clock UTC with ``Z`` (the time the line was written; stream
  time stays in the message and payloads);
* ``incident_id``, ``trace_id``, ``result_id``, ``result_ids`` and ``asset``
  come right after ``message`` when present, from ``extra=`` or from
  ``context()``; any other ``extra=`` field follows, sorted by name;
* ``otel_trace_id`` and ``otel_span_id`` are added while an OpenTelemetry span
  with a valid context is current (pdm_common.tracing), so a log line leads to
  its trace in Jaeger;
* ``exception`` holds the traceback text of ``log.exception(...)``.

The services' loggers do not change: ``logging.getLogger("ingest")`` and
``log.info("... %s", x)`` work as before, whether ``main()`` calls
``logging.basicConfig`` or ``logs.setup``. ``setup`` replaces a plain stderr or
stdout handler left by ``basicConfig`` (otherwise every line would print twice)
and leaves any other handler, such as pytest's log capture, in place.

``PDM_LOG_FORMAT=text`` switches to one plain line per record
(``<time> <logger> <message>``) for a terminal; the stack logs JSON.

    from pdm_common import logs
    logs.setup("agent")
    with logs.context(incident_id=iid, trace_id=tid):
        log.info("run started")          # carries incident_id and trace_id
"""
from __future__ import annotations

import contextvars
import datetime as dt
import json
import logging
import math
import os
import sys
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Optional, TextIO, Union

# Correlation fields, in the order they appear after "message" (OPS-01).
CORRELATION = ("incident_id", "trace_id", "result_id", "result_ids", "asset", "otel_trace_id", "otel_span_id")
BASE = ("ts", "level", "service", "logger", "message")

# Attributes every LogRecord has; anything else on a record came from extra=.
_RECORD_ATTRS = frozenset(vars(logging.LogRecord("x", logging.INFO, "x", 0, "x", None, None))) | {
    "message", "asctime", "taskName"}
_SKIP = frozenset({"color_message"})          # uvicorn's coloured copy of the message

_CONTEXT: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar("pdm_log_context", default={})

_UNSET = object()
_OTEL: Any = _UNSET                            # opentelemetry.trace, or None when not installed


@contextmanager
def context(**fields) -> Iterator[dict]:
    """Add ``fields`` to every record logged inside the block, in this thread and in asyncio
    tasks created inside it (a thread started inside it does not inherit them). Nested
    blocks add to the outer one and restore it on exit; ``None`` values are ignored; an
    ``extra=`` field of the same name wins over the context."""
    merged = dict(_CONTEXT.get())
    merged.update({k: v for k, v in fields.items() if v is not None})
    token = _CONTEXT.set(merged)
    try:
        yield dict(merged)
    finally:
        _CONTEXT.reset(token)


def current() -> dict:
    """The fields ``context()`` adds at this point."""
    return dict(_CONTEXT.get())


def _otel_ids() -> Optional[tuple[str, str]]:
    """(trace id, span id) in hex of the current OpenTelemetry span, if it has a valid context."""
    global _OTEL
    if _OTEL is _UNSET:
        try:
            from opentelemetry import trace as otel_trace
        except ImportError:
            otel_trace = None
        _OTEL = otel_trace
    if _OTEL is None:
        return None
    ctx = _OTEL.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


def _iso(created: float) -> str:
    moment = dt.datetime.fromtimestamp(created, tz=dt.timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _plain(value: Any) -> Any:
    """Something json.dumps can write without NaN: non-finite floats become strings,
    tuples and sets become lists, unknown objects their str()."""
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return str(value)


class JsonFormatter(logging.Formatter):
    """One JSON object per record; see the module docstring for the fields."""

    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def fields(self, record: logging.LogRecord) -> dict:
        doc = {"ts": _iso(record.created), "level": record.levelname, "service": self.service,
               "logger": record.name, "message": record.getMessage()}
        extra = dict(_CONTEXT.get())
        extra.update({k: v for k, v in vars(record).items()
                      if k not in _RECORD_ATTRS and k not in _SKIP and not k.startswith("_")})
        if "otel_trace_id" not in extra:
            ids = _otel_ids()
            if ids is not None:
                extra["otel_trace_id"], extra["otel_span_id"] = ids
        for key in CORRELATION:
            if key in extra:
                doc[key] = extra.pop(key)
        for key in sorted(extra):
            doc[key if key not in BASE else f"extra_{key}"] = extra[key]   # never overwrite the base fields
        if record.exc_info:
            doc["exception"] = self.formatException(record.exc_info)
        elif record.exc_text:
            doc["exception"] = record.exc_text
        if record.stack_info:
            doc["stack"] = self.formatStack(record.stack_info)
        return doc

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(_plain(self.fields(record)), ensure_ascii=False, allow_nan=False)


def _level(level: Union[int, str, None]) -> int:
    if level is None:
        level = os.environ.get("PDM_LOG_LEVEL", "INFO")
    if isinstance(level, str):
        named = logging.getLevelName(level.strip().upper())
        if not isinstance(named, int):
            raise ValueError(f"not a log level: {level!r}")
        return named
    return int(level)


def _left_by_basic_config(handler: logging.Handler) -> bool:
    return type(handler) is logging.StreamHandler and handler.stream in (
        sys.stderr, sys.stdout, sys.__stderr__, sys.__stdout__)


def setup(service: str, level: Union[int, str, None] = None, *, stream: Optional[TextIO] = None) -> logging.Handler:
    """Log every record as one JSON line to ``stream`` (default stderr; stdout stays free
    for the reports services print). ``level``: a number or name, default $PDM_LOG_LEVEL
    or INFO. Calling it again replaces the handler it installed before. Returns the handler."""
    number = _level(level)                     # a bad level fails before anything changes
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_pdm_json", False) or _left_by_basic_config(handler):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr if stream is None else stream)
    handler._pdm_json = True                   # type: ignore[attr-defined]
    if os.environ.get("PDM_LOG_FORMAT", "json").strip().lower() == "text":
        # one readable line per record, for a terminal; the deployed stack logs JSON
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    else:
        handler.setFormatter(JsonFormatter(service))
    root.addHandler(handler)
    root.setLevel(number)
    return handler


__all__ = ["BASE", "CORRELATION", "JsonFormatter", "context", "current", "setup"]
