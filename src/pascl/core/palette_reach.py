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
from typing import Final

from pascl.core.gamut import XY, ClipRule, Polygon, clip
from pascl.core.palette import CCT_COOL_K, CCT_NIGHT_K, kelvin_xy
from pascl.model import (
    CREDIBLE_KELVIN,
    HomeModel,
    fixture_cct_range,
    fixture_cct_sources,
    unobservable,
    untried,
)

#: An entry further than this per axis from its clip is reported; below it the clip is
#: within a transport's own rounding and no one could tell.
MIN_GAP: Final = 0.001
#: How finely the solar arcs are sampled, in kelvin.
ARC_STEP_K: Final = 25.0
#: The credible band (``pascl.model.CREDIBLE_KELVIN``): a range beyond it is a placeholder,
#: and the render is floored at a number the device will clip.
CREDIBLE_K: Final[tuple[int, int]] = CREDIBLE_KELVIN

CctRange = tuple[int | None, int | None]
"""(floor, ceiling) in kelvin as ``fixture_cct_range`` resolves them; a None end is one
nothing speaks for."""
CctSources = tuple[str | None, str | None]
"""The provenance of each end, as ``fixture_cct_sources`` gives it (a tier name, ``author``
for the material's declaration, None for an end nothing speaks for)."""


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
    ct_range_k: CctRange


@dataclass(frozen=True)
class CctCredibility:
    """A colour-temperature range a reader should look at: an end outside what a physical
    device could have, an end no tier could speak for, or the author's slot holding a
    transport's placeholder. ``question`` is set only for the one state in which the model
    asks the author: an end every tier declined WITH PROOF (``unobservable``), where the
    chain has shown that nothing on the wire can bound it."""

    ct_range_k: CctRange
    sources: CctSources
    fixtures: tuple[str, ...]
    note: str
    material: str = ""
    question: str | None = None

    @property
    def source(self) -> str:
        """The provenance in one word for a row: the floor's tier, else the ceiling's, else
        ``unknown``."""
        return self.sources[0] or self.sources[1] or "unknown"


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


CctState = tuple[str, ...]
"""Per fixture, how each unsourced end stands: ``unobservable`` (every tier declined it with
proof), ``untried:<tiers>`` (a tier has not spoken with proof), or ``bounded``."""


def _end_state(fx_gamut: object, i: int, srcs: CctSources) -> str:
    from pascl.model import Gamut

    g = fx_gamut if isinstance(fx_gamut, Gamut) else None
    if srcs[i] is not None:
        return "bounded"
    if unobservable(g)[i]:
        return "unobservable"
    missing = untried(g)[i]
    return "untried:" + ",".join(missing)


def _cct_groups(
    model: HomeModel,
) -> list[tuple[CctRange, CctSources, bool, str, CctState, tuple[str, ...]]]:
    """Every cct-capable fixture, grouped by (range, sources, has xy, material, the state of
    each end)."""
    keyed: dict[tuple[CctRange, CctSources, bool, str, CctState], list[str]] = {}
    for room in model.rooms.values():
        for fid, fx in room.fixtures.items():
            mat = model.materials.get(fx.material)
            if mat is None or "cct" not in mat.capabilities:
                continue
            rng = fixture_cct_range(model, fid)
            srcs = fixture_cct_sources(model, fid)
            state = (_end_state(fx.gamut, 0, srcs), _end_state(fx.gamut, 1, srcs))
            key = (rng, srcs, "xy" in mat.capabilities, fx.material, state)
            keyed.setdefault(key, []).append(fid)
    return [
        (rng, srcs, has_xy, material, state, tuple(fids))
        for (rng, srcs, has_xy, material, state), fids in keyed.items()
    ]


def cct_unreachable(model: HomeModel) -> list[CctUnreachable]:
    """The solar arc's excursions outside the range of fixtures that render colour
    temperature only (``cct`` without ``xy``): the render floors a white at the range and
    such a fixture has no xy to fall back to, so the warmest night white and the coolest
    day white are shown at the range's ends instead. One row per palette and range, at the
    worse of the two ends; an end nothing speaks for parks nothing."""
    out: list[CctUnreachable] = []
    for pid, pal in model.scenes.palettes.items():
        if pal.kind not in ("solar_cct", "solar_cct_offset"):
            continue
        for rng, _srcs, has_xy, _material, _state, fids in _cct_groups(model):
            if has_xy:
                continue
            lo, hi = rng
            below = lo - CCT_NIGHT_K if lo is not None and lo > CCT_NIGHT_K else 0.0
            above = CCT_COOL_K - hi if hi is not None and hi < CCT_COOL_K else 0.0
            if below <= 0 and above <= 0:
                continue
            if below >= above and lo is not None:
                out.append(CctUnreachable(pid, CCT_NIGHT_K, lo, below, fids, rng))
            elif hi is not None:
                out.append(CctUnreachable(pid, CCT_COOL_K, hi, above, fids, rng))
    out.sort(key=lambda u: (u.palette, -u.gap))
    return out


