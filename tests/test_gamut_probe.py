"""The gamut measurement protocol against simulated devices, and the choice of what to measure.

A simulated device clips every command to a hidden polygon, the way a real one does, and the
protocol must recover that polygon from the answers alone. Devices that answer nothing but the
transport's echo, or always the same point, must come back unmeasured rather than as a wrong
polygon.
"""

from __future__ import annotations

from collections.abc import Callable
from itertools import pairwise
from pathlib import Path

import pytest

from pascl.core.gamut import (
    XY,
    Polygon,
    clip,
    inside,
    project,
    rgb_clamp,
    signed_area,
    toward_white,
)
from pascl.estimator.gamut import (
    FAR_POINTS,
    SEED_TOL,
    Probe,
    confirm_seed,
    consistency,
    fit_clip_rule,
    inherit,
    is_echo,
    outward_steps,
    pick_target,
    seeds,
    statuses,
)
from pascl.model import Gamut, ModelError, dumps, load, validate, with_fixture_gamut

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "demo_home.yaml"

TRIANGLE: Polygon = ((0.153185, 0.047547), (0.691493, 0.308293), (0.169986, 0.699992))
#: A four-primary emitter with a deep purple primary between blue and red (convex, CCW). No far
#: point reaches that corner, so the first hull cuts it and the refinement has to find it.
QUAD: Polygon = ((0.1532, 0.0475), (0.40, 0.12), (0.6915, 0.3083), (0.17, 0.70))
#: A four-primary emitter whose amber corner barely leaves the red-green chord.
SHALLOW: Polygon = ((0.1532, 0.0475), (0.6915, 0.3083), (0.56, 0.42), (0.17, 0.70))


def run(device: Callable[[XY], XY | None]) -> Probe:
    probe = Probe()
    while (step := probe.next()) is not None:
        probe.answer(step, device(step.xy))
    return probe


def clipper(poly: Polygon, decimals: int | None = None) -> Callable[[XY], XY]:
    def device(xy: XY) -> XY:
        px, py = project(xy, poly)
        return (px, py) if decimals is None else (round(px, decimals), round(py, decimals))

    return device


def test_far_points_are_valid_and_alternate_regions() -> None:
    for x, y in FAR_POINTS:
        assert x >= 0.02 and y >= 0.02 and x + y <= 0.99
    # Consecutive far points must clip to different places on a three-primary gamut, or the
    # second would change no attribute and a report-only device would never answer it.
    clips = [project(p, TRIANGLE) for p in FAR_POINTS]
    for a, b in pairwise(clips):
        assert abs(a[0] - b[0]) + abs(a[1] - b[1]) > 0.02


def test_triangle_is_recovered_exactly() -> None:
    probe = run(clipper(TRIANGLE))
    v = probe.verdict()
    assert v.polygon is not None and len(v.polygon) == 3
    for hidden in TRIANGLE:
        assert min(abs(hidden[0] - h[0]) + abs(hidden[1] - h[1]) for h in v.polygon) < 1e-5
    assert v.model_error is not None and v.model_error < 1e-5
    assert v.unanswered == 0
    assert v.device_reports == len(FAR_POINTS) + 2 * 3
    assert signed_area(v.polygon) > 0


def test_triangle_survives_a_transport_that_rounds_to_four_decimals() -> None:
    v = run(clipper(TRIANGLE, decimals=4)).verdict()
    assert v.polygon is not None and len(v.polygon) == 3
    assert v.model_error is not None and v.model_error < 2e-4


def test_multi_primary_device_is_measured_as_a_larger_hull() -> None:
    v = run(clipper(QUAD)).verdict()
    assert v.polygon is not None
    # No far point clips to the purple corner, so the first hull cuts it with a chord; the edge
    # probe outside that chord lands well beyond it, a vertex probe goes out along the chord's
    # normal into the corner's wedge, and the true vertex comes back exactly.
    assert len(v.polygon) == 4
    for hidden in QUAD:
        assert min(abs(hidden[0] - h[0]) + abs(hidden[1] - h[1]) for h in v.polygon) < 1e-5
    assert signed_area(v.polygon) > 0.999 * signed_area(QUAD)
    assert v.model_error is not None and v.model_error < 1e-5
    assert not any("bulge" in n for n in v.notes)


