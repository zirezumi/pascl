"""The render core on the public demo home: colour math, curves, palettes, frames, preview."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pascl.core import curves
from pascl.core.color import blend_xy, oklab_to_xy, xy_to_oklab
from pascl.core.palette import kelvin_xy, natural_white, natural_xy, static_index
from pascl.core.render import FixtureState, RoomState, SolarState, ambient_factor, render_fixture
from pascl.harness.preview import PreviewInputs, preview, scrub, solar_state
from pascl.model import HomeModel, load

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "demo_home.yaml"


@pytest.fixture(scope="module")
def model() -> HomeModel:
    return load(EXAMPLE.read_text(encoding="utf-8"))


# --- colour ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "xy", [(0.53, 0.41), (0.438, 0.405), (0.574, 0.39), (0.322, 0.329), (0.7, 0.29)]
)
def test_oklab_round_trip(xy: tuple[float, float]) -> None:
    lab = xy_to_oklab(*xy)
    back = oklab_to_xy(*lab)
    assert back[0] == pytest.approx(xy[0], abs=1e-6)
    assert back[1] == pytest.approx(xy[1], abs=1e-6)


def test_blend_endpoints_and_midpoint_are_sane() -> None:
    warm, cool = (0.53, 0.41), (0.438, 0.405)
    assert blend_xy(warm, cool, 0.0) == (0.53, 0.41)
    assert blend_xy(warm, cool, 1.0) == (0.438, 0.405)
    mid = blend_xy(warm, cool, 0.5)
    assert 0.438 < mid[0] < 0.53
    assert blend_xy(warm, cool, 7.0) == (0.438, 0.405)


# --- curves ----------------------------------------------------------------------------------

TABLE = {
    "midnight": 25,
    "pre_dawn_mid": 40,
    "civil_dawn": 90,
    "am_mid": 120,
    "noon": 165,
    "pm_mid": 120,
    "civil_dusk": 100,
    "pre_midnight_mid": 70,
}
ANCHORS = curves.AnchorTable.from_day(0.20, 0.50, 0.80)


def test_curve_hits_its_anchors_exactly() -> None:
    assert curves.interpolate(TABLE, 0.0, ANCHORS) == 25
    assert curves.interpolate(TABLE, 0.20, ANCHORS) == pytest.approx(90)
    assert curves.interpolate(TABLE, 0.50, ANCHORS) == pytest.approx(165)
    assert curves.interpolate(TABLE, 0.80, ANCHORS) == pytest.approx(100)


def test_curve_is_monotonic_between_anchors_and_eases() -> None:
    last = curves.interpolate(TABLE, 0.20, ANCHORS)
    for i in range(1, 100):
        p = 0.20 + (0.35 - 0.20) * i / 100
        v = curves.interpolate(TABLE, p, ANCHORS)
        assert v >= last
        last = v
    # rising segments ease out: past the midpoint the value is already most of the way up
    mid = curves.interpolate(TABLE, 0.275, ANCHORS)
    assert mid > 90 + 0.5 * (120 - 90)
    # falling segments ease in: at the midpoint the value has dropped less than half
    mid_fall = curves.interpolate(TABLE, 0.575, ANCHORS)
    assert mid_fall > 165 - 0.5 * (165 - 120)


def test_compose_scales_and_clamps() -> None:
    b = curves.compose(TABLE, 0.50, ANCHORS, ambient=1.1, multiplier=2.0, factor=1.0)
    assert b.solar_baseline == 181
    assert b.goal == 254
    assert b.baseline_target == pytest.approx(362.0)
    assert not b.off
    dim = curves.compose({k: 0 for k in TABLE}, 0.3, ANCHORS)
    assert dim.off and dim.solar_baseline == 1


# --- palette ---------------------------------------------------------------------------------


def test_natural_white_by_day_and_night() -> None:
    pct = {"astronomical_dawn": 0.12, "astronomical_dusk": 0.88}
    noon = natural_white(0.5, 1.0, pct)
    assert noon.kelvin == 3100 and not noon.is_night
    dawn = natural_white(0.2, 0.0, pct)
    assert dawn.kelvin == 2000
    midnight = natural_white(0.0, 0.0, pct)
    assert midnight.is_night and midnight.kelvin == pytest.approx(1475)
    dusk_edge = natural_white(0.88, 0.0, pct)
    assert dusk_edge.is_night and dusk_edge.kelvin == pytest.approx(2000)
    unready = natural_white(-1.0, 0.0, pct)
    assert unready.kelvin == 2000 and not unready.is_night


def test_static_index_wraps_and_handles_empty() -> None:
    assert static_index(10, 4507, 31, 5) == (10 + 4507 + 31) % 5
    assert static_index(0, 0, 0, 0) == -1


def test_kelvin_xy_follows_the_arc(model: HomeModel) -> None:
    calib = model.calibrations["hue_bulb"]
    assert kelvin_xy(2000, calib) == calib.warm_xy
    assert kelvin_xy(3100, calib) == calib.cool_xy
    assert kelvin_xy(1475, calib) == calib.night_xy


def test_ambient_factor_rules() -> None:
    assert ambient_factor(0.5, 1.2, True) == pytest.approx(1.1)
    assert ambient_factor(0.5, 1.2, False) == 1.0
    assert ambient_factor(None, 1.2, True) == 1.0
    assert ambient_factor(-0.3, 1.2, True) == 1.0
    assert ambient_factor(2.0, 1.2, True) == pytest.approx(1.2)


# --- render ----------------------------------------------------------------------------------

NOON = SolarState(
    progress=0.5,
    factor=1.0,
    pct={
        "civil_dawn": 0.2,
        "noon": 0.5,
        "civil_dusk": 0.8,
        "astronomical_dawn": 0.12,
        "astronomical_dusk": 0.88,
    },
)
NIGHT = SolarState(progress=0.0, factor=0.0, pct=NOON.pct)


def test_bulb_on_the_white_arc_renders_as_colour_temperature(model: HomeModel) -> None:
    white = natural_white(NOON.progress, NOON.factor, NOON.pct)
    calib = model.calibrations["hue_bulb"]
    f = render_fixture(
        model,
        "den",
        "den_pendant_1",
        NOON,
        RoomState(),
        FixtureState(),
        white,
        model.scenes.palettes["natural_white"],
    )
    assert f.on and f.mode == "ct" and f.ct_mired == 322 and f.xy is None
    assert f.brightness == 165 and f.solar_baseline == 165
    assert natural_xy(white, calib) == calib.cool_xy


def test_strip_never_takes_colour_temperature(model: HomeModel) -> None:
    white = natural_white(NOON.progress, NOON.factor, NOON.pct)
    f = render_fixture(
        model,
        "den",
        "den_strip",
        NOON,
        RoomState(),
        FixtureState(),
        white,
        model.scenes.palettes["natural_white"],
    )
    assert f.on and f.mode == "xy" and f.ct_mired is None
    assert f.xy == model.calibrations["hue_strip"].cool_xy


def test_night_white_is_below_the_bulbs_range_so_it_renders_as_xy(model: HomeModel) -> None:
    white = natural_white(NIGHT.progress, NIGHT.factor, NIGHT.pct)
    f = render_fixture(
        model,
        "den",
        "den_pendant_1",
        NIGHT,
        RoomState(),
        FixtureState(),
        white,
        model.scenes.palettes["natural_white"],
    )
    assert f.mode == "xy" and f.xy == model.calibrations["hue_bulb"].night_xy
    assert f.brightness == 25


def test_vacant_tier_multiplier_factor_and_held_off(model: HomeModel) -> None:
    white = natural_white(NOON.progress, NOON.factor, NOON.pct)
    pal = model.scenes.palettes["natural_white"]
    fading = render_fixture(
        model, "den", "den_pendant_1", NOON, RoomState(phase="fading"), FixtureState(), white, pal
    )
    assert fading.on and fading.brightness == 100
    vacant = render_fixture(
        model, "den", "den_pendant_1", NOON, RoomState(phase="vacant"), FixtureState(), white, pal
    )
    assert not vacant.on and vacant.brightness is None and vacant.mode == "off"
    scaled = render_fixture(
        model,
        "den",
        "den_pendant_1",
        NOON,
        RoomState(multiplier=0.5),
        FixtureState(factor=0.5),
        white,
        pal,
    )
    assert scaled.brightness == int(165 * 0.5 * 0.5)
    held = render_fixture(
        model, "den", "den_pendant_1", NOON, RoomState(), FixtureState(held_off=True), white, pal
    )
    assert not held.on


def test_sleep_suppresses_but_keeps_the_virtual_frame(model: HomeModel) -> None:
    white = natural_white(NIGHT.progress, NIGHT.factor, NIGHT.pct)
    f = render_fixture(
        model,
        "pantry",
        "pantry_light",
        NIGHT,
        RoomState(sleeping=True),
        FixtureState(),
        white,
        model.scenes.palettes["natural_white"],
    )
    assert not f.on and f.virtual and f.brightness == 45 and f.mode == "off"


def test_colour_override_pulls_the_goal_and_leaves_the_white_arc(model: HomeModel) -> None:
    white = natural_white(NOON.progress, NOON.factor, NOON.pct)
    pal = model.scenes.palettes["natural_white"]
    red = (0.7, 0.29)
    full = render_fixture(
        model,
        "den",
        "den_pendant_1",
        NOON,
        RoomState(),
        FixtureState(color_factor=1.0, override_xy=red),
        white,
        pal,
    )
    assert full.mode == "xy" and full.xy == red
    half = render_fixture(
        model,
        "den",
        "den_pendant_1",
        NOON,
        RoomState(),
        FixtureState(color_factor=0.5, override_xy=red),
        white,
        pal,
    )
    assert half.mode == "xy" and half.xy is not None and 0.438 < half.xy[0] < 0.7
    dead = render_fixture(
        model,
        "den",
        "den_pendant_1",
        NOON,
        RoomState(),
        FixtureState(color_factor=0.01, override_xy=red),
        white,
        pal,
    )
    assert dead.mode == "ct"


def test_static_palette_rotates_with_the_offset(model: HomeModel) -> None:
    white = natural_white(NOON.progress, NOON.factor, NOON.pct)
    dusk = model.scenes.palettes["dusk"]
    a = render_fixture(
        model,
        "den",
        "den_pendant_1",
        NOON,
        RoomState(),
        FixtureState(),
        white,
        dusk,
        scene_offset=0,
    )
    b = render_fixture(
        model,
        "den",
        "den_pendant_1",
        NOON,
        RoomState(),
        FixtureState(),
        white,
        dusk,
        scene_offset=1,
    )
    assert a.mode == "xy" and b.mode == "xy" and a.xy != b.xy
    assert a.xy in dusk.bulbs and b.xy in dusk.bulbs


# --- preview ---------------------------------------------------------------------------------


def test_preview_tracks_the_day(model: HomeModel) -> None:
    noon_utc = datetime(2026, 6, 21, 20, 0, tzinfo=UTC)
    r = preview(model, noon_utc)
    assert 0.45 < r.solar.progress < 0.6
    assert r.frames["den_pendant_1"].on
    night = preview(
        model, datetime(2026, 6, 21, 9, 0, tzinfo=UTC), PreviewInputs(phases={"den": "vacant"})
    )
    assert not night.frames["den_pendant_1"].on
    assert night.frames["pantry_light"].on


def test_scrub_covers_a_day_monotonically(model: HomeModel) -> None:
    start = datetime(2026, 3, 8, 8, 0, tzinfo=UTC)
    results = list(scrub(model, start, start + timedelta(hours=23), timedelta(hours=1)))
    assert len(results) == 24
    assert all(r.frames for r in results)
    sol = solar_state(model, start)
    assert set(sol.pct) >= {
        "civil_dawn",
        "noon",
        "civil_dusk",
        "astronomical_dawn",
        "astronomical_dusk",
    }
