"""A colour-temperature override blends from the white baseline only while that baseline is a
temperature the bulb can take; below the bulb's floor the baseline is xy and the override wins
the wire as a temperature only once it has fully decayed in."""

from __future__ import annotations

from pathlib import Path

import pytest

from pascl.core.palette import natural_white, natural_xy
from pascl.core.render import FixtureState, RoomState, SolarState, render_fixture
from pascl.model import HomeModel, load

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "demo_home.yaml"
PCT = {
    "astronomical_dawn": 0.10,
    "civil_dawn": 0.20,
    "noon": 0.50,
    "civil_dusk": 0.80,
    "astronomical_dusk": 0.90,
}


@pytest.fixture(scope="module")
def model() -> HomeModel:
    return load(EXAMPLE.read_text(encoding="utf-8"))


def _frame(model: HomeModel, solar: SolarState, cf: float):  # type: ignore[no-untyped-def]
    white = natural_white(solar.progress, solar.factor, solar.pct)
    anchor = natural_xy(white, model.calibrations["hue_bulb"])
    return render_fixture(
        model,
        "den",
        "den_pendant_1",
        solar,
        RoomState(),
        FixtureState(color_factor=cf, override_is_ct=True, override_kelvin=2700.0, scene_xy=anchor),
        white,
        model.scenes.palettes["natural_white"],
    )


def test_by_day_the_override_blends_in_mireds(model: HomeModel) -> None:
    noon = SolarState(progress=0.5, factor=1.0, pct=PCT)  # 3100 K, within a 2000 K floor
    half = _frame(model, noon, 0.5)
    assert half.mode == "ct" and half.ct_mired is not None
    assert int(1e6 / 3100) < half.ct_mired < int(1e6 / 2700)


def test_below_the_bulbs_floor_the_baseline_is_xy_until_the_override_wins(
    model: HomeModel,
) -> None:
    night = SolarState(progress=0.95, factor=0.0, pct=PCT)  # ~1738 K, below the 2000 K floor
    half = _frame(model, night, 0.5)
    assert half.mode == "xy" and half.ct_mired is None
    won = _frame(model, night, 1.0)
    assert won.mode == "ct" and won.ct_mired == int(1e6 / 2700)
