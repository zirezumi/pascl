"""The injected clock.

Nothing in the render core or the estimator reads wall time. Every timestamp they see arrives
through a :class:`Clock` handed in by the shell, so a replay, a preview or a test can run the
same code at any instant, at any speed, deterministically. ``tests/test_purity.py`` fails the
build if either package reaches for ``time``, ``datetime.now`` or :class:`SystemClock` directly.

Two clocks exist. :class:`SystemClock` is the real one and belongs to the shell alone.
:class:`ManualClock` moves only when told to and is what tests, replays and previews inject.
"""

from __future__ import annotations

import time as _time
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """What the engine is allowed to know about time."""

    def now(self) -> datetime:
        """Wall time, always timezone-aware and in UTC."""

    def monotonic(self) -> float:
        """Seconds on a clock that never runs backwards; only differences are meaningful."""


class SystemClock:
    """The real clock. Only the shell may construct one."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return _time.monotonic()


class ManualClock:
    """A clock that moves only when told to.

    ``advance`` moves both wall and monotonic time forward together. ``set`` jumps wall time, as a
    replay does when it seeks to another day; monotonic time then moves forward by the jump when
    the jump is forward and not at all when it is backward, so it keeps its promise.
    """

    def __init__(self, now: datetime, monotonic: float = 0.0) -> None:
        self._now = _as_utc(now)
        self._monotonic = monotonic

    @classmethod
    def at(cls, when: datetime) -> ManualClock:
        return cls(when)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, by: float | timedelta) -> None:
        delta = by if isinstance(by, timedelta) else timedelta(seconds=by)
        if delta < timedelta(0):
            raise ValueError("a clock does not run backwards; use set() to jump")
        self._now += delta
        self._monotonic += delta.total_seconds()

    def set(self, when: datetime) -> None:
        when = _as_utc(when)
        jump = (when - self._now).total_seconds()
        self._now = when
        if jump > 0:
            self._monotonic += jump

    def __repr__(self) -> str:
        return f"ManualClock(now={self._now.isoformat()}, monotonic={self._monotonic:.3f})"


def _as_utc(when: datetime) -> datetime:
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("the engine only accepts timezone-aware datetimes")
    return when.astimezone(UTC)