def test_shallow_corner_is_within_the_comparators_tolerance() -> None:
    v = run(clipper(SHALLOW)).verdict()
    assert v.polygon is not None
    # Every recovered vertex is a point the device really reaches...
    for h in v.polygon:
        assert inside(h, SHALLOW)
        p = project(h, SHALLOW)
        assert abs(p[0] - h[0]) + abs(p[1] - h[1]) < 1e-9
    # ...and wherever the hull cuts the shallow corner it stays inside the clip-model limit,
    # which is what the drift comparators need, not the vertex itself.
    assert signed_area(v.polygon) > 0.995 * signed_area(SHALLOW)
    assert v.model_error is not None and v.model_error < 0.002


def test_echo_only_device_measures_nothing() -> None:
    v = run(lambda xy: xy).verdict()
    assert v.polygon is None
    assert v.device_reports == 0
    assert v.unanswered == len(FAR_POINTS)
    assert any("echo" in n for n in v.notes)


def test_silent_device_measures_nothing() -> None:
    v = run(lambda _xy: None).verdict()
    assert v.polygon is None
    assert v.unanswered == len(FAR_POINTS)


def test_white_only_device_measures_nothing() -> None:
    v = run(lambda _xy: (0.45, 0.41)).verdict()
    assert v.polygon is None
    assert any("degenerate" in n for n in v.notes)


def test_is_echo_and_outward_steps() -> None:
    assert is_echo((0.95, 0.04), (0.9502, 0.0399))
    assert not is_echo((0.95, 0.04), (0.6915, 0.3083))
    steps = outward_steps(TRIANGLE)
    assert [s.kind for s in steps] == ["vertex"] * 3 + ["edge"] * 3
    for s in steps:
        assert not inside(s.xy, TRIANGLE)


def test_statuses_and_pick_target() -> None:
    model = load(EXAMPLE.read_text(encoding="utf-8"))
    st = {s.fixture: s.status for s in statuses(model)}
    # only xy-capable fixtures are on the list, and none is measured yet
    assert "pantry_light" not in st
    assert st and set(st.values()) == {"unmeasured"}
    # a measured fixture bound to its device stays measured; a replaced device is rebound
    g = Gamut(TRIANGLE, bound_to="dev-1", measured="2026-09-16T21:46:14+00:00", model_error=2e-5)
    m2 = with_fixture_gamut(model, "den_strip", g)
    assert {s.fixture: s.status for s in statuses(m2, {"den_strip": "dev-1"})}[
        "den_strip"
    ] == "measured"
    assert {s.fixture: s.status for s in statuses(m2, {"den_strip": "dev-2"})}[
        "den_strip"
    ] == "rebound"
    # the pick skips lit fixtures and occupied rooms, and prefers unmeasured over rebound
    m3 = with_fixture_gamut(m2, "den_pendant_1", g)
    m3 = with_fixture_gamut(m3, "den_pendant_2", g)
    ids = {"den_strip": "dev-2", "den_pendant_1": "dev-1", "den_pendant_2": "dev-1"}
    pick = pick_target(m3, ids, lit=set(), occupied_rooms=set())
    assert pick is not None and pick.status == "rebound" and pick.fixture == "den_strip"
    assert pick_target(m3, ids, lit={"den_strip"}, occupied_rooms=set()) is None
    assert pick_target(m3, ids, lit=set(), occupied_rooms={pick.room}) is None
    pick = pick_target(model, None, lit=set(), occupied_rooms=set())
    assert pick is not None and pick.status == "unmeasured"


# ---------------------------------------------------------------------------------------------
# The clip rule is measured, not assumed
# ---------------------------------------------------------------------------------------------


def test_closest_point_device_is_fitted_as_closest() -> None:
    v = run(clipper(TRIANGLE)).verdict()
    assert v.clip_rule == "closest"
    assert v.fits[0].rule == "closest" and v.fits[0].error < 1e-5 and v.fits[0].outliers == 0
    # the other rules are on the table, and clearly worse
    assert {f.rule for f in v.fits} == {"closest", "toward_white", "rgb_clamp"}
    assert all(f.error > 0.01 for f in v.fits if f.rule != "closest")
    assert len(v.answers) == v.device_reports
    assert all(s.kind in ("far", "vertex", "edge") for s, _ in v.answers)


