"""The replay comparison on the public demo home with a hand-built trace."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pascl.core.palette import natural_white, natural_xy
from pascl.core.render import FixtureState, RoomState, SolarState, render_fixture
from pascl.harness.replay import delta_e, describe, replay
from pascl.harness.trace import Record
from pascl.model import HomeModel, load

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "demo_home.yaml"
T0 = datetime(2026, 6, 21, 19, 0, tzinfo=UTC)
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


def _snapshot(model: HomeModel, scene_xy: tuple[float, float]) -> Record:
    data: dict[str, object] = {
        "solar.progress": 0.5,
        "solar.factor": 1.0,
        "solar.ready": True,
        "rooms.den.presence": True,
        "rooms.den.multiplier": 1.0,
        "scene.offset": 0,
        "fixtures.den_pendant_1.scene_x": scene_xy[0],
        "fixtures.den_pendant_1.scene_y": scene_xy[1],
        # a known host state; an unknown one would make the replay assume a recovery frame
        "fixtures.den_pendant_1.light": {"on": True, "brightness": 100},
    }
    for k, v in PCT.items():
        data[f"solar.pct.{k}"] = v
    return Record(t=T0, kind="snapshot", data=data)


def test_replay_matches_the_engines_own_frame_and_flags_a_drift(model: HomeModel) -> None:
    solar = SolarState(progress=0.5, factor=1.0, pct=PCT)
    white = natural_white(0.5, 1.0, PCT)
    calib = model.calibrations["hue_bulb"]
    scene_xy = natural_xy(white, calib)
    frame = render_fixture(
        model,
        "den",
        "den_pendant_1",
        solar,
        RoomState(),
        FixtureState(scene_xy=scene_xy),
        white,
        model.scenes.palettes["natural_white"],
    )
    assert frame.on and frame.brightness is not None and frame.ct_mired is not None

    records = [
        _snapshot(model, scene_xy),
        Record(
            t=T0 + timedelta(seconds=1),
            kind="output",
            path="fixtures.den_pendant_1",
            data={"on": True, "brightness": frame.brightness, "ct_mired": frame.ct_mired},
        ),
        Record(
            t=T0 + timedelta(seconds=2),
            kind="output",
            path="fixtures.den_pendant_1",
            data={"brightness": frame.brightness + 10},
        ),
        Record(
            t=T0 + timedelta(seconds=3),
            kind="output",
            path="groups.den.no_such_group",
            data={"on": True},
        ),
        Record(
            t=T0 + timedelta(seconds=4),
            kind="output",
            path="fixtures.den_pendant_1",
            data={"transition_s": 5.0},
        ),
        Record(
            t=T0 + timedelta(seconds=5),
            kind="input",
            path="rooms.den.presence",
            value=False,
        ),
        # Presence has cleared and the vacancy timer has not moved yet: the room is fading, so
        # a vacant-tier brightness is what the reference would publish here.
        Record(
            t=T0 + timedelta(seconds=6),
            kind="output",
            path="fixtures.den_pendant_1",
            data={"brightness": 999},
        ),
        # The den is on its space's timer; that timer finishing is the sweep.
        Record(
            t=T0 + timedelta(seconds=7),
            kind="input",
            path="spaces.house.vacancy_timer",
            value="idle:finished",
        ),
        Record(
            t=T0 + timedelta(seconds=8),
            kind="output",
            path="fixtures.den_pendant_1",
            data={"on": False},
        ),
    ]
    result = replay(model, records)
    assert len(result.samples) == 4
    assert result.samples[0].ok
    assert not result.samples[1].ok
    assert [v.field for v in result.samples[1].verdicts if not v.ok] == ["brightness"]
    fading = result.samples[2]
    assert fading.context["phase"] == "fading" and fading.frame.on
    assert result.samples[3].ok  # the timer's idle edge after the presence edge is the sweep
    assert result.samples[3].context["phase"] == "vacant"
    assert result.skipped["unknown_group"] == 1
    assert result.skipped["no_comparable_field"] == 1
    assert result.fields["brightness:ok"] == 1 and result.fields["brightness:miss"] == 2
    line = describe(result.samples[1])
    assert "brightness: expected" in line and "phase=occupied" in line
    assert "mismatched 2" in result.summary()


def test_delta_e_is_zero_on_identity_and_large_between_the_white_anchors() -> None:
    assert delta_e((0.53, 0.41), (0.53, 0.41)) == 0.0
    assert delta_e((0.53, 0.41), (0.438, 0.405)) > 2.0
    assert delta_e((0.53, 0.41), (0.531, 0.41)) < 2.0
