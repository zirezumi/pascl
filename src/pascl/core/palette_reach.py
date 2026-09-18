"""Which of a home's palette colours its fixtures can actually show.

A palette may name any chromaticity; a fixture shows the clip of it onto its measured gamut.
The comparators are exact about that (``pascl.core.gamut``), so an unreachable entry costs
nothing at runtime, but it is still a colour the author chose and will never see. This report
lists every such entry with the colour that renders instead, so the choice becomes visible:
keep it (the reachable colour may be fine), move it inside, or change the fixture.

Static palettes are checked entry by entry, per family (``bulbs`` for bulb materials,
``strips`` for strips). The solar white palettes run along each calibration's arc (warm to
cool by day, warm to night after dusk); the arc is sampled and its worst excursion reported.
Fixtures that share a polygon and rule are reported together, so a fleet of one model reads
as one line per entry. Pure: the model in, rows out.

Colour temperature has the same question in one dimension. The solar arc is rendered as a
colour temperature on a fixture that takes one, from ``CCT_NIGHT_K`` to ``CCT_COOL_K``, and
the render floors it at the fixture's range (``fixture_cct_range``: measured, else declared)
and falls back to the calibrated xy below the floor. A fixture with ``cct`` and no ``xy``
cannot fall back, so the part of the arc outside its range is unreachable and reported
(``cct_unreachable``). And the range itself may be wrong: a declared one is what the
transport advertised, and the reference installation found 22 bulbs advertising 1000-20000 K
over a physical 2000-6535 K, so the render sent them a night white they clipped. A range
outside ``CREDIBLE_K`` is reported as not credible (``cct_credibility``) until measured.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

from pascl.core.gamut import XY, ClipRule, Polygon, clip
from pascl.core.palette import CCT_COOL_K, CCT_NIGHT_K, kelvin_xy
from pascl.model import CREDIBLE_KELVIN, HomeModel, fixture_cct_range

#: An entry further than this per axis from its clip is reported; below it the clip is
#: within a transport's own rounding and no one could tell.
MIN_GAP: Final = 0.001
#: How finely the solar arcs are sampled, in kelvin.
ARC_STEP_K: Final = 25.0
#: The credible band (``pascl.model.CREDIBLE_KELVIN``): a range beyond it is a placeholder,
#: and the render is floored at a number the device will clip.
CREDIBLE_K: Final[tuple[int, int]] = CREDIBLE_KELVIN


@dataclass(frozen=True)
class CctUnreachable:
    """The part of a palette's colour-temperature arc some fixtures cannot show and cannot
    render as xy either (their material has ``cct`` and no ``xy``)."""

    palette: str
    kelvin: float
    """The arc's temperature furthest outside the range."""
    reachable: int
    """The range end the fixtures show instead."""
    gap: float
    """Kelvin between the two."""
    fixtures: tuple[str, ...]
    ct_range_k: tuple[int, int]


CctSource = Literal["measured", "declared"]


@dataclass(frozen=True)
class CctCredibility:
    """A colour-temperature range that cannot be a physical device's."""

    ct_range_k: tuple[int, int]
    source: CctSource
    fixtures: tuple[str, ...]
    note: str


@dataclass(frozen=True)
class Unreachable:
    palette: str
    family: str
    """``bulbs`` / ``strips`` with an index for a static palette; ``arc`` for a solar one."""
    index: int | None
    intent: XY
    """The authored colour (for an arc, the point of worst excursion)."""
    reachable: XY
    """What the fixtures show instead."""
    gap: float
    """The larger per-axis distance between the two."""
    fixtures: tuple[str, ...]
    """Every fixture with this polygon and rule that renders this palette family."""
    kelvin: float | None = None
    """For an arc, the temperature at which the excursion is worst."""


@dataclass(frozen=True)
class _GamutGroup:
    polygon: Polygon
    rule: ClipRule
    fixtures: tuple[str, ...]
    is_strip: bool
    calibration: str | None


def _groups(model: HomeModel) -> list[_GamutGroup]:
    keyed: dict[tuple[Polygon, ClipRule, bool, str | None], list[str]] = {}
    for room in model.rooms.values():
        for fid, fx in room.fixtures.items():
            mat = model.materials.get(fx.material)
            if mat is None or "xy" not in mat.capabilities or fx.gamut is None:
                continue
            key = (fx.gamut.vertices, fx.gamut.clip_rule, mat.kind == "strip", mat.calibration)
            keyed.setdefault(key, []).append(fid)
    return [
        _GamutGroup(poly, rule, tuple(fids), is_strip, cal)
        for (poly, rule, is_strip, cal), fids in keyed.items()
    ]


def _gap(a: XY, b: XY) -> float:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def _arc(model: HomeModel, calibration: str) -> Iterable[tuple[float, XY]]:
    cal = model.calibrations.get(calibration)
    if cal is None:
        return
    k = CCT_NIGHT_K
    while k <= CCT_COOL_K + 1e-9:
        yield k, kelvin_xy(k, cal)
        k += ARC_STEP_K