def test_desaturating_device_is_fitted_as_toward_white() -> None:
    v = run(lambda xy: toward_white(xy, TRIANGLE)).verdict()
    assert v.polygon is not None
    assert v.clip_rule == "toward_white", v.fits
    assert v.model_error is not None and v.model_error < 0.002
    assert any("toward_white" in n for n in v.notes)
    # every recovered vertex is a point the device really reaches (on the true boundary, to
    # the hull's own 6 dp rounding)
    for h in v.polygon:
        p = project(h, TRIANGLE)
        assert abs(p[0] - h[0]) + abs(p[1] - h[1]) < 1e-5
    # and the comparators, told the rule, predict where a palette entry lands
    for cmd in ((0.481, 0.199), (0.95, 0.04), (0.02, 0.70)):
        shown = toward_white(cmd, TRIANGLE)
        predicted = clip(cmd, v.polygon, v.clip_rule)
        assert abs(shown[0] - predicted[0]) < 0.003 and abs(shown[1] - predicted[1]) < 0.003


def test_primary_clamping_device_is_fitted_as_rgb_clamp() -> None:
    v = run(lambda xy: rgb_clamp(xy, TRIANGLE)).verdict()
    assert v.polygon is not None and len(v.polygon) == 3
    assert v.clip_rule == "rgb_clamp", v.fits
    assert v.model_error is not None and v.model_error < 1e-4
    for hidden in TRIANGLE:
        assert min(abs(hidden[0] - h[0]) + abs(hidden[1] - h[1]) for h in v.polygon) < 1e-5


def test_fit_clip_rule_ties_go_to_closest_and_skip_rgb_clamp_off_a_triangle() -> None:
    fits = fit_clip_rule([], TRIANGLE)
    assert (fits[0].rule, fits[0].error, fits[0].outliers) == ("closest", 0.0, 0)
    assert [f.rule for f in fit_clip_rule([], QUAD)] == ["closest", "toward_white"]
    # one answer no rule accounts for (a stale report of an earlier command, on the boundary)
    # is set aside rather than allowed to pick the rule
    device = clipper(TRIANGLE)
    answers = [(p, device(p)) for p in FAR_POINTS]
    answers[6] = (FAR_POINTS[6], device(FAR_POINTS[5]))
    fits = fit_clip_rule(answers, TRIANGLE)
    assert fits[0].rule == "closest" and fits[0].error < 1e-9 and fits[0].outliers == 1


def test_stale_and_foreign_answers_are_not_evidence() -> None:
    """Two things the reference installation's records contain: the previous colour reported
    late (taken as the next probe's answer) and a colour a render put on the device during a
    sample. Neither may shape the fit, and the polygon comes out the same."""
    device = clipper(TRIANGLE)
    white = (0.5245, 0.4136)
    probe = Probe()
    n = 0
    while (step := probe.next()) is not None:
        n += 1
        if n == 4:
            probe.answer(step, device(FAR_POINTS[2]))  # the previous answer again
        elif n == 9:
            probe.answer(step, white)  # a warm white from a group render
        else:
            probe.answer(step, device(step.xy))
    v = probe.verdict()
    assert v.polygon is not None and len(v.polygon) == 3
    for hidden in TRIANGLE:
        assert min(abs(hidden[0] - h[0]) + abs(hidden[1] - h[1]) for h in v.polygon) < 1e-5
    assert v.clip_rule == "closest" and v.model_error is not None and v.model_error < 1e-5
    assert any("stale" in note for note in v.notes)
    assert any("inside the hull ignored" in note for note in v.notes)
    # a trusted read-back may repeat the previous answer (two probes that really clip to the
    # same place) and is kept
    probe = Probe()
    step = probe.next()
    assert step is not None
    probe.answer(step, (0.6915, 0.3083))
    step = probe.next()
    assert step is not None
    probe.answer(step, (0.6915, 0.3083), trusted=True)
    assert len(probe.verdict().answers) == 2


# ---------------------------------------------------------------------------------------------
# Seeds: a measurement travels to same-model fixtures with its provenance
# ---------------------------------------------------------------------------------------------

