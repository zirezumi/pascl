"""Colour gamut geometry.

A fixture cannot render every chromaticity a palette can name. Commanded a point outside its
gamut, the device clips to the nearest reachable point and reports that, so a comparator that
judges the device against the authored intent disagrees for as long as the intent is out of
reach, and a correction loop built on that comparator re-sends the same unreachable colour on
every visit. The reference installation measured that loop at 44 % of all colour corrections
before it modelled reachability.

The engine's answer is exact rather than permissive: the intent stays authored on the wire and
in the cache, and every comparator judges the device against the intent CLIPPED onto the
fixture's gamut the way the device itself clips it: the closest point of the convex polygon for
every fixture measured so far (``project``), or one of the other rules a device may follow
(``clip``), whichever the calibration fitted to its answers. For an in-gamut intent the clip is
the identity, so nothing changes; for an out-of-gamut one the tolerance sits around the point
the device can actually reach. A wrong polygon fails loud (the comparator fires), a missing one
degrades to the identity, and no brand knowledge lives here: a polygon and its rule are
MEASURED per physical fixture (``pascl.estimator.gamut``), a triangle for a three-primary
emitter, larger for a multi-primary one.

Everything here is pure and deterministic. The projection reproduces the device's own
closest-point clip to within 2e-05 on every fixture measured so far.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Final

from pascl.model.geometry import (
    CLIP_RULES,
    DUP_TOL,
    XY,
    ClipRule,
    cross,
    polygon_problems,
    signed_area,
)

__all__ = [
    "BOUNDARY_EPS",
    "CLIP_RULES",
    "COLLINEAR_TOL",
    "WHITE_D65",
    "XY",
    "ClipRule",
    "Polygon",
    "clip",
    "convex_hull",
    "diverged",
    "inside",
    "polygon_problems",
    "project",
    "reachable",
    "rgb_clamp",
    "signed_area",
    "toward_white",
]

Polygon = tuple[XY, ...]

#: A hull vertex within this distance of the chord between its neighbours is collinear noise.
COLLINEAR_TOL: Final = 0.001
#: A point this close to an edge (cross product) counts as on it: a projected point must test
#: inside its own polygon despite floating-point residue.
BOUNDARY_EPS: Final = 1e-12
#: The white point the ``toward_white`` rule pulls toward (CIE D65).
WHITE_D65: Final[XY] = (0.3127, 0.3290)


def inside(p: XY, poly: Sequence[XY]) -> bool:
    """True when ``p`` is inside or on the boundary of the convex polygon (any winding)."""
    n = len(poly)
    if n < 3:
        return True
    pos = neg = 0
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        cr = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
        if cr > BOUNDARY_EPS:
            pos += 1
        elif cr < -BOUNDARY_EPS:
            neg += 1
    return pos == 0 or neg == 0


def project(p: XY, poly: Sequence[XY]) -> XY:
    """Closest point of ``p`` on the convex polygon; ``p`` itself when inside, on the boundary,
    or when the polygon has fewer than three vertices (no gamut known: the identity).

    Full precision, no rounding, so the caller's tolerance is the only slack. Constant for
    constant with the reference installation's Jinja twin, which its gate holds equal.
    """
    n = len(poly)
    px, py = float(p[0]), float(p[1])
    if n < 3:
        return (px, py)
    pos = neg = 0
    best_d = math.inf
    best = (px, py)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        vx, vy = b[0] - a[0], b[1] - a[1]
        cr = vx * (py - a[1]) - vy * (px - a[0])
        if cr > 0:
            pos += 1
        elif cr < 0:
            neg += 1
        den = vx * vx + vy * vy
        u = ((px - a[0]) * vx + (py - a[1]) * vy) / den if den > 0 else 0.0
        u = max(0.0, min(1.0, u))
        c = (a[0] + u * vx, a[1] + u * vy)
        dd = (px - c[0]) ** 2 + (py - c[1]) ** 2
        if dd < best_d:
            best_d, best = dd, c
    if pos == 0 or neg == 0:
        return (px, py)
    return best


def toward_white(p: XY, poly: Sequence[XY], white: XY = WHITE_D65) -> XY:
    """Where the segment from ``p`` to the white point crosses the polygon's boundary: the
    clip of a device that desaturates an unreachable colour rather than moving to the nearest
    reachable one. ``p`` itself when inside; the closest point when the white point is not
    inside the polygon either (no such segment exists)."""
    n = len(poly)
    px, py = float(p[0]), float(p[1])
    if n < 3 or inside((px, py), poly):
        return (px, py)
    if not inside(white, poly):
        return project((px, py), poly)
    dx, dy = white[0] - px, white[1] - py
    best_t = math.inf
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        ex, ey = b[0] - a[0], b[1] - a[1]
        den = dx * ey - dy * ex
        if abs(den) < BOUNDARY_EPS:
            continue  # the segment runs along this edge; a neighbouring edge catches it
        t = ((a[0] - px) * ey - (a[1] - py) * ex) / den
        u = ((a[0] - px) * dy - (a[1] - py) * dx) / den
        if -BOUNDARY_EPS <= u <= 1 + BOUNDARY_EPS and 0 <= t < best_t:
            best_t = t
    if best_t is math.inf:
        return project((px, py), poly)
    return (px + best_t * dx, py + best_t * dy)


def rgb_clamp(p: XY, poly: Sequence[XY]) -> XY:
    """The clip of a device that converts to its three primaries, clamps negative ones to
    zero and renormalises: in the triangle's barycentric coordinates, negative weights go to
    zero. Beyond one edge that is the point where the line from the opposite vertex through
    ``p`` meets the edge; beyond two, the vertex between them. Defined for a triangle only;
    any other polygon falls back to the closest point."""
    px, py = float(p[0]), float(p[1])
    if len(poly) != 3:
        return project((px, py), poly)
    if inside((px, py), poly):
        return (px, py)
    r, g, b = poly
    den = (g[1] - b[1]) * (r[0] - b[0]) + (b[0] - g[0]) * (r[1] - b[1])
    if abs(den) < BOUNDARY_EPS:
        return project((px, py), poly)
    wr = ((g[1] - b[1]) * (px - b[0]) + (b[0] - g[0]) * (py - b[1])) / den
    wg = ((b[1] - r[1]) * (px - b[0]) + (r[0] - b[0]) * (py - b[1])) / den
    wb = 1.0 - wr - wg
    weights = [max(0.0, wr), max(0.0, wg), max(0.0, wb)]
    total = sum(weights)
    if total <= 0:
        return project((px, py), poly)
    return (
        sum(w * v[0] for w, v in zip(weights, poly, strict=True)) / total,
        sum(w * v[1] for w, v in zip(weights, poly, strict=True)) / total,
    )


def clip(p: XY, poly: Sequence[XY], rule: ClipRule = "closest") -> XY:
    """What the device will show for ``p``, by its measured clip rule; the identity inside
    the polygon or when there is no polygon."""
    if rule == "toward_white":
        return toward_white(p, poly)
    if rule == "rgb_clamp":
        return rgb_clamp(p, poly)
    return project(p, poly)


def reachable(intent: XY, poly: Sequence[XY], rule: ClipRule = "closest") -> XY:
    """The colour the fixture will actually show for ``intent``: its clip onto the gamut."""
    return clip(intent, poly, rule)


def diverged(
    device: XY | None, intent: XY, poly: Sequence[XY], tol: float, rule: ClipRule = "closest"
) -> bool:
    """The comparator rule: the device sits more than ``tol`` on either axis from the REACHABLE
    form of the intent. A missing device reading counts as diverged."""
    if device is None:
        return True
    ex, ey = clip(intent, poly, rule)
    return abs(device[0] - ex) > tol or abs(device[1] - ey) > tol


def convex_hull(points: Iterable[XY]) -> Polygon:
    """Andrew's monotone chain, counter-clockwise, near-duplicates and collinear vertices
    dropped. Fewer than three distinct points come back as they are (no hull)."""
    pts = sorted({(round(x, 6), round(y, 6)) for x, y in points})
    if len(pts) < 3:
        return tuple(pts)
    lower: list[XY] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[XY] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    out: list[XY] = []
    for p in hull:
        if not out or math.dist(out[-1], p) > DUP_TOL:
            out.append(p)
    if len(out) > 1 and math.dist(out[0], out[-1]) <= DUP_TOL:
        out.pop()
    changed = True
    while changed and len(out) > 3:
        changed = False
        for i in range(len(out)):
            a, b, c = out[i - 1], out[i], out[(i + 1) % len(out)]
            chord = math.dist(a, c)
            if chord == 0:
                continue
            if abs(cross(a, c, b)) / chord < COLLINEAR_TOL:
                out.pop(i)
                changed = True
                break
    return tuple(out)
