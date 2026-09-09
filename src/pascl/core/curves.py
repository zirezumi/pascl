"""Brightness curves on the solar day.

A fixture type's brightness is a table of eight values on named anchors of the normalised solar
day (midnight, the pre-dawn midpoint, civil dawn, mid-morning, noon, mid-afternoon, civil dusk,
the pre-midnight midpoint), one table for an occupied room and one for a vacant one. The anchor
positions come from the day's normalised civil dawn, noon and civil dusk fractions, and the day is
closed by a second midnight just short of 1 so the last segment has a far end. Between two
anchors the value eases: a quarter sine rising (fast, then flattening), an inverted quarter cosine
falling (slow, then steep). Ambient normalisation scales the eased value; the room multiplier and
the fixture factor scale the clamped baseline. A value that would round below half a step turns
the fixture off rather than dimming it to one.

Every quantity here is a plain number; nothing reads a clock or a model at render time beyond
the curve table and the anchor fractions passed in.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import cos, pi, sin
from typing import Final

DEFAULT_LEVEL: Final = 128
MIN_LEVEL: Final = 1
MAX_LEVEL: Final = 254
CURVE_ZERO_BELOW: Final = 0.5
CLOSING_MIDNIGHT: Final = 0.999999


@dataclass(frozen=True)
class Anchor:
    progress: float
    name: str


@dataclass(frozen=True)
class AnchorTable:
    """The nine anchors of one day, sorted by progress."""

    anchors: tuple[Anchor, ...]

    @classmethod
    def from_day(cls, civil_dawn: float, noon: float, civil_dusk: float) -> AnchorTable:
        cd, n, dusk = civil_dawn, noon, civil_dusk
        raw = [
            Anchor(0.0, "midnight"),
            Anchor(cd / 2, "pre_dawn_mid"),
            Anchor(cd, "civil_dawn"),
            Anchor((cd + n) / 2, "am_mid"),
            Anchor(n, "noon"),
            Anchor((n + dusk) / 2, "pm_mid"),
            Anchor(dusk, "civil_dusk"),
            Anchor((dusk + 1.0) / 2, "pre_midnight_mid"),
            Anchor(CLOSING_MIDNIGHT, "midnight"),
        ]
        return cls(tuple(sorted(raw, key=lambda a: a.progress)))

    def segment(self, p: float) -> tuple[Anchor, Anchor, float]:
        """The anchors bracketing ``p`` and the eased-input fraction between them."""
        idx = sum(1 for a in self.anchors if a.progress <= p) - 1
        if idx < 0:
            idx = 0
        start = self.anchors[idx]
        end = self.anchors[(idx + 1) % len(self.anchors)]
        to_time = end.progress + (0.0 if end.progress > start.progress else 1.0)
        p_time = p + (1.0 if p < start.progress else 0.0)
        span = to_time - start.progress
        u = (p_time - start.progress) / (span if span != 0 else 1.0)
        # The reference renders this ratio as fixed-point text with nine decimals before reading
        # it back, so it never sees more precision than that.
        u = float(f"{u:.9f}")
        return start, end, u


def interpolate(table: Mapping[str, int], p: float, anchors: AnchorTable) -> float:
    """The eased brightness of one curve table at day progress ``p``."""
    start, end, u = anchors.segment(p)
    bri_from = float(table.get(start.name, DEFAULT_LEVEL))
    bri_to = float(table.get(end.name, DEFAULT_LEVEL))
    if bri_to >= bri_from:
        return bri_from + sin(u * (pi / 2)) * (bri_to - bri_from)
    return bri_from + (1.0 - cos(u * (pi / 2))) * (bri_to - bri_from)


@dataclass(frozen=True)
class Brightness:
    raw: float
    """The eased curve value after ambient normalisation, before clamping."""
    solar_baseline: int
    """The clamped baseline the layers scale."""
    goal: int
    """What the fixture is asked for: baseline times room multiplier times fixture factor."""
    baseline_target: float
    """Baseline times multiplier, unclamped: the divisor an override back-computes against."""
    off: bool
    """True when the curve asks for less than half a step, which means off rather than dim."""


def clamp_level(value: float) -> int:
    return min(max(int(value), MIN_LEVEL), MAX_LEVEL)


def compose(
    table: Mapping[str, int],
    p: float,
    anchors: AnchorTable,
    ambient: float = 1.0,
    multiplier: float = 1.0,
    factor: float = 1.0,
) -> Brightness:
    raw = interpolate(table, p, anchors) * ambient
    baseline = clamp_level(raw)
    goal = clamp_level(baseline * multiplier * factor)
    return Brightness(
        raw=raw,
        solar_baseline=baseline,
        goal=goal,
        baseline_target=baseline * multiplier,
        off=raw < CURVE_ZERO_BELOW,
    )
