"""Replay the reference installation's recorded windows under its private binding.

Needs ``PASCL_PRIVATE_MODELS`` and ``PASCL_PRIVATE_TRACES``; skipped otherwise, as in CI. The
test prints the replay summary and the first mismatches so a run with ``-s`` is the report, and
it asserts only that the comparison ran: the pass rate is judged by the person reading it until
the exclusion list of CONTRACTS section 1 is encoded.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pascl.harness.assemble import assemble
from pascl.harness.binding import load_binding
from pascl.harness.replay import describe, replay
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
@pytest.mark.parametrize("recorded_colour", [True, False], ids=["recorded", "palette"])
def test_private_replay_runs_and_reports(raw: Path, recorded_colour: bool) -> None:
    assert MODEL_DIR is not None
    model = load(Path(MODEL_DIR, "loft_v0.yaml").read_text(encoding="utf-8"))
    binding = load_binding(Path(MODEL_DIR, "loft_binding.yaml").read_text(encoding="utf-8"))
    with raw.open(encoding="utf-8") as f:
        assembled = assemble(model, binding, f)
    result = replay(model, assembled.records, recorded_scene_colour=recorded_colour)
    print(f"\n{raw.stem} ({'recorded' if recorded_colour else 'palette'} colour)")
    print(result.summary())
    for s in result.mismatches[:20]:
        print(describe(s))
    assert result.samples, "no fixture sample was compared"
