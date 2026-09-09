"""The solar clock: anchors, seasonal normalisation, day progress and the daylight factor.

A day runs from one solar midnight to the next. Twenty-five named anchors mark it (twilights,
sunrise and sunset, the golden and blue hours, noon, and six shoulder points between the golden
hours and noon). Seasonal normalisation warps those anchors toward a reference day, the summer
solstice, by a blend fraction, so evenings feel like summer evenings for most of the year while
the sun itself is left alone. Day progress is the fraction of the (warped) day elapsed, and the
daylight factor is a sine hump between the golden hours that peaks at noon.

Everything here is pure: each function takes a site, a date or an instant, and the parameters,
and returns values. Nothing reads a clock; the shell decides what "now" is.

Three things about fidelity to the reference installation, whose published anchors are the
oracle this module is tested against:

* Every anchor except solar midnight and noon is an elevation crossing found by scan and
  bisection, because the reference's calls to the library's own sunrise, sunset, dawn and dusk
  fail under its runtime and fall back to those crossings. The crossings are what the lights
  have always followed, so they are what is ported.
* The elevation lookup is evaluated on local-zone datetimes. astral 2.x takes the calendar date
  from the datetime's own zone and the time of day from UTC, so the same instant written in UTC
  reads about two minutes differently in the evening. The reference writes local; so does this.
  Both facts are tied to the pinned library major version and go away together with it.
* Two departures are deliberate and recorded in the reference's exclusion list: the reference
  reads the host's daylight-saving flag at the moment it recomputes, whereas here the caller
  states it (``is_dst``); and the reference publishes two midpoint percentages nothing consumes,
  one of them mis-paired, which are not ported.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from math import asin, cos, degrees, floor, pi, radians, sin, tan
from typing import Final

from astral import Observer
from astral.sun import azimuth, elevation, midnight, noon

ANCHOR_KEYS: Final[tuple[str, ...]] = (
    "midnight",
    "next_midnight",
    "noon",
    "astronomical_dawn",
    "nautical_dawn",
    "civil_dawn",
    "sunrise",
    "golden_hour_am_start",
    "golden_hour_am_end",
    "blue_hour_am_start",
    "blue_hour_am_end",
    "sunset",
    "civil_dusk",
    "nautical_dusk",
    "astronomical_dusk",
    "golden_hour_pm_start",
    "golden_hour_pm_end",
    "blue_hour_pm_start",
    "blue_hour_pm_end",
    "am_early",
    "am_mid",
    "am_late",
    "pm_early",
    "pm_mid",
    "pm_late",
)

# The anchors whose presence gates "ready" in the reference implementation.
READY_KEYS: Final[tuple[str, ...]] = (
    "midnight",
    "next_midnight",
    "noon",
    "civil_dawn",
    "sunrise",
    "golden_hour_am_start",
    "golden_hour_am_end",
    "blue_hour_am_start",
    "blue_hour_am_end",
    "sunset",
    "civil_dusk",
    "golden_hour_pm_start",
    "golden_hour_pm_end",
    "blue_hour_pm_start",
    "blue_hour_pm_end",
)

# The anchors published as a fraction of the day.
PERCENT_KEYS: Final[tuple[str, ...]] = tuple(
    k for k in ANCHOR_KEYS if k not in ("midnight", "next_midnight")
)

PM_CHAIN: Final[tuple[str, ...]] = (
    "noon",
    "sunset",
    "civil_dusk",
    "nautical_dusk",
    "astronomical_dusk",
    "next_midnight",
)
AM_CHAIN: Final[tuple[str, ...]] = (
    "noon",
    "sunrise",
    "civil_dawn",
    "nautical_dawn",
    "astronomical_dawn",
    "midnight",
)
SECONDARY_KEYS: Final[tuple[str, ...]] = (
    "golden_hour_am_start",
    "golden_hour_am_end",
    "blue_hour_am_start",
    "blue_hour_am_end",
    "golden_hour_pm_start",
    "golden_hour_pm_end",
    "blue_hour_pm_start",
    "blue_hour_pm_end",
    "am_early",
    "am_mid",
    "am_late",
    "pm_early",
    "pm_mid",
    "pm_late",
)

PROGRESS_CEILING: Final = 0.999999


@dataclass(frozen=True)
class Site:
    """Where the home is. Elevation is in metres; ``tz`` is the zone whose calendar dates name
    the solar days."""

    latitude: float
    longitude: float
    elevation_m: float
    tz: tzinfo

    def observer(self) -> Observer:
        return Observer(
            latitude=self.latitude, longitude=self.longitude, elevation=self.elevation_m
        )


@dataclass(frozen=True)
class SolarParams:
    """Tunables of the solar clock. The defaults are the reference installation's values."""

    solstice_blend_frac: float = 0.775
    astronomical_depression: float = -18.0
    nautical_depression: float = -12.0
    civil_depression: float = -6.0
    blue_hour_top: float = -4.0
    golden_hour_top: float = 6.0
    sun_horizon: float = -0.833
    am_shoulders: tuple[float, float, float] = (0.20, 0.50, 0.95)
    pm_shoulders: tuple[float, float, float] = (0.05, 0.50, 0.95)
    crossing_coarse_steps: int = 48
    crossing_tolerance_s: float = 1.0


