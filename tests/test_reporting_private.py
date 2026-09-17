"""The report-cadence estimator against the reference installation's recorded fades.

Point ``PASCL_PRIVATE_REPORTING`` at a directory of extracts written by the reference
installation's derivation tool (``report_cadence_derive.py --extract``): every colour fade in a
window with the fixture's device report times, every trip the drift sensor would have lost,
and the model label per fixture. Skipped, as in CI, when the variable is unset.

What must hold on that data is what the design was verified on (a 30 s crossfade every 60 s,
sensors armed at 30 s): every Hue unit reports a moving colour at ~10 s, the phase prediction
puts the settling report within a fraction of a second, the delivery jitter is a few seconds
at the 99th percentile, and arming at prediction plus jitter loses no more than a percent of
the trips while acting on a stuck fixture well inside the linger.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pascl.estimator.reporting import (
    Fade,
    Trip,
    estimate,
    inherit,
    jitter,
    replay,
    residuals,
    stands_down,
)

SOURCE = os.environ.get("PASCL_PRIVATE_REPORTING")
EXTRACTS = sorted(Path(SOURCE).glob("*.json")) if SOURCE else []

pytestmark = pytest.mark.skipif(not EXTRACTS, reason="PASCL_PRIVATE_REPORTING not set or empty")


def load(path: Path) -> tuple[list[Fade], list[Trip], dict[str, str | None], float, float]:
    ex = json.loads(path.read_text(encoding="utf-8"))
    fades = [Fade(f["fixture"], f["transition_s"], tuple(f["report_times"])) for f in ex["fades"]]
    trips = [Trip(**t) for t in ex["trips"]]
    return fades, trips, ex["labels"], float(ex["transition_s"]), float(ex["linger_s"])


@pytest.mark.parametrize("path", EXTRACTS, ids=lambda p: p.stem)
def test_every_measured_unit_reports_at_the_hue_cadence(path: Path) -> None:
    fades, _trips, _labels, _t, _l = load(path)
    measured = {f: r for f, r in estimate(fades).items() if r.source == "measured"}
    assert len(measured) >= 20, "the extract should measure a good part of the scening fleet"
    for r in measured.values():
        assert r.seconds is not None
        assert 9.5 <= r.seconds <= 10.5, (r.fixture, r.seconds)
        assert r.on_cadence is not None and r.on_cadence >= 0.95, (r.fixture, r.on_cadence)
        assert r.p90_s is not None and r.p90_s - r.seconds < 0.1, (r.fixture, r.p90_s)


@pytest.mark.parametrize("path", EXTRACTS, ids=lambda p: p.stem)
def test_seeds_travel_along_the_model_label_and_never_disagree(path: Path) -> None:
    fades, _trips, labels, _t, _l = load(path)
    measured = estimate(fades)
    got = inherit(measured, labels, sorted(labels))
    inherited = [r for r in got.values() if r.source == "inherited"]
    assert inherited, "some unmeasured fixture shares a label with a measured one"
    for r in inherited:
        assert r.inherited_from in measured
        assert r.seconds == pytest.approx(measured[r.inherited_from].seconds, abs=0.05)
    assert not [r for r in got.values() if r.note and "disagree" in r.note]
    # a fixture that reports no colour at all is declined by name, never seeded silently
    for f, r in got.items():
        if r.source == "none":
            assert r.note, f


@pytest.mark.parametrize("path", EXTRACTS, ids=lambda p: p.stem)
def test_phase_prediction_and_jitter_reproduce_the_design_note(path: Path) -> None:
    fades, trips, labels, transition, linger = load(path)
    intervals = inherit(estimate(fades), labels, sorted(labels))
    assert len(trips) >= 100
    res = sorted(residuals(trips, intervals))
    # the prediction is essentially exact: the median residual is nil and the 90th a tenth
    assert res[len(res) // 2] <= 0.05
    assert res[int(0.9 * len(res))] <= 0.3
    j = jitter(trips, intervals)
    assert j.seconds is not None
    assert 1.0 <= j.seconds <= 6.0, j
    played = replay(trips, intervals, j.seconds)
    assert played.false_arms <= max(1, len(trips) // 100)
    assert played.wait_p50_s <= 6.0
    assert played.arm_max_s < linger - 1.0
    # no margin at all loses most trips: the wait is what removes the class
    bare = replay(trips, intervals, None)
    assert bare.false_arms > played.false_arms
    # today's helpers sit inside the detection regime for every fixture
    for f, r in intervals.items():
        assert not stands_down(transition, linger, r.seconds, j.seconds), f
