"""The measurement protocol against the reference installation's recorded probe answers.

Point ``PASCL_PRIVATE_GAMUT`` at the private ``measured.json`` the reference installation's
probe writes (every fixture with every commanded colour and the device's answer). For each
measured fixture the protocol is replayed with a device that answers exactly what the real one
did, and must arrive at the same polygon. Skipped, as in CI, when the variable is unset.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pascl.core.gamut import XY, polygon_problems
from pascl.estimator.gamut import Probe

SOURCE = os.environ.get("PASCL_PRIVATE_GAMUT")
DATA = json.loads(Path(SOURCE).read_text(encoding="utf-8")) if SOURCE else {}
FIXTURES = sorted(
    f
    for f, e in DATA.get("fixtures", {}).items()
    if len(e.get("hull") or []) >= 3 and not e.get("verbatim")
)

pytestmark = pytest.mark.skipif(
    not FIXTURES, reason="PASCL_PRIVATE_GAMUT not set or holds no measured fixture"
)


@pytest.mark.parametrize("fixture", FIXTURES)
def test_recorded_answers_reproduce_the_recorded_polygon(fixture: str) -> None:
    entry = DATA["fixtures"][fixture]
    answers: dict[tuple[float, float], XY] = {}
    for p in entry["probes"]:
        answers[(round(p["cmd"][0], 4), round(p["cmd"][1], 4))] = (p["report"][0], p["report"][1])

    def device(xy: XY) -> XY | None:
        return answers.get((round(xy[0], 4), round(xy[1], 4)))

    probe = Probe()
    while (step := probe.next()) is not None:
        probe.answer(step, device(step.xy))
    v = probe.verdict()
    assert v.polygon is not None, v.notes
    assert polygon_problems(v.polygon) == []
    recorded = [tuple(x) for x in entry["hull"]]
    assert len(v.polygon) == len(recorded)
    for r in recorded:
        assert min(abs(r[0] - h[0]) + abs(r[1] - h[1]) for h in v.polygon) < 1e-5
    # every fixture in the reference fleet clips to the nearest point, whatever stale or
    # foreign answers the recording tool let through
    assert v.clip_rule == "closest", v.fits
    if entry.get("model_err_max") is not None:
        assert v.model_error is not None
        # the engine's figure sets aside a stale report or a colour from elsewhere during a
        # sample, which the recording tool's did not: never worse, sometimes better
        assert v.model_error <= entry["model_err_max"] + 1e-4
        assert v.model_error < 0.002
