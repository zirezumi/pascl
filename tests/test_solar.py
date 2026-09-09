"""Invariants of the solar clock on a generic mid-latitude site and at the polar edge."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from pascl.core import solar
from pascl.core.solar import DayAnchors, Site, SolarParams

SEATTLE = Site(
    latitude=47.61, longitude=-122.33, elevation_m=50.0, tz=ZoneInfo("America/Los_Angeles")
)
TROMSO = Site(latitude=69.65, longitude=18.96, elevation_m=10.0, tz=ZoneInfo("Europe/Oslo"))

ORDERED_MORNING = (
    "midnight",
    "astronomical_dawn",
    "nautical_dawn",
    "civil_dawn",
    "sunrise",
    "golden_hour_am_end",
    "am_early",
    "am_mid",
    "am_late",
    "noon",
)
ORDERED_EVENING = (
    "noon",
    "pm_early",
    "pm_mid",
    "pm_late",
    "golden_hour_pm_start",
    "sunset",
    "civil_dusk",
    "nautical_dusk",
    "astronomical_dusk",
    "next_midnight",
)


@pytest.fixture(params=[date(2026, 3, 8), date(2026, 6, 21), date(2026, 9, 8), date(2026, 12, 21)])
def day(request: pytest.FixtureRequest) -> date:
    return request.param  # type: ignore[no-any-return]


@pytest.fixture
def today(day: date) -> DayAnchors:
    return solar.compute_day(SEATTLE, day)


def _assert_ordered(anchors: DayAnchors, keys: tuple[str, ...]) -> None:
    for k1, k2 in pairwise(keys):
        assert anchors[k1] <= anchors[k2], (
            f"{k1} ({anchors[k1]}) should not be after {k2} ({anchors[k2]})"
        )


def test_every_anchor_exists_and_is_aware(today: DayAnchors) -> None:
    assert set(today.at) == set(solar.ANCHOR_KEYS)
    for k, t in today.at.items():
        assert t.tzinfo is not None, k
    assert solar.ready(today)


def test_anchors_are_ordered_through_the_day(today: DayAnchors) -> None:
    _assert_ordered(today, ORDERED_MORNING)
    _assert_ordered(today, ORDERED_EVENING)
    assert today.day_length > timedelta(hours=8)
    assert today["next_midnight"] - today["midnight"] == pytest.approx(
        timedelta(days=1), abs=timedelta(minutes=2)
    )


def test_normalisation_pins_noon_and_keeps_order(today: DayAnchors) -> None:
    ref = solar.reference_day(SEATTLE, today)
    assert ref["noon"] == today["noon"]
    norm = solar.normalize(today, ref, dst_in_force=solar.is_dst(today["noon"]))
    assert norm["noon"] == today["noon"]
    assert set(norm.at) == set(solar.ANCHOR_KEYS)
    _assert_ordered(norm, ORDERED_MORNING)
    _assert_ordered(norm, ORDERED_EVENING)


def test_blend_zero_is_today_and_blend_one_is_the_reference(today: DayAnchors) -> None:
    ref = solar.reference_day(SEATTLE, today)
    dst = solar.is_dst(today["noon"])
    same = solar.normalize(today, ref, dst, SolarParams(solstice_blend_frac=0.0))
    for k in solar.PM_CHAIN + solar.AM_CHAIN:
        assert abs((same[k] - today[k]).total_seconds()) < 1e-6, k
    full = solar.normalize(today, ref, dst, SolarParams(solstice_blend_frac=1.0))
    correction = 0.0 if dst else 3600.0
    for chain, sign in ((solar.PM_CHAIN, 1), (solar.AM_CHAIN, -1)):
        for cur, nxt in pairwise(chain):
            got = (full[nxt] - full[cur]).total_seconds() * sign
            want = (ref[nxt] - ref[cur]).total_seconds() * sign
            # Outside DST the reference's evening is stretched by an hour and its morning
            # shortened by one, so the warped day keeps its summer wall-clock feel.
            if (cur, nxt) == ("noon", "sunset"):
                want += correction
            elif (cur, nxt) == ("noon", "sunrise"):
                want -= correction
            assert got == pytest.approx(want, abs=1e-3), (cur, nxt)


def test_progress_starts_at_zero_rises_monotonically_and_never_reaches_one(
    today: DayAnchors,
) -> None:
    start = today["midnight"]
    assert solar.day_progress(today, start) == 0.0
    last = -1.0
    for minutes in range(0, 24 * 60, 15):
        p = solar.day_progress(today, start + timedelta(minutes=minutes))
        assert p is not None
        assert p >= last
        assert p < 1.0
        last = p
    assert solar.day_progress(today, today["next_midnight"]) == solar.PROGRESS_CEILING


def test_anchor_percents_match_progress_at_the_anchor(today: DayAnchors) -> None:
    pct = solar.anchor_percents(today)
    assert set(pct) == set(solar.PERCENT_KEYS)
    for k, v in pct.items():
        assert v == solar.day_progress(today, today[k]), k
    assert pct["noon"] is not None and 0.4 < pct["noon"] < 0.6


def test_daylight_factor_is_zero_at_night_and_one_at_noon(today: DayAnchors) -> None:
    assert solar.daylight_factor(today, today["midnight"]) == 0.0
    assert solar.daylight_factor(today, today["astronomical_dusk"]) == 0.0
    assert solar.daylight_factor(today, today["noon"]) == pytest.approx(1.0)
    assert solar.daylight_factor(today, today["golden_hour_am_end"]) == pytest.approx(0.0, abs=1e-9)
    mid_morning = today["golden_hour_am_end"] + (today["noon"] - today["golden_hour_am_end"]) / 2
    assert 0.6 < solar.daylight_factor(today, mid_morning) < 0.8


def test_solar_date_is_yesterday_before_solar_midnight() -> None:
    day = date(2026, 9, 8)
    anchors = solar.compute_day(SEATTLE, day)
    just_before = anchors["midnight"] - timedelta(minutes=5)
    just_after = anchors["midnight"] + timedelta(minutes=5)
    assert solar.solar_date(SEATTLE, just_before) == day - timedelta(days=1)
    assert solar.solar_date(SEATTLE, just_after) == day


def test_polar_summer_degrades_to_the_backbone_without_raising() -> None:
    anchors = solar.compute_day(TROMSO, date(2026, 6, 21))
    assert anchors["astronomical_dawn"] == anchors["midnight"]
    assert anchors["astronomical_dusk"] == anchors["next_midnight"]
    assert solar.ready(anchors)
    ref = solar.reference_day(TROMSO, anchors)
    norm = solar.normalize(anchors, ref, dst_in_force=True)
    assert solar.day_progress(norm, anchors["noon"]) is not None


def test_position_is_sane_at_noon_and_midnight() -> None:
    anchors = solar.compute_day(SEATTLE, date(2026, 6, 21))
    at_noon = solar.position(SEATTLE, anchors["noon"])
    at_midnight = solar.position(SEATTLE, anchors["midnight"])
    assert 60 < at_noon.elevation < 70
    assert at_midnight.elevation < -15
    assert 170 < at_noon.azimuth < 190
    assert 22 < at_noon.declination < 24
    assert -5 < at_noon.equation_of_time < 0
    assert at_noon.elevation_mid < at_noon.elevation


def test_declination_and_equation_of_time_reference_values() -> None:
    # Perihelion-side check against published NOAA values (within the series' own precision).
    assert solar.declination(datetime(2026, 1, 1, 12, tzinfo=UTC)) == pytest.approx(-23.0, abs=0.2)
    assert solar.equation_of_time(datetime(2026, 11, 3, 12, tzinfo=UTC)) == pytest.approx(
        16.4, abs=0.3
    )
