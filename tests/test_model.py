"""Schema v0 on the public synthetic home: strictness, references, and the lossless round trip."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from pascl.model import (
    SCHEMA_VERSION,
    ModelError,
    consumer_envelopes,
    dumps,
    from_dict,
    load,
    loads,
    to_dict,
    validate,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "demo_home.yaml"


@pytest.fixture
def raw() -> dict[str, Any]:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_example_loads_and_validates(raw: dict[str, Any]) -> None:
    model = from_dict(raw)
    assert model.version == SCHEMA_VERSION
    assert validate(model) == []
    assert set(model.rooms) == {"den", "pantry"}
    assert model.rooms["den"].fixtures["den_strip"].role == "highlight"
    assert model.materials["color_bulb"].capabilities == frozenset({"dim", "cct", "xy"})


def test_round_trip_is_lossless(raw: dict[str, Any]) -> None:
    model = from_dict(raw)
    again = from_dict(to_dict(model))
    assert again == model
    assert loads(dumps(model)) == model


def test_unknown_keys_are_errors(raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    bad["home_model"]["rooms"]["den"]["fixtures"]["den_strip"]["colour"] = "blue"
    bad["home_model"]["extra"] = 1
    with pytest.raises(ModelError) as exc:
        from_dict(bad)
    joined = "\n".join(exc.value.errors)
    assert "rooms.den.fixtures.den_strip: unknown key(s) ['colour']" in joined
    assert "home_model: unknown key(s) ['extra']" in joined


def test_missing_curve_anchor_is_an_error(raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    del bad["home_model"]["rooms"]["pantry"]["curves"]["ceiling"]["vacant"]["noon"]
    with pytest.raises(ModelError) as exc:
        from_dict(bad)
    assert any("missing anchor(s) ['noon']" in e for e in exc.value.errors)


def test_wrong_version_is_an_error(raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    bad["home_model"]["version"] = 7
    with pytest.raises(ModelError):
        from_dict(bad)


def test_dangling_references_are_reported_together(raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    den = bad["home_model"]["rooms"]["den"]
    den["fixtures"]["den_pendant_1"]["material"] = "unobtainium"
    den["presence"]["sources"].append("ghost_sensor")
    den["ambient_scope"] = "attic"
    bad["home_model"]["scenes"]["parents"]["house"].append("nowhere")
    errs = validate(from_dict(bad))
    assert any("material 'unobtainium'" in e for e in errs)
    assert any("source 'ghost_sensor'" in e for e in errs)
    assert any("ambient_scope 'attic'" in e for e in errs)
    assert any("child 'nowhere'" in e for e in errs)
    assert len(errs) >= 4


def test_pir_latch_needs_its_pir(raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    bad["home_model"]["rooms"]["pantry"]["presence"]["pir"] = None
    errs = validate(from_dict(bad))
    assert any("or_pir_latched needs pir" in e for e in errs)


def test_phantom_group_must_hold_its_surface(raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    bad["home_model"]["rooms"]["den"]["groups"]["den_phantom"]["members"] = ["den_pendant_1"]
    errs = validate(from_dict(bad))
    assert any("not a control surface" in e for e in errs)
    assert any("not a member of its phantom group" in e for e in errs)


def test_lux_sensor_cannot_be_a_presence_source(raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    bad["home_model"]["rooms"]["den"]["presence"]["sources"].append("den_plate_lux")
    errs = validate(from_dict(bad))
    assert any("is a lux sensor" in e for e in errs)


def test_consumer_envelopes_are_derived_from_the_coalescer(raw: dict[str, Any]) -> None:
    model = from_dict(raw)
    env = consumer_envelopes(model)
    assert env["den"] == 100
    assert env["den_switch"] is None
    assert env["pantry"] is None


def test_load_reports_validation_errors_as_model_error(raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    bad["home_model"]["ambient_normalization"]["cap"] = 0.9
    with pytest.raises(ModelError) as exc:
        load(yaml.safe_dump(bad))
    assert any("only adds light" in e for e in exc.value.errors)