def unreachable(model: HomeModel, min_gap: float = MIN_GAP) -> list[Unreachable]:
    """Every palette colour some measured fixture cannot show, with what it shows instead."""
    out: list[Unreachable] = []
    groups = _groups(model)
    for pid, pal in model.scenes.palettes.items():
        if pal.kind == "static":
            for grp in groups:
                family = "strips" if grp.is_strip else "bulbs"
                entries = pal.strips if grp.is_strip else pal.bulbs
                for i, entry in enumerate(entries):
                    shown = clip(entry, grp.polygon, grp.rule)
                    gap = _gap(entry, shown)
                    if gap > min_gap:
                        out.append(Unreachable(pid, family, i, entry, shown, gap, grp.fixtures))
        elif pal.kind in ("solar_cct", "solar_cct_offset"):
            for grp in groups:
                if grp.calibration is None:
                    continue
                worst: Unreachable | None = None
                for k, xy in _arc(model, grp.calibration):
                    shown = clip(xy, grp.polygon, grp.rule)
                    gap = _gap(xy, shown)
                    if gap > min_gap and (worst is None or gap > worst.gap):
                        worst = Unreachable(pid, "arc", None, xy, shown, gap, grp.fixtures, k)
                if worst is not None:
                    out.append(worst)
    out.sort(key=lambda u: (u.palette, u.family, u.index if u.index is not None else -1, -u.gap))
    return out


def _cct_groups(model: HomeModel) -> list[tuple[tuple[int, int], CctSource, bool, tuple[str, ...]]]:
    """Every fixture with a colour-temperature range, grouped by (range, source, has xy)."""
    keyed: dict[tuple[tuple[int, int], CctSource, bool], list[str]] = {}
    for room in model.rooms.values():
        for fid, fx in room.fixtures.items():
            mat = model.materials.get(fx.material)
            if mat is None or "cct" not in mat.capabilities:
                continue
            rng = fixture_cct_range(model, fid)
            if rng is None:
                continue
            measured = fx.gamut is not None and fx.gamut.ct_range_k is not None
            src: CctSource = "measured" if measured else "declared"
            key = (rng, src, "xy" in mat.capabilities)
            keyed.setdefault(key, []).append(fid)
    return [(rng, src, has_xy, tuple(fids)) for (rng, src, has_xy), fids in keyed.items()]


def cct_unreachable(model: HomeModel) -> list[CctUnreachable]:
    """The solar arc's excursions outside the range of fixtures that render colour
    temperature only (``cct`` without ``xy``): the render floors a white at the range and
    such a fixture has no xy to fall back to, so the warmest night white and the coolest
    day white are shown at the range's ends instead. One row per palette and range, at the
    worse of the two ends."""
    out: list[CctUnreachable] = []
    for pid, pal in model.scenes.palettes.items():
        if pal.kind not in ("solar_cct", "solar_cct_offset"):
            continue
        for rng, _src, has_xy, fids in _cct_groups(model):
            if has_xy:
                continue
            lo, hi = rng
            below = lo - CCT_NIGHT_K if lo > CCT_NIGHT_K else 0.0
            above = CCT_COOL_K - hi if hi < CCT_COOL_K else 0.0
            if below <= 0 and above <= 0:
                continue
            if below >= above:
                out.append(CctUnreachable(pid, CCT_NIGHT_K, lo, below, fids, rng))
            else:
                out.append(CctUnreachable(pid, CCT_COOL_K, hi, above, fids, rng))
    out.sort(key=lambda u: (u.palette, -u.gap))
    return out


def cct_credibility(model: HomeModel) -> list[CctCredibility]:
    """Every colour-temperature range in force (measured, else declared) that lies outside
    ``CREDIBLE_K``: the transport's placeholder, until the fixture is measured. A measured
    range outside it is reported too, since the measurement then contradicts physics."""
    out: list[CctCredibility] = []
    lo_ok, hi_ok = CREDIBLE_K
    for rng, src, _has_xy, fids in _cct_groups(model):
        lo, hi = rng
        problems = []
        if lo < lo_ok:
            problems.append(f"floor {lo} K is below {lo_ok} K")
        if hi > hi_ok:
            problems.append(f"ceiling {hi} K is above {hi_ok} K")
        if not problems:
            continue
        what = "; ".join(problems)
        how = (
            "not credible for a physical emitter: measure it (the render floors the night "
            "white at this range, and the device clips what it is sent)"
            if src == "declared"
            else "not credible for a physical emitter: the measurement wants a look"
        )
        out.append(CctCredibility(rng, src, fids, f"{what}; {how}"))
    out.sort(key=lambda c: (c.source, c.ct_range_k))
    return out
