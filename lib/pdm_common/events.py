"""Events file loading (CFG-04) and the time conventions it declares.

events.yaml (v0.2 header) fixes how spans are read, and every consumer uses
these helpers rather than re-deciding:

* all times are UTC;
* spans written with a time include their end instant: [start, end];
* spans written as dates cover whole days, end day included;
* buffers and regimes are half-open: [start, end).

Spans come back half-open, as aware UTC datetimes, so they compose with
``start <= t < end`` everywhere.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from pdm_common.hashing import sha256_file
from pdm_common.profile import read_yaml
from pdm_common.timeutil import to_utc

# An inclusive end instant becomes an exclusive end one tick later.
INCLUSIVE = dt.timedelta(microseconds=1)


class EventsError(ValueError):
    """The events file cannot be read or is malformed."""


def load_events(path: str | Path) -> dict:
    """Load an events file; the failures list must be present."""
    events = read_yaml(Path(path), error=EventsError, what="events file")
    if not isinstance(events.get("failures"), list):
        raise EventsError(f"{path}: events file has no failures list")
    return events


def events_hash(path: str | Path) -> str:
    """SHA-256 of the events file: stamped beside the profile hash (CFG-07)."""
    return sha256_file(path)


_DATE_ONLY = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s*$")


def _is_date_only(value) -> bool:
    """A date with no time: PyYAML's date, or a quoted '2020-07-02'."""
    if isinstance(value, str):
        return bool(_DATE_ONLY.match(value))
    return isinstance(value, dt.date) and not isinstance(value, dt.datetime)


def event_span(entry: dict) -> tuple[dt.datetime, dt.datetime]:
    """A failure, mask, grey, uncertain or candidate span as [start, end)."""
    start = to_utc(entry["start"])
    end = entry["end"]
    if _is_date_only(end):
        return start, to_utc(end) + dt.timedelta(days=1)
    return start, to_utc(end) + INCLUSIVE


def half_open(entry: dict) -> tuple[dt.datetime, dt.datetime]:
    """A regime or buffer span: [start, end) exactly as written."""
    return to_utc(entry["start"]), to_utc(entry["end"])