DEFAULT_PARAMS: Final = SolarParams()


@dataclass(frozen=True)
class DayAnchors:
    """The twenty-five anchors of one solar day, real or normalised, plus its daylight length.

    Every instant is UTC-aware. Arithmetic on aware datetimes that share a zone object is
    wall-clock arithmetic in Python, which goes wrong by an hour across a daylight-saving
    transition, so the clock keeps its instants in UTC and presents them in the site's zone only
    at the edge.
    """

    day: date
    at: dict[str, datetime]
    day_length: timedelta

    def __getitem__(self, key: str) -> datetime:
        return self.at[key]


@dataclass(frozen=True)
class SunPosition:
    elevation: float
    elevation_mid: float
    azimuth: float
    declination: float
    equation_of_time: float


# ---------------------------------------------------------------------------------------------
# Declination and the equation of time (NOAA-style series, UTC in, degrees / minutes out)
# ---------------------------------------------------------------------------------------------


def _julian_day(dt_utc: datetime) -> float:
    y, m = dt_utc.year, dt_utc.month
    d = dt_utc.day + (dt_utc.hour + (dt_utc.minute + dt_utc.second / 60) / 60) / 24
    a = floor((14 - m) / 12)
    y2 = y + 4800 - a
    m2 = m + 12 * a - 3
    return (
        d
        + floor((153 * m2 + 2) / 5)
        + 365 * y2
        + floor(y2 / 4)
        - floor(y2 / 100)
        + floor(y2 / 400)
        - 32045
    )


def _julian_centuries(dt_utc: datetime) -> float:
    return (_julian_day(dt_utc) - 2451545.0) / 36525.0


