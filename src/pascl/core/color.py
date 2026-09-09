"""Chromaticity math.

CIE xy at unit luminance to OKLab and back (CSS Color 4 / Ottosson, D65), and the Cartesian
OKLab lerp the reference installation uses for every colour interpolation: between the palette's
white anchors, and from a fixture's goal toward a held colour override as that override decays.
Blending in OKLab rather than in xy avoids the grey wash a straight xy lerp cuts through between
a saturated colour and white. Luminance rides its own channel, so it is fixed at 1 here and
cancels when the result is renormalised to xy.

Ported constant for constant; the reference rounds the blended xy to three decimals and clamps
it to the unit square, and so does this.
"""

from __future__ import annotations

from typing import Final

XY = tuple[float, float]
Lab = tuple[float, float, float]

NEAR_WHITE: Final[XY] = (0.322, 0.329)


def _cbrt(v: float) -> float:
    return float(v ** (1.0 / 3.0)) if v >= 0 else -float((-v) ** (1.0 / 3.0))


def xy_to_oklab(x: float, y: float) -> Lab:
    yy = y if y > 1e-9 else 1e-9
    big_x = x / yy
    big_y = 1.0
    big_z = (1.0 - x - y) / yy
    lms_l = 0.8190224379967030 * big_x + 0.3619062600528904 * big_y - 0.1288737815209879 * big_z
    lms_m = 0.0329836539323885 * big_x + 0.9292868615863434 * big_y + 0.0361446663506424 * big_z
    lms_s = 0.0481771893596242 * big_x + 0.2642395317527308 * big_y + 0.6335478284694309 * big_z
    l_, m_, s_ = _cbrt(lms_l), _cbrt(lms_m), _cbrt(lms_s)
    lab_l = 0.2104542683093140 * l_ + 0.7936177747023054 * m_ - 0.0040720430116193 * s_
    lab_a = 1.9779985324311684 * l_ - 2.4285922420485799 * m_ + 0.4505937096174110 * s_
    lab_b = 0.0259040424655478 * l_ + 0.7827717124575296 * m_ - 0.8086757549230774 * s_
    return (lab_l, lab_a, lab_b)


def oklab_to_xy(lab_l: float, lab_a: float, lab_b: float) -> XY:
    l_ = lab_l + 0.3963377773761749 * lab_a + 0.2158037573099136 * lab_b
    m_ = lab_l - 0.1055613458156586 * lab_a - 0.0638541728258133 * lab_b
    s_ = lab_l - 0.0894841775298119 * lab_a - 1.2914855480194092 * lab_b
    lms_l, lms_m, lms_s = l_ * l_ * l_, m_ * m_ * m_, s_ * s_ * s_
    big_x = 1.2268798758459243 * lms_l - 0.5578149944602171 * lms_m + 0.2813910456659647 * lms_s
    big_y = -0.0405757452148008 * lms_l + 1.1122868032803170 * lms_m - 0.0717110580655164 * lms_s
    big_z = -0.0763729366746601 * lms_l - 0.4214933324022432 * lms_m + 1.5869240198367816 * lms_s
    tot = big_x + big_y + big_z
    if abs(tot) <= 1e-9:
        tot = 1e-9
    return (big_x / tot, big_y / tot)


def blend_xy(goal: XY, target: XY, t: float) -> XY:
    """Move ``goal`` toward ``target`` by ``t`` in OKLab: 0 is the goal, 1 the target."""
    t = min(max(t, 0.0), 1.0)
    g = xy_to_oklab(*goal)
    o = xy_to_oklab(*target)
    lab = (g[0] + t * (o[0] - g[0]), g[1] + t * (o[1] - g[1]), g[2] + t * (o[2] - g[2]))
    x, y = oklab_to_xy(*lab)
    return (round(min(max(x, 0.0), 1.0), 3), round(min(max(y, 0.0), 1.0), 3))


def kelvin_to_mired(kelvin: float) -> int:
    return int(1e6 / kelvin)
