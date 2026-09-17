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
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from pascl.core.gamut import XY, ClipRule, Polygon, clip
from pascl.core.palette import CCT_COOL_K, CCT_NIGHT_K, kelvin_xy
from pascl.model import HomeModel

#: An entry further than this per axis from its clip is reported; below it the clip is
#: within a transport's own rounding and no one could tell.
MIN_GAP: Final = 0.001
#: How finely the solar arcs are sampled, in kelvin.
ARC_STEP_K: Final = 25.0


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