def _geom_mean_long_sun(t: float) -> float:
    return (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0


def _geom_mean_anom_sun(t: float) -> float:
    return 357.52911 + t * (35999.05029 - 0.0001537 * t)


def _ecc_earth_orbit(t: float) -> float:
    return 0.016708634 - t * (0.000042037 + 0.0000001267 * t)


def _sun_eq_of_center(t: float, m_deg: float) -> float:
    m = radians(m_deg)
    return (
        sin(m) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + sin(2 * m) * (0.019993 - 0.000101 * t)
        + sin(3 * m) * 0.000289
    )


def _mean_obliq_ecliptic(t: float) -> float:
    seconds = 21.448 - t * (46.8150 + t * (0.00059 - t * 0.001813))
    return 23.0 + (26.0 + (seconds / 60.0)) / 60.0


def _obliq_corr(t: float, eps0_deg: float) -> float:
    return eps0_deg + 0.00256 * sin(radians(125.04 - 1934.136 * t) + 1.57079632679)


def _sun_app_long(t: float, true_long_deg: float) -> float:
    return true_long_deg - 0.00569 - 0.00478 * sin(radians(125.04 - 1934.136 * t))


def declination(dt_utc: datetime) -> float:
    t = _julian_centuries(dt_utc)
    true_long = _geom_mean_long_sun(t) + _sun_eq_of_center(t, _geom_mean_anom_sun(t))
    lam_app = _sun_app_long(t, true_long)
    eps = _obliq_corr(t, _mean_obliq_ecliptic(t))
    return degrees(asin(sin(radians(eps)) * sin(radians(lam_app))))


def equation_of_time(dt_utc: datetime) -> float:
    t = _julian_centuries(dt_utc)
    l0 = radians(_geom_mean_long_sun(t))
    e = _ecc_earth_orbit(t)
    m = radians(_geom_mean_anom_sun(t))
    eps = _obliq_corr(t, _mean_obliq_ecliptic(t))
    y = tan(radians(eps) / 2) ** 2
    return 4 * degrees(
        y * sin(2 * l0)
        - 2 * e * sin(m)
        + 4 * e * y * sin(m) * sin(2 * l0 + 1.57079632679)
        - 0.5 * y * y * sin(4 * l0)
        - 1.25 * e * e * sin(2 * m)
    )


# ---------------------------------------------------------------------------------------------
# Elevation crossings
# ---------------------------------------------------------------------------------------------


def _elevation(observer: Observer, tz: tzinfo, at: datetime) -> float:
    """astral 2.x reads the calendar date from the datetime's own zone and the time of day from
    UTC, so the value depends on how an instant is written. The reference installation writes
    every instant in its local zone; so does this, on purpose, until the library is upgraded by a
    dated amendment."""
    return float(elevation(observer, at.astimezone(tz)))


def _crossing(
    observer: Observer,
    tz: tzinfo,
    start: datetime,
    end: datetime,
    target_deg: float,
    rising: bool,
    coarse_steps: int,
    tol_secs: float,
) -> datetime | None:
    """The instant the sun's elevation crosses ``target_deg`` between ``start`` and ``end``.

    A coarse scan finds the bracketing step, a bisection tightens it to ``tol_secs``. ``None``
    when the sun never crosses the angle in the window (polar summer or winter). Arithmetic runs
    on UTC instants; only the elevation lookup sees local time.
    """
    if start >= end:
        return None
    dt0 = start
    e0 = _elevation(observer, tz, dt0) - target_deg
    step = (end - start) / coarse_steps
    found: tuple[datetime, datetime] | None = None
    for _ in range(coarse_steps):
        dt1 = dt0 + step
        e1 = _elevation(observer, tz, dt1) - target_deg
        cond = (e0 < 0 <= e1) if rising else (e0 > 0 >= e1)
        if cond:
            found = (dt0, dt1)
            break
        dt0, e0 = dt1, e1
    if found is None:
        return None
    a, b = found
    while (b - a).total_seconds() > tol_secs:
        m = a + (b - a) / 2
        em = _elevation(observer, tz, m) - target_deg
        if (em >= 0 and rising) or (em <= 0 and not rising):
            b = m
        else:
            a = m
    return b


# ---------------------------------------------------------------------------------------------
# One solar day
# ---------------------------------------------------------------------------------------------


def solar_date(site: Site, at: datetime) -> date:
    """The calendar date of the solar day containing ``at``: between clock midnight and solar
    midnight that is still yesterday."""
    local = at.astimezone(site.tz)
    today_midnight = midnight(site.observer(), date=local.date(), tzinfo=site.tz)
    if local < today_midnight:
        return local.date() - timedelta(days=1)
    return local.date()


def compute_day(site: Site, day: date, params: SolarParams = DEFAULT_PARAMS) -> DayAnchors:
    """The real anchors of one solar day.

    Solar midnight and noon come from the library; every other anchor is an elevation crossing
    found by scan and bisection, and each degrades to a backbone time (solar midnight, noon or
    the next midnight) when the sun never reaches its angle, so the set is always complete. The
    reference installation's calls to the library's own sunrise, sunset, dawn and dusk fail under
    its runtime and fall back to exactly these crossings, so the crossings are the oracle and the
    event calls are not made.
    """
    obs = site.observer()
    tz = site.tz
    mid = midnight(obs, date=day, tzinfo=tz).astimezone(UTC)
    next_mid = midnight(obs, date=day + timedelta(days=1), tzinfo=tz).astimezone(UTC)
    noon_t = noon(obs, date=day, tzinfo=tz).astimezone(UTC)

    def cross_morn(angle: float) -> datetime | None:
        return _crossing(
            obs,
            tz,
            mid,
            noon_t,
            angle,
            True,
            params.crossing_coarse_steps,
            params.crossing_tolerance_s,
        )

    def cross_even(angle: float) -> datetime | None:
        return _crossing(
            obs,
            tz,
            noon_t,
            next_mid,
            angle,
            False,
            params.crossing_coarse_steps,
            params.crossing_tolerance_s,
        )

    astro_dawn = cross_morn(params.astronomical_depression) or mid
    naut_dawn = cross_morn(params.nautical_depression) or mid
    civil_dawn = cross_morn(params.civil_depression) or mid
    sunrise_t = cross_morn(params.sun_horizon) or noon_t
    blue_am_start = civil_dawn
    blue_am_end = cross_morn(params.blue_hour_top) or noon_t
    gold_am_start = blue_am_end
    gold_am_end = cross_morn(params.golden_hour_top) or noon_t

    gold_pm_start = cross_even(params.golden_hour_top) or noon_t
    gold_pm_end = cross_even(params.blue_hour_top) or next_mid
    blue_pm_start = gold_pm_end
    blue_pm_end = cross_even(params.civil_depression) or next_mid
    sunset_t = cross_even(params.sun_horizon) or noon_t
    civil_dusk = blue_pm_end
    naut_dusk = cross_even(params.nautical_depression) or next_mid
    astro_dusk = cross_even(params.astronomical_depression) or next_mid

    def lerp(a: datetime, b: datetime, t: float) -> datetime:
        return a + (b - a) * t

    am_e, am_m, am_l = params.am_shoulders
    pm_e, pm_m, pm_l = params.pm_shoulders
    at = {
        "midnight": mid,
        "next_midnight": next_mid,
        "noon": noon_t,
        "astronomical_dawn": astro_dawn,
        "nautical_dawn": naut_dawn,
        "civil_dawn": civil_dawn,
        "sunrise": sunrise_t,
        "golden_hour_am_start": gold_am_start,
        "golden_hour_am_end": gold_am_end,
        "blue_hour_am_start": blue_am_start,
        "blue_hour_am_end": blue_am_end,
        "sunset": sunset_t,
        "civil_dusk": civil_dusk,
        "nautical_dusk": naut_dusk,
        "astronomical_dusk": astro_dusk,
        "golden_hour_pm_start": gold_pm_start,
        "golden_hour_pm_end": gold_pm_end,
        "blue_hour_pm_start": blue_pm_start,
        "blue_hour_pm_end": blue_pm_end,
        "am_early": lerp(gold_am_end, noon_t, am_e),
        "am_mid": lerp(gold_am_end, noon_t, am_m),
        "am_late": lerp(gold_am_end, noon_t, am_l),
        "pm_early": lerp(noon_t, gold_pm_start, pm_e),
        "pm_mid": lerp(noon_t, gold_pm_start, pm_m),
        "pm_late": lerp(noon_t, gold_pm_start, pm_l),
    }
    return DayAnchors(day=day, at=at, day_length=sunset_t - sunrise_t)


def reference_day(
    site: Site, today: DayAnchors, params: SolarParams = DEFAULT_PARAMS
) -> DayAnchors:
    """The solstice the normalisation pulls toward, shifted so its noon coincides with today's."""
    year = today.day.year
    solstice = date(year, 6, 21) if site.latitude >= 0 else date(year, 12, 21)
    raw = compute_day(site, solstice, params)
    shift = today["noon"] - raw["noon"]
    return DayAnchors(
        day=raw.day, at={k: v + shift for k, v in raw.at.items()}, day_length=raw.day_length
    )


def is_dst(at: datetime) -> bool:
    """Whether daylight-saving time is in force at ``at`` in its own zone."""
    offset = at.dst()
    return offset is not None and offset != timedelta(0)


def normalize(
    today: DayAnchors,
    reference: DayAnchors,
    dst_in_force: bool,
    params: SolarParams = DEFAULT_PARAMS,
) -> DayAnchors:
    """Warp today's anchors toward the reference day's by the blend fraction.

    Noon is pinned. The evening chain (noon, sunset, civil dusk, nautical dusk, astronomical dusk,
    next midnight) and the morning chain walk outward from noon, each segment's duration blended
    between today's and the reference's. Outside daylight-saving time an hour is added to the
    reference's noon-to-sunset segment and taken from noon-to-sunrise, so the warped evening lands
    on the same wall-clock feel year round. Secondary anchors are mapped by their proportional
    position inside the backbone segment that contains them.
    """
    blend = params.solstice_blend_frac
    correction = 0.0 if dst_in_force else 3600.0
    norm: dict[str, datetime] = {"noon": today["noon"]}

    for i in range(len(PM_CHAIN) - 1):
        cur, nxt = PM_CHAIN[i], PM_CHAIN[i + 1]
        dur_today = (today[nxt] - today[cur]).total_seconds()
        dur_ref = (reference[nxt] - reference[cur]).total_seconds()
        if cur == "noon" and nxt == "sunset":
            dur_ref += correction
        norm[nxt] = norm[cur] + timedelta(seconds=dur_today + (dur_ref - dur_today) * blend)

    for i in range(len(AM_CHAIN) - 1):
        cur, nxt = AM_CHAIN[i], AM_CHAIN[i + 1]
        dur_today = (today[cur] - today[nxt]).total_seconds()
        dur_ref = (reference[cur] - reference[nxt]).total_seconds()
        if cur == "noon" and nxt == "sunrise":
            dur_ref -= correction
        norm[nxt] = norm[cur] - timedelta(seconds=dur_today + (dur_ref - dur_today) * blend)

    def map_time(t: datetime, chain: tuple[str, ...]) -> datetime:
        for i in range(len(chain) - 1):
            k1, k2 = chain[i], chain[i + 1]
            t1, t2 = today[k1], today[k2]
            if (t1 <= t <= t2) or (t2 <= t <= t1):
                total = (t2 - t1).total_seconds()
                if total == 0:
                    return norm[k1]
                p = (t - t1).total_seconds() / total
                return norm[k1] + (norm[k2] - norm[k1]) * p
        return t

    for key in SECONDARY_KEYS:
        chain = AM_CHAIN if today[key] < today["noon"] else PM_CHAIN
        norm[key] = map_time(today[key], chain)

    return DayAnchors(day=today.day, at=norm, day_length=norm["sunset"] - norm["sunrise"])


# ---------------------------------------------------------------------------------------------
# Fractions of the day
# ---------------------------------------------------------------------------------------------


def clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > PROGRESS_CEILING:
        return PROGRESS_CEILING
    return x


def fraction_between(start: datetime, end: datetime, at: datetime) -> float | None:
    span = (end - start).total_seconds()
    if span <= 0:
        return None
    return clamp01((at - start).total_seconds() / span)


def day_progress(anchors: DayAnchors, at: datetime) -> float | None:
    """Where ``at`` falls in the day, 0 at solar midnight, never reaching 1."""
    return fraction_between(anchors["midnight"], anchors["next_midnight"], at)


def anchor_percents(anchors: DayAnchors) -> dict[str, float | None]:
    """Each anchor as a fraction of the day, the form the render's curve tables key on."""
    start, end = anchors["midnight"], anchors["next_midnight"]
    return {k: fraction_between(start, end, anchors[k]) for k in PERCENT_KEYS}


def daylight_factor(anchors: DayAnchors, at: datetime) -> float:
    """A sine hump from the end of the morning golden hour to the start of the evening one,
    peaking at noon: 0 at night and in the golden hours, 1 at noon."""
    start, end = anchors["midnight"], anchors["next_midnight"]
    p_now = fraction_between(start, end, at)
    p_start = fraction_between(start, end, anchors["golden_hour_am_end"])
    p_noon = fraction_between(start, end, anchors["noon"])
    p_end = fraction_between(start, end, anchors["golden_hour_pm_start"])
    if p_now is None or p_start is None or p_noon is None or p_end is None:
        return 0.0
    if p_now < p_start or p_now > p_end:
        return 0.0
    if p_now < p_noon:
        if p_noon == p_start:
            return 1.0
        return sin((p_now - p_start) / (p_noon - p_start) * (pi / 2))
    if p_end == p_noon:
        return 1.0
    return cos((p_now - p_noon) / (p_end - p_noon) * (pi / 2))


def ready(anchors: DayAnchors) -> bool:
    return all(k in anchors.at for k in READY_KEYS)


# ---------------------------------------------------------------------------------------------
# Where the sun is
# ---------------------------------------------------------------------------------------------


def position(site: Site, at: datetime) -> SunPosition:
    """Elevation and azimuth at ``at``, the elevation thirty minutes earlier (the mid-window
    convention the ambient layer uses), and the declination and equation of time."""
    obs = site.observer()
    local = at.astimezone(site.tz)
    utc = local.astimezone(UTC)
    return SunPosition(
        elevation=_elevation(obs, site.tz, at),
        elevation_mid=_elevation(obs, site.tz, at - timedelta(minutes=30)),
        azimuth=azimuth(obs, local),
        declination=declination(utc),
        equation_of_time=equation_of_time(utc),
    )
