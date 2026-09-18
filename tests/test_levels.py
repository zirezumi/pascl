"""Brightness levels: the host's rescale, its exact inverse, the comparator on device units, and
the level-report check.

The reference case is the reference installation's own defect: a bulb sent 130 with the intent
already at 128 (the render's allowed lag of two) reports 131 through the host's rescale, and a
comparator reading the attribute raw sees three and trips; the same comparator on device units
sees two and does not. That is the window class, 82% of that installation's brightness drift
repaints, and it must stay reproduced here.
"""

from __future__ import annotations

import pytest

from pascl.core.levels import (
    BRIGHTNESS_TOLERANCE,
    HOST_SCALE,
    ZIGBEE_SCALE,
    brightness_diverged,
    device_level,
    host_brightness,
)
from pascl.estimator.levels import EXACT_MIN, MIN_PAIRS, LevelPair, assess, fleet


def test_the_rescale_is_identity_below_127_and_plus_one_from_127_up():
    assert host_brightness(1) == 1
    assert host_brightness(126) == 126
    assert host_brightness(127) == 128
    assert host_brightness(200) == 201
    assert host_brightness(254) == 255
    assert host_brightness(254, scale=HOST_SCALE) == 254


def test_the_inverse_recovers_every_device_level_exactly():
    for level in range(0, ZIGBEE_SCALE + 1):
        assert device_level(host_brightness(level)) == level
    assert device_level(None) is None
    assert device_level(131, scale=HOST_SCALE) == 131
    assert device_level(300) == ZIGBEE_SCALE  # clamped, never above the transport's top


def test_the_window_case_trips_raw_and_not_on_device_units():
    sent, intent = 130, 128  # the render's allowed lag of two, on a falling ramp
    attribute = host_brightness(sent)  # 131
    assert brightness_diverged(attribute, intent)  # the defect: three, read raw
    assert not brightness_diverged(device_level(attribute), intent)  # two, in device units
    # a device that did not go where it was sent is still caught one step further on
    assert brightness_diverged(device_level(host_brightness(131)), intent)


def test_an_unreadable_side_counts_as_diverged():
    assert brightness_diverged(None, 100)
    assert brightness_diverged(100, None)
    assert BRIGHTNESS_TOLERANCE == 2


def pairs(levels, f):
    return [LevelPair(s, f(s)) for s in levels]


def test_a_fixture_reporting_the_rescale_gets_the_rescale_verdict():
    r = assess("bulb", pairs(range(100, 240, 4), host_brightness))
    assert r.verdict == "rescale" and r.exact == 1.0 and r.mismatches == ()


def test_below_the_knee_the_verdict_notes_the_coincidence():
    r = assess("dim", pairs(range(10, 110, 3), host_brightness))
    assert r.verdict == "rescale" and r.identity == 1.0 and "agree" in r.note


def test_a_transport_without_a_rescale_gets_identity():
    r = assess("lutron", pairs(range(130, 250, 3), lambda s: s))
    assert r.verdict == "identity" and r.exact < EXACT_MIN and r.identity == 1.0


def test_a_quantising_device_is_named_irregular_with_its_worst_pairs():
    r = assess("odd", pairs(range(130, 250, 2), lambda s: host_brightness(s) - (2 if s % 3 else 0)))
    assert r.verdict == "irregular"
    assert r.mismatches and all(rep == host_brightness(s) - 2 for s, rep in r.mismatches)


def test_the_check_declines_below_the_sample_floor():
    r = assess("new", pairs(range(130, 130 + MIN_PAIRS - 1), host_brightness))
    assert r.verdict == "declined" and r.pairs == MIN_PAIRS - 1


def test_fleet_counts_verdicts():
    reports = [assess("a", pairs(range(130, 250, 3), host_brightness)), assess("b", [])]
    assert fleet(reports) == {"rescale": 1, "declined": 1}


@pytest.mark.parametrize("scale", [ZIGBEE_SCALE, HOST_SCALE])
def test_scales_round_trip(scale):
    for level in range(0, scale + 1):
        assert device_level(host_brightness(level, scale), scale) == level
