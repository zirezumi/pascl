"""Assemble a recorded window of the reference installation under its private binding.

Needs ``PASCL_PRIVATE_MODELS`` (the model and binding) and ``PASCL_PRIVATE_TRACES`` (a directory
holding ``*.jsonl`` exports from the observation tap). Skipped otherwise, as in CI. The test does
not judge the render; it checks the binding places the render's inputs and the wire outputs,
and prints what it could not place so the input-vector audit has a list to work from.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pascl.harness.assemble import assemble
from pascl.harness.binding import load_binding
from pascl.model import load

MODEL_DIR = os.environ.get("PASCL_PRIVATE_MODELS")
TRACE_DIR = os.environ.get("PASCL_PRIVATE_TRACES")
READY = bool(
    MODEL_DIR
    and TRACE_DIR
    and Path(MODEL_DIR, "loft_v0.yaml").is_file()
    and Path(MODEL_DIR, "loft_binding.yaml").is_file()
)
RAWS = sorted(Path(TRACE_DIR).glob("*.jsonl")) if READY and TRACE_DIR else []

pytestmark = pytest.mark.skipif(not RAWS, reason="private model, binding or traces not available")


@pytest.mark.parametrize("raw", RAWS, ids=[p.stem for p in RAWS])
def test_private_export_assembles_with_inputs_and_outputs(raw: Path) -> None:
    assert MODEL_DIR is not None
    model = load(Path(MODEL_DIR, "loft_v0.yaml").read_text(encoding="utf-8"))
    binding = load_binding(Path(MODEL_DIR, "loft_binding.yaml").read_text(encoding="utf-8"))
    with raw.open(encoding="utf-8") as f:
        result = assemble(model, binding, f)
    kinds = result.kinds
    print("\nkinds:", dict(kinds))
    print("unmapped entities (top 25):", result.unmapped_entities.most_common(25))
    print("unmapped topics (top 10):", result.unmapped_topics.most_common(10))
    assert kinds["input"] > 0
    assert kinds["output"] > 0
    paths = {r.path for r in result.records if r.kind in ("input", "latent")}
    assert any(p.startswith("solar.") for p in paths)
    assert any(p.startswith("fixtures.") for p in paths)
