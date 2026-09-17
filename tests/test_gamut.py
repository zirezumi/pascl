"""Gamut geometry against what real fixtures do.

The projection has to reproduce a device's own clip, so the vectors here are measured ones: a
three-primary gamut measured on a colour lightstrip (its vertices are what the device reported
when commanded far beyond each corner), and the clipped values the same family of devices
reported for palette colours that sit outside it. Anything the projection gets wrong here it
would get wrong in the drift comparator.
"""

from __future__ import annotations

import pytest

from pascl.core.gamut import (
    WHITE_D65,
    Polygon,
    clip,
    convex_hull,
    diverged,
    inside,
    polygon_problems,
    project,
    reachable,
    rgb_clamp,
    signed_area,
    toward_white,
)
from pascl.model.geometry import vertex_deviation

#: A measured three-primary gamut (a Hue-class lightstrip), counter-clockwise: blue, red, green.
MEASURED: Polygon = ((0.153185, 0.047547), (0.691493, 0.308293), (0.169986, 0.699992))

#: (commanded, what the device reported) pairs measured on that gamut. The first three are
#: palette entries that churned the reference installation's drift net for days; the rest are
#: the far-outside probe points and their clips from one measurement run.
MEASURED_CLIPS: list[tuple[tuple[float, float], tuple[float, float]]] = [
    ((0.481, 0.199), (0.478111, 0.204929)),
    ((0.532, 0.222), (0.528435, 0.229297)),
    ((0.652, 0.283), (0.649577, 0.287984)),
    ((0.95, 0.04), (0.69149, 0.30829)),
    ((0.04, 0.94), (0.16999, 0.69999)),
    ((0.02, 0.02), (0.15319, 0.04755)),
    ((0.22, 0.02), (0.19649, 0.06851)),
    ((0.35, 0.64), (0.31389, 0.5919)),
    ((0.02, 0.12), (0.15496, 0.1165)),
    ((0.80, 0.02), (0.66625, 0.29607)),
    ((0.60, 0.39), (0.59377, 0.38169)),
    ((0.55, 0.44), (0.53779, 0.42373)),
    ((0.02, 0.40), (0.16217, 0.39634)),
    ((0.50, 0.02), (0.4233, 0.17838)),
    ((0.30, 0.02), (0.2613, 0.0999)),
    ((0.08, 0.25), (0.15834, 0.24799)),
]


@pytest.mark.parametrize(("command", "reported"), MEASURED_CLIPS)
def test_projection_reproduces_the_device_clip(
    command: tuple[float, float], reported: tuple[float, float]
) -> None:
    px, py = project(command, MEASURED)
    assert abs(px - reported[0]) < 1e-4
    assert abs(py - reported[1]) < 1e-4
    assert inside((px, py), MEASURED)


def test_projection_is_the_identity_inside_and_without_a_gamut() -> None:
    assert project((0.434, 0.383), MEASURED) == (0.434, 0.383)
    assert project((0.481, 0.199), ()) == (0.481, 0.199)
    assert project((0.481, 0.199), ((0.1, 0.1), (0.2, 0.2))) == (0.481, 0.199)
    assert reachable((0.5, 0.4), MEASURED) == (0.5, 0.4)


def test_projection_is_winding_agnostic() -> None:
    cw = tuple(reversed(MEASURED))
    for command, _ in MEASURED_CLIPS:
        assert project(command, cw) == pytest.approx(project(command, MEASURED), abs=1e-12)


def test_diverged_judges_against_the_reachable_point() -> None:
    intent = (0.481, 0.199)
    parked = (0.478111, 0.204929)
    # The device can never match the raw intent...
    assert abs(parked[1] - intent[1]) > 0.003
    # ...but it sits on the reachable point, so the comparator is quiet.
    assert not diverged(parked, intent, MEASURED, 0.003)
    # Without a gamut the comparator is the raw compare, and fires.
    assert diverged(parked, intent, (), 0.003)
    # A genuine drift on an out-of-gamut intent still fires.
    assert diverged((0.5, 0.3), intent, MEASURED, 0.003)
    # No reading at all is a divergence.
    assert diverged(None, intent, MEASURED, 0.003)


def test_hull_recovers_the_triangle_from_clips() -> None:
    reports = [r for _c, r in MEASURED_CLIPS]
    hull = convex_hull(reports)
    assert len(hull) == 3
    for v in MEASURED:
        assert min(abs(v[0] - h[0]) + abs(v[1] - h[1]) for h in hull) < 2e-4
    assert signed_area(hull) > 0


def test_hull_drops_duplicates_and_collinear_points() -> None:
    pts = [(0.0, 0.0), (0.5, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0001, 0.0001)]
    assert convex_hull(pts) == ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    assert convex_hull([(0.1, 0.1), (0.1, 0.1)]) == ((0.1, 0.1),)


