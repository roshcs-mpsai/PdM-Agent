"""Streaming windows, Version Zero (R0.3).

``MinuteWindower`` turns a time-ordered stream of samples into fixed,
epoch-aligned buckets and reports each bucket's per-signal mean when the
stream moves past it. The ingest stub publishes these as window_v1, and the
R0 z-score fit feeds its training rows through the same class, so the scorer
is fitted on exactly the representation it scores.

R1a.5 replaces this with 10-minute windows on the masked, bridged 10 s grid.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def floor_time(moment: dt.datetime, length: dt.timedelta) -> dt.datetime:
    """Start of the epoch-aligned bucket of ``length`` holding ``moment``."""
    return EPOCH + ((moment - EPOCH) // length) * length


@dataclass
class Window:
    start: dt.datetime
    end: dt.datetime
    n: int                              # samples in the bucket
    means: dict[str, float]
    first_ts: dt.datetime
    last_ts: dt.datetime


@dataclass
class _Bucket:
    start: dt.datetime
    sums: dict[str, float]
    n: int = 0
    first_ts: dt.datetime | None = None
    last_ts: dt.datetime | None = None


class MinuteWindower:
    """Per-signal means over fixed buckets (default one minute).

    Samples must arrive in time order; the caller (ingest) quarantines any
    that do not. A bucket closes when the first sample of a later bucket
    arrives, or on ``flush()``.
    """

    def __init__(self, signals: list[str], length: dt.timedelta = dt.timedelta(minutes=1)):
        if not signals:
            raise ValueError("MinuteWindower needs at least one signal")
        self.signals = list(signals)
        self.length = length
        self._open: _Bucket | None = None

    @property
    def open_start(self) -> dt.datetime | None:
        return self._open.start if self._open else None

    def add(self, ts: dt.datetime, values: dict[str, float]) -> list[Window]:
        """Add one sample; return the window it closed, if any."""
        start = floor_time(ts, self.length)
        closed = []
        if self._open is not None and start != self._open.start:
            closed.append(self._close())
        if self._open is None:
            self._open = _Bucket(start=start, sums={s: 0.0 for s in self.signals}, first_ts=ts)
        bucket = self._open
        for s in self.signals:
            bucket.sums[s] += values[s]
        bucket.n += 1
        bucket.last_ts = ts
        return closed

    def flush(self) -> list[Window]:
        """Close the open bucket (end of stream or shutdown)."""
        return [self._close()] if self._open is not None else []

    def _close(self) -> Window:
        b, self._open = self._open, None
        return Window(
            start=b.start,
            end=b.start + self.length,
            n=b.n,
            means={s: b.sums[s] / b.n for s in self.signals},
            first_ts=b.first_ts,
            last_ts=b.last_ts,
        )
