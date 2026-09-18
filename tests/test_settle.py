"""The settle rule: when a colour is read back after a fade, when a comparator may judge it,
which commands deserve a read, and one due time per fixture."""

from __future__ import annotations

import pytest

from pascl.core.settle import (
    DRIFT_TOLERANCE,
    READ_ALLOWANCE_S,
    READ_MARGIN_S,
    ReadSchedule,
    arm_at,
    fade_end,
    fade_end_of,
    read_at,
    worth_reading,
)


def test_the_read_follows_the_fade_by_the_margin_and_the_arm_by_the_allowance_too():
    end = fade_end(100.0, 30.0)
    assert end == 130.0
    assert read_at(end) == 130.0 + READ_MARGIN_S
    assert arm_at(end) == 130.0 + READ_MARGIN_S + READ_ALLOWANCE_S
    assert arm_at(end) > read_at(end) > end


def test_a_missing_or_negative_transition_ends_at_the_command():
    assert fade_end(50.0, 0.0) == 50.0
    assert fade_end(50.0, -3.0) == 50.0


def test_a_fixture_stops_moving_at_the_latest_of_its_commands_on_any_channel():
    # A colour command at 95 s over 5 s and a brightness-only command at 100 s over 30 s: the
    # comparator judges the fixture, so it waits for the brightness fade, not the colour one.
    assert fade_end_of((95.0, 5.0), (100.0, 30.0)) == 130.0
    assert fade_end_of((100.0, 30.0), (95.0, 5.0)) == 130.0
    # A later, shorter command does not pull the fade end back over one still in flight.
    assert fade_end_of((100.0, 30.0), (110.0, 5.0)) == 130.0
    assert fade_end_of((100.0, -1.0)) == 100.0
    assert fade_end_of() == 0.0


def test_the_constants_are_the_reference_installation_numbers():
    # The loft's generated Jinja carries these three; a change here must be a decision.
    assert READ_MARGIN_S == 1.5
    assert READ_ALLOWANCE_S == 4.0
    assert DRIFT_TOLERANCE == 0.003


@pytest.mark.parametrize(
    ("previous", "xy", "expected"),
    [
        (None, (0.4, 0.4), True),  # nothing known: read
        ((0.4, 0.4), (0.4, 0.4), False),  # did not move
        ((0.4, 0.4), (0.402, 0.402), False),  # inside tolerance on both axes
        ((0.4, 0.4), (0.404, 0.4), True),  # x past tolerance
        ((0.4, 0.4), (0.4, 0.396), True),  # y past tolerance
    ],
)
def test_only_a_move_past_the_comparator_tolerance_is_worth_a_read(previous, xy, expected):
    assert worth_reading(previous, xy) is expected


def test_a_dark_fixture_is_never_read():
    assert worth_reading(None, (0.4, 0.4), lit=False) is False
    assert worth_reading((0.1, 0.1), (0.5, 0.5), lit=False) is False


def test_the_tolerance_can_be_the_consumers():
    assert worth_reading((0.4, 0.4), (0.402, 0.4), tol=0.001) is True


def test_one_due_time_per_fixture_and_a_new_command_moves_it():
    sched = ReadSchedule()
    assert sched.commanded("a", 0.0, 10.0) == 10.0 + READ_MARGIN_S
    sched.commanded("b", 0.0, 2.0)
    # a is re-commanded before its read is due: the earlier read never happens
    sched.commanded("a", 5.0, 30.0)
    assert sched.pending() == {"a": 35.0 + READ_MARGIN_S, "b": 2.0 + READ_MARGIN_S}
    assert sched.next_due() == 2.0 + READ_MARGIN_S
    assert sched.due(3.0) == []
    assert sched.due(2.0 + READ_MARGIN_S) == ["b"]
    assert sched.due(2.0 + READ_MARGIN_S) == []  # handed out once
    assert sched.pending() == {"a": 35.0 + READ_MARGIN_S}
    assert sched.due(100.0) == ["a"]
    assert sched.next_due() is None


def test_due_fixtures_come_earliest_first_and_forget_drops_one():
    sched = ReadSchedule()
    sched.commanded("late", 0.0, 20.0)
    sched.commanded("early", 0.0, 1.0)
    sched.commanded("gone", 0.0, 0.0)
    sched.forget("gone")
    sched.forget("never-scheduled")
    assert sched.due(1000.0) == ["early", "late"]
