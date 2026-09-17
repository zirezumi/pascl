"""Polygon checks the Home Model needs to validate a measured gamut.

The model is the lowest layer and imports nothing above it, so the little geometry its
validation needs lives here; ``pascl.core.gamut`` builds the projection and the hull on top of
it. Everything is pure and dependency-free.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Final, Literal

XY = tuple[float, float]

#: Fewest / most vertices a measured polygon may have. Three primaries give a triangle; a
#: multi-primary emitter a larger convex hull; more than twelve is not a gamut, it is noise.
MIN_VERTICES: Final = 3
MAX_VERTICES: Final = 12
#: A gamut smaller than this in xy area is not a colour gamut (a white-only device measures
#: as a point or a sliver and must resolve to the identity, never to a wrong polygon).
MIN_AREA: Final = 0.005
#: Two vertices closer than this collapse to one.
DUP_TOL: Final = 0.002
#: Two fixtures whose materials carry the same model label are the same hardware by the
#: author's declaration. Measured polygons that disagree by more than this are not two units
#: of one model: one measurement is wrong, or the label is. The model refuses to carry that.
SAME_MODEL_FAIL: Final = 0.02

#: How a device maps a commanded chromaticity it cannot reach onto its gamut. ``closest`` is
#: the nearest point of the polygon (every fixture measured so far); ``toward_white`` the point
#: where the line from the command to the white point crosses the boundary; ``rgb_clamp`` what
#: clamping negative primaries to zero gives (a triangle only). Measured, never assumed: the
#: calibration fits every rule to the device's answers and records the one that reproduces
#: them. The names are data the model carries; the arithmetic lives in ``pascl.core.gamut``.
ClipRule = Literal["closest", "toward_white", "rgb_clamp"]
CLIP_RULES: Final[tuple[ClipRule, ...]] = ("closest", "toward_white", "rgb_clamp")


def cross(o: XY, a: XY, b: XY) -> float:
    """z of (a - o) x (b - o): positive when o -> a -> b turns counter-clockwise."""
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def signed_area(poly: Sequence[XY]) -> float:
    """Shoelace area: positive for a counter-clockwise polygon, negative for clockwise."""
    n = len(poly)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        s += a[0] * b[1] - b[0] * a[1]
    return s / 2.0


def vertex_deviation(a: Sequence[XY], b: Sequence[XY]) -> float:
    """How far two polygons disagree: the largest distance from any vertex of either to the
    nearest vertex of the other. Zero for the same polygon in any rotation; infinite when
    either has no vertices."""
    if not a or not b:
        return math.inf
    return max(
        max(min(math.dist(p, q) for q in b) for p in a),
        max(min(math.dist(q, p) for p in a) for q in b),
    )


def polygon_problems(poly: Sequence[XY]) -> list[str]:
    """Why ``poly`` is not an acceptable gamut, empty when it is: vertex count, chromaticity
    range, convexity, counter-clockwise winding, area, and no two vertices too close."""
    errs: list[str] = []
    n = len(poly)
    if n < MIN_VERTICES or n > MAX_VERTICES:
        errs.append(f"{n} vertices; a gamut has {MIN_VERTICES} to {MAX_VERTICES}")
        return errs
    for i, (x, y) in enumerate(poly):
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and x + y <= 1.0):
            errs.append(f"vertex {i} {[x, y]} is not a chromaticity")
    if errs:
        return errs
    signs = {
        1 if cross(poly[i], poly[(i + 1) % n], poly[(i + 2) % n]) > 0 else -1 for i in range(n)
    }
    if len(signs) > 1:
        errs.append("not convex")
    area = signed_area(poly)
    if area < 0:
        errs.append("clockwise; vertices must run counter-clockwise")
    if abs(area) < MIN_AREA:
        errs.append(f"area {abs(area):.4f} below {MIN_AREA}")
    for i in range(n):
        for j in range(i + 1, n):
            if math.dist(poly[i], poly[j]) < DUP_TOL:
                errs.append(f"vertices {i} and {j} are within {DUP_TOL}")
    return errs
