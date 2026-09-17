"""The measured gamut in the Home Model: strict, validated, lossless."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from pascl.model import (
    Gamut,
    ModelError,
    dumps,
    fixture_clip,
    fixture_gamut,
    fixture_model_label,
    from_dict,
    load,
    loads,
    to_dict,
    validate,
    with_fixture_gamut,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "demo_home.yaml"
TRIANGLE = [[0.153185, 0.047547], [0.691493, 0.308293], [0.169986, 0.699992]]


@pytest.fixture
def raw() -> dict[str, Any]:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _fixture(raw: dict[str, Any], room: str, fid: str) -> dict[str, Any]:
    return raw["home_model"]["rooms"][room]["fixtures"][fid]  # type: ignore[no-any-return]


def test_gamut_round_trips_and_resolves(raw: dict[str, Any]) -> None:
    _fixture(raw, "den", "den_strip")["gamut"] = {
        "vertices": TRIANGLE,
        "bound_to": "0x0017880100aa0001_light",
        "measured": "2026-09-16T21:46:14+00:00",
        "model_error": 2e-05,
    }
    model = from_dict(raw)
    assert validate(model) == []
    assert from_dict(to_dict(model)) == model
    assert loads(dumps(model)) == model
    assert fixture_gamut(model, "den_strip") == tuple(tuple(v) for v in TRIANGLE)
    assert fixture_gamut(model, "den_pendant_1") == ()
    assert fixture_gamut(model, "no_such_fixture") == ()
    # the key is emitted only when a gamut is set, so a model without one is byte-stable
    assert "gamut" not in to_dict(model)["home_model"]["rooms"]["den"]["fixtures"]["den_pendant_1"]
    # the fields a record may omit read as their defaults and are written out explicitly
    g = model.rooms["den"].fixtures["den_strip"].gamut
    assert g is not None and g.clip_rule == "closest" and g.inherited_from is None
    assert g.firmware is None
    assert fixture_clip(model, "den_strip") == (g.vertices, "closest")
    assert fixture_clip(model, "den_pendant_1") == ((), "closest")
    written = to_dict(model)["home_model"]["rooms"]["den"]["fixtures"]["den_strip"]["gamut"]
    assert (
        written["clip_rule"] == "closest" and "inherited_from" in written and "firmware" in written
    )
    assert fixture_model_label(model, "den_strip") == "demo strip"
    assert fixture_model_label(model, "pantry_light") is None
    assert fixture_model_label(model, "no_such_fixture") is None


def test_full_record_round_trips(raw: dict[str, Any]) -> None:
    _fixture(raw, "den", "den_pendant_1")["gamut"] = {
        "vertices": TRIANGLE,
        "bound_to": "0x0017880100aa0001",
        "measured": "2026-09-16T21:46:14+00:00",
        "model_error": 0.0014,
        "clip_rule": "toward_white",
        "firmware": "1.116.3",
    }
    _fixture(raw, "den", "den_pendant_2")["gamut"] = {
        "vertices": TRIANGLE,
        "bound_to": "0x001788010ef8f722",
        "measured": "2026-09-16T21:46:14+00:00",
        "model_error": 0.0014,
        "clip_rule": "toward_white",
        "inherited_from": "den_pendant_1",
    }
    model = from_dict(raw)
    assert validate(model) == []
    assert from_dict(to_dict(model)) == model
    assert loads(dumps(model)) == model
    assert fixture_clip(model, "den_pendant_2") == (
        tuple(tuple(v) for v in TRIANGLE),
        "toward_white",
    )


def test_gamut_is_strict(raw: dict[str, Any]) -> None:
    _fixture(raw, "den", "den_strip")["gamut"] = {"vertices": TRIANGLE, "brand": "hue"}
    with pytest.raises(ModelError) as e:
        from_dict(raw)
    assert any("unknown key" in msg for msg in e.value.errors)


QUAD = [[0.1532, 0.0475], [0.40, 0.12], [0.6915, 0.3083], [0.17, 0.70]]


@pytest.mark.parametrize(
    ("gamut", "fragment"),
    [
        ({"vertices": list(reversed(TRIANGLE))}, "clockwise"),
        ({"vertices": [[0.7, 0.3], [0.4, 0.4], [0.2, 0.7], [0.15, 0.05]]}, "convex"),
        ({"vertices": TRIANGLE[:2]}, "vertices"),
        ({"vertices": TRIANGLE, "model_error": -1.0}, "non-negative"),
        ({"vertices": TRIANGLE, "clip_rule": "nearest"}, "expected one of"),
        ({"vertices": QUAD, "clip_rule": "rgb_clamp"}, "three-primary"),
        ({"vertices": TRIANGLE, "inherited_from": "den_strip"}, "inherited from itself"),
        ({"vertices": TRIANGLE, "inherited_from": "no_such"}, "not a measured fixture"),
        ({"vertices": TRIANGLE, "inherited_from": "den_pendant_1"}, "not a measured fixture"),
    ],
)
def test_bad_gamuts_are_model_errors(
    raw: dict[str, Any], gamut: dict[str, Any], fragment: str
) -> None:
    _fixture(raw, "den", "den_strip")["gamut"] = gamut
    with pytest.raises(ModelError) as e:
        load(yaml.safe_dump(raw))
    assert any(fragment in msg for msg in e.value.errors)


def test_inheritance_needs_the_same_model_label(raw: dict[str, Any]) -> None:
    # den_pendant_1 is a colour bulb, den_strip a colour strip: different labels
    _fixture(raw, "den", "den_pendant_1")["gamut"] = {"vertices": TRIANGLE}
    _fixture(raw, "den", "den_strip")["gamut"] = {
        "vertices": TRIANGLE,
        "inherited_from": "den_pendant_1",
    }
    with pytest.raises(ModelError) as e:
        load(yaml.safe_dump(raw))
    assert any("same material model label" in msg for msg in e.value.errors)
    # a copy of a copy is refused too: provenance points at a measurement
    _fixture(raw, "den", "den_strip")["gamut"] = {"vertices": TRIANGLE}
    _fixture(raw, "den", "den_pendant_2")["gamut"] = {
        "vertices": TRIANGLE,
        "inherited_from": "den_pendant_1",
    }
    assert validate(from_dict(raw)) == []
    _fixture(raw, "den", "den_pendant_1")["gamut"]["inherited_from"] = "den_pendant_2"
    with pytest.raises(ModelError):
        load(yaml.safe_dump(raw))


def test_same_label_units_must_agree(raw: dict[str, Any]) -> None:
    _fixture(raw, "den", "den_pendant_1")["gamut"] = {"vertices": TRIANGLE}
    far = [[0.153185, 0.047547], [0.691493, 0.308293], [0.219986, 0.699992]]
    _fixture(raw, "den", "den_pendant_2")["gamut"] = {"vertices": far}
    with pytest.raises(ModelError) as e:
        load(yaml.safe_dump(raw))
    assert any("limit 0.02" in msg for msg in e.value.errors)
    # a strip of another label may differ freely
    _fixture(raw, "den", "den_pendant_2")["gamut"] = {"vertices": TRIANGLE}
    _fixture(raw, "den", "den_strip")["gamut"] = {"vertices": far}
    assert validate(from_dict(raw)) == []


def test_gamut_needs_an_xy_material(raw: dict[str, Any]) -> None:
    _fixture(raw, "pantry", "pantry_light")["gamut"] = {"vertices": TRIANGLE}
    with pytest.raises(ModelError) as e:
        load(yaml.safe_dump(raw))
    assert any("xy capability" in msg for msg in e.value.errors)


def test_with_fixture_gamut_is_a_functional_update() -> None:
    model = load(EXAMPLE.read_text(encoding="utf-8"))
    g = Gamut(tuple(tuple(v) for v in TRIANGLE), bound_to="dev", measured=None, model_error=None)
    m2 = with_fixture_gamut(model, "den_strip", g)
    assert fixture_gamut(model, "den_strip") == ()
    assert fixture_gamut(m2, "den_strip") == g.vertices
    assert validate(m2) == []
    assert from_dict(to_dict(m2)) == m2
    assert with_fixture_gamut(m2, "den_strip", None) == model
    with pytest.raises(KeyError):
        with_fixture_gamut(model, "no_such_fixture", g)
    deep = copy.deepcopy(to_dict(m2))
    assert deep["home_model"]["rooms"]["den"]["fixtures"]["den_strip"]["gamut"]["bound_to"] == "dev"