def _end_note(
    which: str, value: int | None, source: str | None, bound: int, state: str, *, below: bool
) -> str | None:
    if value is None:
        if state == "unobservable":
            return f"{which} unobservable: every tier declined it with proof"
        tiers = state.split(":", 1)[1] if ":" in state else ""
        return f"{which} not known yet: {tiers or 'a tier'} still to speak"
    outside = value < bound if below else value > bound
    if not outside:
        return None
    where = "below" if below else "above"
    return f"{which} {value} K ({source}) is {where} {bound} K"


def _proof(model: HomeModel, fids: tuple[str, ...]) -> str:
    """The declines recorded on the first fixture of a group, as one line: the proof that
    goes beside the question."""
    for room in model.rooms.values():
        for fid in fids:
            fx = room.fixtures.get(fid)
            if fx is not None and fx.gamut is not None and fx.gamut.ct_declined:
                return "; ".join(f"{d.tier}: {d.reason}" for d in fx.gamut.ct_declined)
    return ""


def cct_credibility(model: HomeModel) -> list[CctCredibility]:
    """Every colour-temperature range in force with an end a reader should look at, one row
    per (range, provenance, material, state):

    - an end outside ``CREDIBLE_K`` from the author's slot: the transport's placeholder copied
      into it, never a declaration (the author tier is never populated from a transport);
      replace it with the vendor's value;
    - an end outside ``CREDIBLE_K`` from a measurement: it contradicts physics, look at it;
    - an end no tier could speak for, in one of two states that must never read alike
      (DERIVED_PARAMETERS P2): UNOBSERVABLE, every tier declined it with proof, the one state
      in which the row carries ``question``, the single thing the author is asked for, with
      the proof beside it; or NOT KNOWN YET, a tier still to speak, which is a measurement
      or an observation away, not a question for the author.

    Each row names the ends' provenance, so probed, observed, declared, inherited and the
    author's word read apart."""
    out: list[CctCredibility] = []
    lo_ok, hi_ok = CREDIBLE_K
    for rng, srcs, _has_xy, material, state, fids in _cct_groups(model):
        lo, hi = rng
        problems = [
            n
            for n in (
                _end_note("floor", lo, srcs[0], lo_ok, state[0], below=True),
                _end_note("ceiling", hi, srcs[1], hi_ok, state[1], below=False),
            )
            if n is not None
        ]
        if not problems:
            continue
        what = "; ".join(problems)
        question: str | None = None
        if "unobservable" in state:
            ends = " and ".join(
                e for e, st in zip(("floor", "ceiling"), state, strict=True) if st == "unobservable"
            )
            question = (
                f"what is the white range of material {material!r} (its {ends})? Nothing on this "
                f"transport can bound it, so the model asks for exactly this, on the material "
                f"(cct_range_k), labelled author; the render floors it at the policy floor until "
                f"then. Proof: {_proof(model, fids)}"
            )
            how = "the chain declined with proof at every tier; see the question"
        elif lo is None or hi is None:
            how = (
                "not a question for the author yet: measure it (pascl gamut measure --ct-only) "
                "or gather its pairs (pascl gamut observe-ct) until every tier has spoken"
            )
        elif "author" in srcs:
            how = (
                f"the author's slot on material {material!r} holds a transport's placeholder, not "
                "a declaration (the author tier is never populated from a transport); replace it "
                "with the vendor's value, or measure the fixture"
            )
        else:
            how = "not credible for a physical emitter: the measurement wants a look"
        out.append(CctCredibility(rng, srcs, fids, f"{what}; {how}", material, question))
    out.sort(key=lambda c: (c.source, c.ct_range_k[0] or 0, c.ct_range_k[1] or 0, c.material))
    return out