def test_polygon_problems() -> None:
    assert polygon_problems(MEASURED) == []
    assert any("clockwise" in p for p in polygon_problems(tuple(reversed(MEASURED))))
    assert any(
        "convex" in p for p in polygon_problems(((0.7, 0.3), (0.4, 0.4), (0.2, 0.7), (0.15, 0.05)))
    )
    assert any("vertices" in p for p in polygon_problems(((0.1, 0.1), (0.2, 0.2))))
    assert any("area" in p for p in polygon_problems(((0.3, 0.3), (0.31, 0.3), (0.3, 0.31))))
    assert any("chromaticity" in p for p in polygon_problems(((0.9, 0.9), (0.1, 0.1), (0.5, 0.1))))


# ---------------------------------------------------------------------------------------------
# The other clip rules a device may follow
# ---------------------------------------------------------------------------------------------


def _on_boundary(p: tuple[float, float], poly: Polygon) -> bool:
    q = project(p, poly)
    return inside(p, poly) and abs(q[0] - p[0]) + abs(q[1] - p[1]) < 1e-9


def _collinear(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> bool:
    return abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) < 1e-9


@pytest.mark.parametrize("commanded", [c for c, _ in MEASURED_CLIPS])
def test_toward_white_lands_on_the_boundary_on_the_line_to_white(
    commanded: tuple[float, float],
) -> None:
    got = toward_white(commanded, MEASURED)
    assert _on_boundary(got, MEASURED)
    assert _collinear(commanded, WHITE_D65, got)
    # between the command and white, never beyond either
    assert (
        min(commanded[0], WHITE_D65[0]) - 1e-9 <= got[0] <= max(commanded[0], WHITE_D65[0]) + 1e-9
    )


def test_toward_white_differs_from_closest_point_off_a_vertex() -> None:
    # far red: the closest point is the red vertex; the line to white crosses an edge instead
    near = project((0.95, 0.04), MEASURED)
    desat = toward_white((0.95, 0.04), MEASURED)
    assert abs(near[0] - desat[0]) + abs(near[1] - desat[1]) > 0.01
    assert toward_white((0.4, 0.35), MEASURED) == (0.4, 0.35)  # identity inside
    assert toward_white((0.4, 0.35), ()) == (0.4, 0.35)  # identity without a gamut


def test_rgb_clamp_projects_from_the_opposite_primary() -> None:
    blue, red, green = MEASURED
    cx = (blue[0] + red[0] + green[0]) / 3
    cy = (blue[1] + red[1] + green[1]) / 3
    # a little beyond the red-green edge (only blue negative): the line from blue through the
    # command meets that edge
    mx, my = (red[0] + green[0]) / 2, (red[1] + green[1]) / 2
    p = (mx + 0.03 * (mx - cx), my + 0.03 * (my - cy))
    got = rgb_clamp(p, MEASURED)
    assert _on_boundary(got, MEASURED)
    assert _collinear(blue, p, got)
    # far out behind the red vertex (red's neighbours both negative): the vertex itself
    behind = (cx + 3 * (red[0] - cx), cy + 3 * (red[1] - cy))
    got = rgb_clamp(behind, MEASURED)
    assert abs(got[0] - red[0]) + abs(got[1] - red[1]) < 1e-9
    assert rgb_clamp((0.4, 0.35), MEASURED) == (0.4, 0.35)
    # not a triangle: falls back to the closest point
    quad: Polygon = ((0.1532, 0.0475), (0.40, 0.12), (0.6915, 0.3083), (0.17, 0.70))
    assert rgb_clamp((0.95, 0.04), quad) == project((0.95, 0.04), quad)


def test_clip_dispatches_and_diverged_honours_the_rule() -> None:
    p = (0.95, 0.04)
    assert clip(p, MEASURED) == project(p, MEASURED)
    assert clip(p, MEASURED, "toward_white") == toward_white(p, MEASURED)
    assert clip(p, MEASURED, "rgb_clamp") == rgb_clamp(p, MEASURED)
    assert reachable(p, MEASURED, "toward_white") == toward_white(p, MEASURED)
    shown = toward_white(p, MEASURED)
    assert not diverged(shown, p, MEASURED, 0.003, "toward_white")
    assert diverged(shown, p, MEASURED, 0.003, "closest")


def test_vertex_deviation() -> None:
    assert vertex_deviation(MEASURED, MEASURED) == 0.0
    rotated = (MEASURED[1], MEASURED[2], MEASURED[0])
    assert vertex_deviation(MEASURED, rotated) == 0.0
    moved = ((0.153185, 0.047547), (0.701493, 0.308293), (0.169986, 0.699992))
    assert abs(vertex_deviation(MEASURED, moved) - 0.01) < 1e-9
    assert vertex_deviation(MEASURED, ()) == float("inf")
