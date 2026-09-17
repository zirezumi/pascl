"""Composite spaces: membership, the shared vacancy timer, and what the validator refuses."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from pascl.model import from_dict, space_rooms, to_dict, validate

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "demo_home.yaml"


@pytest.fixture
def raw() -> dict[str, Any]:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_membership_is_declared_by_the_room_and_round_trips(raw: dict[str, Any]) -> None:
    model = from_dict(raw)
    assert validate(model) == []
    assert model.spaces["house"].vacancy_s == 600
    assert model.rooms["den"].space == "house"
    assert model.rooms["den"].vacancy.scope == "space"
    assert model.rooms["den"].vacancy.seconds is None
    assert model.rooms["pantry"].space is None
    assert space_rooms(model, "house") == ("den",)
    assert from_dict(to_dict(model)) == model


def test_a_room_cannot_use_a_space_timer_without_a_space(raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    bad["home_model"]["rooms"]["pantry"]["vacancy"] = {"scope": "space"}
    errs = validate(from_dict(bad))
    assert any("scope 'space' but the room is in none" in e for e in errs)


def test_seconds_belong_to_the_space_when_shared(raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    bad["home_model"]["rooms"]["den"]["vacancy"] = {"scope": "space", "seconds": 30}
    errs = validate(from_dict(bad))
    assert any("seconds belong to the space" in e for e in errs)


def test_a_member_may_keep_its_own_timer(raw: dict[str, Any]) -> None:
    good = copy.deepcopy(raw)
    good["home_model"]["rooms"]["den"]["vacancy"] = {"scope": "room", "seconds": 30}
    model = from_dict(good)
    assert validate(model) == []
    assert model.rooms["den"].space == "house"


def test_an_undeclared_or_empty_space_is_an_error(raw: dict[str, Any]) -> None:
    bad = copy.deepcopy(raw)
    bad["home_model"]["rooms"]["den"]["space"] = "attic"
    bad["home_model"]["spaces"]["annex"] = {"vacancy_s": 120}
    errs = validate(from_dict(bad))
    assert any("space 'attic' is not declared" in e for e in errs)
    assert any("spaces.annex: no room declares this space" in e for e in errs)


def test_a_home_without_spaces_is_ordinary(raw: dict[str, Any]) -> None:
    solo = copy.deepcopy(raw)
    del solo["home_model"]["spaces"]
    del solo["home_model"]["rooms"]["den"]["space"]
    solo["home_model"]["rooms"]["den"]["vacancy"] = {"scope": "room", "seconds": 600}
    model = from_dict(solo)
    assert validate(model) == []
    assert model.spaces == {}