G = Gamut(TRIANGLE, bound_to="dev-1", measured="2026-09-17T14:00:00+00:00", model_error=2e-5)


def test_seeds_and_inherit_follow_the_material_model_label() -> None:
    model = load(EXAMPLE.read_text(encoding="utf-8"))
    m2 = with_fixture_gamut(model, "den_pendant_1", G)
    # den_pendant_2 shares color_bulb (one label); den_strip is color_strip (another): only the
    # pendant is seeded, the strip stays unmeasured
    planned = seeds(m2)
    assert [(s.fixture, s.source, s.refresh) for s in planned] == [
        ("den_pendant_2", "den_pendant_1", False)
    ]
    m3, applied = inherit(m2, {"den_pendant_2": "dev-2"})
    assert applied == planned
    assert validate(m3) == []
    st = {s.fixture: s.status for s in statuses(m3)}
    assert st == {
        "den_pendant_1": "measured",
        "den_pendant_2": "inherited",
        "den_strip": "unmeasured",
    }
    g2 = m3.rooms["den"].fixtures["den_pendant_2"].gamut
    assert g2 is not None
    assert g2.vertices == TRIANGLE and g2.inherited_from == "den_pendant_1"
    assert g2.bound_to == "dev-2" and g2.measured == G.measured and g2.firmware is None
    # idempotent
    assert inherit(m3, {"den_pendant_2": "dev-2"}) == (m3, [])
    # the seed is measured last: an unmeasured fixture goes first, and a seed is not "measured"
    pick = pick_target(m3, None, lit=set(), occupied_rooms=set())
    assert pick is not None and pick.fixture == "den_strip"
    m4 = with_fixture_gamut(m3, "den_strip", G)
    pick = pick_target(m4, None, lit=set(), occupied_rooms=set())
    assert pick is not None and pick.fixture == "den_pendant_2" and pick.status == "inherited"


def test_seed_refreshes_when_its_source_is_remeasured() -> None:
    model = load(EXAMPLE.read_text(encoding="utf-8"))
    m2, _ = inherit(with_fixture_gamut(model, "den_pendant_1", G))
    moved = Gamut(QUAD, bound_to="dev-1", measured="2026-09-18T14:00:00+00:00")
    m3 = with_fixture_gamut(m2, "den_pendant_1", moved)
    assert any("no longer matches" in n for n in consistency(m3))
    planned = seeds(m3)
    assert [(s.fixture, s.refresh) for s in planned] == [("den_pendant_2", True)]
    m4, _ = inherit(m3)
    g2 = m4.rooms["den"].fixtures["den_pendant_2"].gamut
    assert g2 is not None and g2.vertices == QUAD and g2.inherited_from == "den_pendant_1"
    assert consistency(m4) == []


def test_confirm_seed_and_consistency_notes() -> None:
    agreed, gap = confirm_seed(G, TRIANGLE)
    assert agreed and gap == 0.0
    nudged = ((0.153185, 0.047547), (0.691493 + 2 * SEED_TOL, 0.308293), (0.169986, 0.699992))
    agreed, gap = confirm_seed(G, nudged)
    assert not agreed and abs(gap - 2 * SEED_TOL) < 1e-9
    model = load(EXAMPLE.read_text(encoding="utf-8"))
    m2 = with_fixture_gamut(model, "den_pendant_1", G)
    # two measured units of one label 0.01 apart: a note, not a model error
    other = Gamut(
        ((0.153185, 0.047547), (0.691493, 0.308293), (0.179986, 0.699992)), bound_to="dev-2"
    )
    m3 = with_fixture_gamut(m2, "den_pendant_2", other)
    assert validate(m3) == []
    notes = consistency(m3)
    assert len(notes) == 1 and "worth a look" in notes[0]
    # 0.05 apart: the model refuses it
    far = Gamut(
        ((0.153185, 0.047547), (0.691493, 0.308293), (0.219986, 0.699992)), bound_to="dev-2"
    )
    m4 = with_fixture_gamut(m2, "den_pendant_2", far)
    assert any("limit" in e for e in validate(m4))
    with pytest.raises(ModelError):
        load(dumps(m4))
