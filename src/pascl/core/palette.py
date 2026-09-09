"""Palettes: living colour on the solar day.

The natural-white palette is a colour temperature that follows the sun (2000 K at the civil
twilights, 3100 K at noon on a sine hump, sliding to 1475 K at solar midnight on a raised-cosine
night ramp) and, for each fixture family, the chromaticity that temperature lands on, found by
blending the family's warm, cool and night anchors in OKLab. The warm palette is the same arc
shifted down by a user offset. A static palette is a list of chromaticities per family that a
fixture indexes by the global scene offset, the room's base and its own stride, so a scene keeps
moving on the wall even when its palette is fixed.

Ported from the reference installation's palette updaters constant for constant.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import cos, pi
from typing import Final

from pascl.core.color import NEAR_WHITE, XY, blend_xy
from pascl.model.schema import Calibration, Palette

CCT_WARM_K: Final = 2000.0
CCT_COOL_K: Final = 3100.0
CCT_NIGHT_K: Final = 1475.0


@dataclass(frozen=True)
class NaturalWhite:
    """The solar white at one instant: its temperature and how far into the night ramp it is."""

    kelvin: float
    factor: float
    night_weight: float
    is_night: bool

    @property
    def kelvin_rounded(self) -> int:
        return round(self.kelvin)


def night_weight(progress: float, astro_dawn: float, astro_dusk: float) -> float:
    """0 by day; a raised cosine from astronomical dusk to solar midnight and back to dawn."""
    if progress < 0:
        return 0.0
    if progress >= astro_dusk:
        np_ = (progress - astro_dusk) / (1.0 - astro_dusk)
        return (1 - cos(np_ * pi)) / 2
    if progress < astro_dawn:
        np_ = progress / astro_dawn
        return (1 + cos(np_ * pi)) / 2
    return 0.0


def natural_white(progress: float, factor: float, pct: Mapping[str, float]) -> NaturalWhite:
    """The natural-white arc at day progress ``progress`` (-1 when the solar clock is not ready)."""
    if progress < 0:
        return NaturalWhite(kelvin=CCT_WARM_K, factor=0.0, night_weight=0.0, is_night=False)
    astro_dawn = pct.get("astronomical_dawn", 0.1)
    astro_dusk = pct.get("astronomical_dusk", 0.9)
    is_night = progress >= astro_dusk or progress < astro_dawn
    nw = night_weight(progress, astro_dawn, astro_dusk)
    if is_night:
        kelvin = CCT_WARM_K + nw * (CCT_NIGHT_K - CCT_WARM_K)
    else:
        kelvin = CCT_WARM_K + factor * (CCT_COOL_K - CCT_WARM_K)
    return NaturalWhite(kelvin=kelvin, factor=factor, night_weight=nw, is_night=is_night)


def natural_xy(white: NaturalWhite, calibration: Calibration) -> XY:
    """Where the natural white sits for one fixture family."""
    if white.is_night:
        return blend_xy(calibration.warm_xy, calibration.night_xy, white.night_weight)
    return blend_xy(calibration.warm_xy, calibration.cool_xy, white.factor)


def warm_kelvin(white: NaturalWhite, offset_k: float) -> float:
    return max(CCT_NIGHT_K, white.kelvin - offset_k)


def kelvin_xy(kelvin: float, calibration: Calibration) -> XY:
    """The chromaticity of a temperature on the family's arc: warm to cool above 2000 K, warm
    to night below it."""
    if kelvin >= CCT_WARM_K:
        f = (kelvin - CCT_WARM_K) / (CCT_COOL_K - CCT_WARM_K)
        return blend_xy(calibration.warm_xy, calibration.cool_xy, f)
    f = (CCT_WARM_K - kelvin) / (CCT_WARM_K - CCT_NIGHT_K)
    return blend_xy(calibration.warm_xy, calibration.night_xy, f)


def static_index(offset: int, base: int, stride: int, length: int) -> int:
    """Which entry of a static palette a fixture shows now; -1 when the palette is empty."""
    if length <= 0:
        return -1
    return (offset + base + stride) % length


def scene_goal(
    palette: Palette,
    is_strip: bool,
    white: NaturalWhite,
    calibration: Calibration,
    offset: int,
    base: int,
    stride: int,
) -> XY:
    """The colour a scene asks of one fixture right now.

    ``solar_keyframes`` (the day-of-week keyframe palette) is not yet modelled and renders as
    natural white; recorded as a v0 gap rather than approximated.
    """
    if palette.kind == "static":
        entries = palette.strips if is_strip else palette.bulbs
        idx = static_index(offset, base, stride, len(entries))
        return entries[idx] if idx >= 0 else NEAR_WHITE
    if palette.kind == "solar_cct_offset":
        return kelvin_xy(warm_kelvin(white, float(palette.offset_k or 0)), calibration)
    return natural_xy(white, calibration)
