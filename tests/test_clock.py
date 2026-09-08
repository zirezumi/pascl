from datetime import UTC, datetime, timedelta, timezone

import pytest

from pascl.clock import Clock, ManualClock, SystemClock

T0 = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)


def test_both_clocks_satisfy_the_protocol() -> None:
    assert isinstance(SystemClock(), Clock)
    assert isinstance(ManualClock.at(T0), Clock)


def test_system_clock_is_aware_utc_and_monotonic_never_reverses() -> None:
    clock = SystemClock()
    now = clock.now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
    a = clock.monotonic()
    b = clock.monotonic()
    assert b >= a


def test_manual_clock_starts_where_told_and_stays_there() -> None:
    clock = ManualClock.at(T0)
    assert clock.now() == T0
    assert clock.now() == T0
    assert clock.monotonic() == 0.0


def test_advance_moves_wall_and_monotonic_time_together() -> None:
    clock = ManualClock.at(T0)
    clock.advance(45)
    clock.advance(timedelta(minutes=1))
    assert clock.now() == T0 + timedelta(seconds=105)
    assert clock.monotonic() == pytest.approx(105.0)


def test_advance_refuses_to_run_backwards() -> None:
    clock = ManualClock.at(T0)
    with pytest.raises(ValueError):
        clock.advance(-1)


def test_set_jumps_wall_time_but_monotonic_never_reverses() -> None:
    clock = ManualClock.at(T0)
    clock.advance(10)
    clock.set(T0 + timedelta(days=1))
    assert clock.now() == T0 + timedelta(days=1)
    # The jump is one day minus the ten seconds already elapsed, so monotonic lands on one day.
    assert clock.monotonic() == pytest.approx(86400)
    clock.set(T0)
    assert clock.now() == T0
    assert clock.monotonic() == pytest.approx(86400)


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError):
        ManualClock.at(datetime(2026, 6, 21, 12, 0))
    clock = ManualClock.at(T0)
    with pytest.raises(ValueError):
        clock.set(datetime(2026, 6, 22, 12, 0))


def test_other_zones_are_normalised_to_utc() -> None:
    pacific = timezone(timedelta(hours=-7))
    clock = ManualClock.at(datetime(2026, 6, 21, 5, 0, tzinfo=pacific))
    assert clock.now() == T0
    assert clock.now().tzinfo == UTC
