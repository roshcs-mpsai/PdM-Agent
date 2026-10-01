"""Time helpers: everything inside PdM-Agent is UTC (FRD section 5, time base)."""
from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|min|h|d)\s*$")
_UNIT_SECONDS = {"s": 1, "min": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> dt.timedelta:
    """'10s', '30min', '6h', '7d' -> timedelta. The units the YAML files use."""
    match = _DURATION.match(str(text))
    if not match:
        raise ValueError(f"not a duration: {text!r} (use s, min, h or d, e.g. '30min')")
    number, unit = match.groups()
    return dt.timedelta(seconds=float(number) * _UNIT_SECONDS[unit])


def to_utc(value, tz: str = "UTC") -> dt.datetime:
    """Normalise a YAML or payload time to an aware UTC datetime.

    PyYAML yields a ``date`` for '2020-04-25', a naive ``datetime`` when
    seconds are present, and a plain string for '2020-05-30 12:00'; payloads
    carry ISO strings. A value without an offset is read in ``tz``, the
    declared source timezone (ING-09).
    """
    if isinstance(value, dt.datetime):
        moment = value
    elif isinstance(value, dt.date):
        moment = dt.datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            moment = dt.datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"not an ISO time: {value!r}") from exc
    else:
        raise TypeError(f"cannot read a time from {type(value).__name__}: {value!r}")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone(tz))
    return moment.astimezone(UTC)


def timezone(name: str) -> dt.tzinfo:
    """'UTC' without needing a tz database; anything else through zoneinfo."""
    if name.upper() in ("UTC", "ETC/UTC", "Z"):
        return UTC
    return ZoneInfo(name)


def iso_utc(moment: dt.datetime) -> str:
    """Aware datetime -> '2020-04-17T00:01:00Z' (the payload time format)."""
    if moment.tzinfo is None:
        raise ValueError("iso_utc needs an aware datetime; call to_utc first")
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
