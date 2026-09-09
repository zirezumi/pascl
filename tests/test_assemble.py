"""The trace format, the binding expansion and the assembler on the public demo home."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pascl.harness.assemble import assemble
from pascl.harness.binding import expand, load_binding
from pascl.harness.trace import Record, normalize_command, read_trace, write_trace
from pascl.model import HomeModel, load

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def model() -> HomeModel:
    return load((ROOT / "examples" / "demo_home.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def binding():  # type: ignore[no-untyped-def]
    return load_binding((ROOT / "examples" / "demo_binding.yaml").read_text(encoding="utf-8"))


def test_record_round_trips_through_json() -> None:
    r = Record(
        t=datetime(2026, 9, 9, 1, 2, 3, 456000, tzinfo=UTC),
        kind="input",
        path="rooms.den.presence",
        value=True,
        mono=12.5,
    )
    again = Record.from_json(r.to_json())
    assert again == r
    out = Record(
        t=r.t, kind="output", path="fixtures.den_strip", data={"on": True, "brightness": 120}
    )
    assert Record.from_json(out.to_json()) == out


def test_normalize_command_maps_the_wire_vocabulary() -> None:
    cmd = normalize_command(
        {
            "state": "ON",
            "brightness": 120,
            "color": {"x": 0.5, "y": 0.4},
            "transition": 0.2,
            "foo": 1,
        },
        None,
    )
    assert cmd == {
        "on": True,
        "brightness": 120,
        "xy": [0.5, 0.4],
        "transition_s": 0.2,
        "extra": {"foo": 1},
    }
    assert normalize_command({"color_temp": 322}, None) == {"ct_mired": 322}
    assert normalize_command(None, "garbage") == {"raw": "garbage"}


def test_binding_expands_over_the_model(model: HomeModel, binding) -> None:  # type: ignore[no-untyped-def]
    idx = expand(binding, model)
    assert idx.entities["binary_sensor.den_presence"].path == "rooms.den.presence"
    assert (
        idx.entities["input_number.den_pendant_1_brightness_factor"].path
        == "fixtures.den_pendant_1.factor"
    )
    assert idx.entities["input_select.den_accents_scene_selector"].transform == "palette"
    assert idx.entities["sensor.ambient_fusion_den"].attribute == "coupling"
    assert idx.entities["sensor.sol_noon_normalized_pct"].path == "solar.pct.noon"
    assert idx.entities["input_text.palette_dusk_json"].path == "scene.palette.dusk"
    assert idx.entities["binary_sensor.den_radar_zone_1"].path == "sensors.den_radar_zone_1"
    assert idx.entities["den_plate/illuminance"].transform == "float"
    assert idx.topics["z2m-1/den_strip/set"].path == "fixtures.den_strip"
    assert idx.topics["z2m-1/den_bulbs/set"].path == "groups.den.den_bulbs"
    assert "timer.main_space_vacancy" in idx.entities


def _raw(t: str, **fields: object) -> str:
    return json.dumps({"t": t, **fields})


def test_assemble_builds_snapshot_events_outputs_and_counts_the_rest(
    model: HomeModel, binding, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    lines = [
        _raw(
            "2026-09-09T01:00:00.000000Z",
            kind="snapshot",
            states={
                "sensor.sol_normalized_p": {"state": "0.5", "attrs": {}},
                "binary_sensor.den_presence": {"state": "on", "attrs": {}},
                "input_select.den_accents_scene_selector": {"state": "Natural White", "attrs": {}},
                "sensor.ambient_fusion_den": {"state": "0.8", "attrs": {"coupling": 0.22}},
                "light.den_strip": {
                    "state": "on",
                    "attrs": {"brightness": 120, "xy_color": [0.5, 0.4]},
                },
                "sensor.somebody_elses_thing": {"state": "1", "attrs": {}},
            },
        ),
        _raw(
            "2026-09-09T01:00:45.000000Z",
            kind="state",
            id="sensor.sol_normalized_p",
            state="0.501",
            attrs={},
        ),
        _raw(
            "2026-09-09T01:01:00.000000Z",
            kind="state",
            id="input_number.den_pendant_1_brightness_intent_cache",
            state="163",
            attrs={},
        ),
        _raw(
            "2026-09-09T01:01:00.100000Z",
            kind="set",
            instance="z2m-1",
            target="den_pendant_1",
            topic="z2m-1/den_pendant_1/set",
            payload={"state": "ON", "brightness": 163, "transition": 5},
            raw=None,
        ),
        _raw(
            "2026-09-09T01:01:00.200000Z",
            kind="set",
            instance="z2m-1",
            target="den_plate",
            topic="z2m-1/den_plate/set",
            payload={"ledColorWhenOn": 10},
            raw=None,
        ),
        _raw(
            "2026-09-09T01:02:00.000000Z",
            kind="state",
            id="binary_sensor.den_radar_zone_1",
            state="off",
            attrs={},
        ),
        _raw(
            "2026-09-09T01:02:30.000000Z",
            kind="state",
            id="input_boolean.eeptime",
            state="unavailable",
            attrs={},
        ),
    ]
    result = assemble(model, binding, lines)
    kinds = [r.kind for r in result.records]
    assert kinds == ["snapshot", "input", "latent", "output", "sensor", "input"]
    snap = result.records[0]
    assert snap.data["solar.progress"] == 0.5
    assert snap.data["rooms.den.presence"] is True
    assert snap.data["scene_groups.den_accents"] == "natural_white"
    assert snap.data["ambient.scope.den.coupling"] == 0.22
    assert snap.data["fixtures.den_strip.light"] == {
        "on": True,
        "brightness": 120,
        "xy": [0.5, 0.4],
        "ct_mired": None,
        "color_mode": None,
    }
    assert result.unmapped_entities["sensor.somebody_elses_thing"] == 1
    assert result.records[1].mono == 45.0
    assert result.records[3].path == "fixtures.den_pendant_1"
    assert result.records[3].data == {"on": True, "brightness": 163, "transition_s": 5.0}
    assert result.unmapped_topics["z2m-1/den_plate/set"] == 1
    assert result.records[4].path == "sensors.den_radar_zone_1" and result.records[4].value is False
    assert result.records[5].path == "sleep" and result.records[5].value is None
    out = tmp_path / "t.jsonl"
    assert write_trace(out, result.records) == 6
    assert list(read_trace(out)) == result.records
