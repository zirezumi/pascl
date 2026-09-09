"""Schema v0 against the private reference-home encodings.

Point ``PASCL_PRIVATE_MODELS`` at a directory of ``*.yaml`` Home Models kept outside this
repository (they carry the home's coordinates and topology). Each must load, validate and round
trip losslessly. Skipped, as in CI, when the variable is unset.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pascl.model import consumer_envelopes, dumps, from_dict, load, loads, to_dict, validate

MODEL_DIR = os.environ.get("PASCL_PRIVATE_MODELS")


def _is_model(path: Path) -> bool:
    with path.open(encoding="utf-8") as f:
        return any(line.startswith("home_model:") for line in f)


FILES = sorted(p for p in Path(MODEL_DIR).glob("*.yaml") if _is_model(p)) if MODEL_DIR else []

pytestmark = pytest.mark.skipif(not FILES, reason="PASCL_PRIVATE_MODELS not set or holds no *.yaml")


@pytest.mark.parametrize("path", FILES, ids=[p.stem for p in FILES])
def test_private_model_loads_validates_and_round_trips(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    model = load(text)
    assert validate(model) == []
    assert from_dict(to_dict(model)) == model
    assert loads(dumps(model)) == model
    assert model.rooms
    env = consumer_envelopes(model)
    assert set(model.rooms) <= set(env)
