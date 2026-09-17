"""Report cadence: how often a fixture reports a moving colour, and how late the host can be.

A drift comparator that judges a fixture against its commanded colour must not judge it
before the command's transition has elapsed AND the device has had a chance to report the
result. The transition is known from the command; the second half is a device property the
transport does not declare (on the reference installation Zigbee2MQTT configures level and
on/off reporting only, and the colour cadence is the bulb firmware's own), so it is OBSERVED:
the fixture's own colour reports while a transition is in flight arrive at a steady interval,
and the median of those inter-report gaps is the fixture's report interval, R_f.

Given R_f and the time of the fixture's last report, the next report is predictable:
``max(fade end, last report + R_f)``. What the settling report lands beyond that prediction
is not a device property but the host's delivery jitter, J, derived home-wide as a high
quantile of the residual over every trip the comparator would otherwise have lost. The two
together give a consumer its arm: wait until the prediction plus J, unless the device
converges first.

This module is pure: fades and trips in, estimates out, with provenance. It never reads a
clock or a store. Must-decline cases (a fixture with too few gaps, no colour reports at all,
or gaps that do not cluster around one interval) yield ``none`` rather than a number, because
the consumer's failure direction under a missing value is harmless (it arms at the fade end,
as before) while a wrong value could arm it late for a genuinely stuck device. A fixture with
no observation of its own may carry a same-model fixture's value as ``inherited``: units of
one model label share firmware, so on the reference fleet every unit of a label measured the
same interval to a hundredth of a second, and a seed is exact until the fixture's own reports
confirm or, loudly, contradict it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Final, Literal

from pascl.model import HomeModel, fixture_model_label

#: A measured interval rests on at least this many gaps; fewer is declined. On the reference
#: installation a scening fixture produces ~150 gaps an evening, so this is one evening's
#: worth for an occasional fixture and an hour's for a busy one.
MIN_GAPS: Final = 20
#: A gap within this fraction of the median is on cadence.
CLUSTER_BAND: Final = 0.2
#: At least this fraction of gaps must be on cadence, or the gaps describe no single
#: interval (two report sources interleaved, a device that reports on change only).
CLUSTER_MIN: Final = 0.8
#: A report this long after the transition's end may still be the settling report and its
#: gap a cadence gap; later than that the device was already at rest and the gap says
#: nothing about its cadence.
FADE_SLACK: Final = 2.0
#: Units of one model label whose measured intervals differ by more than this fraction of
#: the smaller are not the same firmware, and the label seeds nothing.
SAME_MODEL_TOL: Final = 0.1
#: A residual quantile below which the consumer accepts a false arm; the 99th on the reference
#: installation is 3.9 s where the 90th is 0.1 s, the tail being host stalls, not devices.
JITTER_QUANTILE: Final = 0.99
#: Fewer trips than this and the jitter is declined (a quantile of a handful is a guess).
MIN_TRIPS: Final = 20

Source = Literal["measured", "inherited", "none"]


@dataclass(frozen=True)
class Fade:
    """One transition a fixture was commanded through, and when it reported new colours.

    ``report_times`` are seconds after the command (or the cache write that stands for it),
    ascending, one per device report that carried a colour DIFFERENT from the previous
    report's: the transport's echo of the command and a brightness report that repeats the
    last colour are not colour reports and must not be here. Reports before the command
    (negative times) are allowed and ignored.
    """

    fixture: str
    transition_s: float
    report_times: tuple[float, ...]


@dataclass(frozen=True)
class ReportInterval:
    fixture: str
    seconds: float | None
    source: Source
    gaps: int = 0
    """Gaps the estimate rests on (0 unless measured)."""
    on_cadence: float | None = None
    """Fraction of gaps within ``CLUSTER_BAND`` of the median (measured only)."""
    p90_s: float | None = None
    """The 90th percentile gap: the interval a phase prediction should be read against."""
    inherited_from: str | None = None
    """The measured fixture whose value this one carries (inherited only)."""
    note: str | None = None
    """Why there is no measured value, or what the measurement noticed."""


@dataclass(frozen=True)
class Trip:
    """A trip the comparator would have lost: its sensor armed before the settling report.

    All times in seconds on one fade's clock. ``fade_end_s`` is the command's transition end;
    ``last_report_s`` the fixture's most recent report at the moment the sensor tripped
    (``tripped_s``); ``settled_s`` the first report inside tolerance of the target.
    """

    fixture: str
    fade_end_s: float
    tripped_s: float
    last_report_s: float
    settled_s: float


@dataclass(frozen=True)
class Jitter:
    seconds: float | None
    trips: int
    quantile: float = JITTER_QUANTILE
    residual_max_s: float | None = None
    note: str | None = None


@dataclass(frozen=True)
class Replay:
    """What a consumer arming at ``prediction + jitter`` would have done on the trips."""

    trips: int
    false_arms: int
    """Trips whose arm still came before the settling report."""
    wait_p50_s: float
    wait_p90_s: float
    wait_max_s: float
    arm_p50_s: float
    """When a genuinely stuck fixture would be acted on, from the fade's clock."""
    arm_max_s: float


def _quantile(values: Sequence[float], q: float) -> float:
    xs = sorted(values)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def _median(values: Sequence[float]) -> float:
    xs = sorted(values)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def gaps_of(fade: Fade, slack: float = FADE_SLACK) -> list[float]:
    """The fixture's inter-report gaps while it was moving: between consecutive colour
    reports after the command, the later of which lands by the transition's end plus
    ``slack``. The gap from a report BEFORE the command to the first one after is not a
    cadence gap (the device reports a first change at once when it has been idle)."""
    inside = [t for t in fade.report_times if 0 < t <= fade.transition_s + slack]
    return [b - a for a, b in pairwise(inside)]


def estimate(fades: Iterable[Fade]) -> dict[str, ReportInterval]:
    """Per fixture, the measured report interval or the reason there is none. A fixture
    appears when at least one fade names it, so a fixture with fades but no colour reports
    is declined explicitly (``none``, "no colour reports") rather than absent."""
    gaps: dict[str, list[float]] = {}
    reports: dict[str, int] = {}
    for fade in fades:
        gaps.setdefault(fade.fixture, []).extend(gaps_of(fade))
        reports[fade.fixture] = reports.get(fade.fixture, 0) + sum(
            1 for t in fade.report_times if t > 0
        )
    out: dict[str, ReportInterval] = {}
    for fixture in sorted(gaps):
        g = gaps[fixture]
        if reports[fixture] == 0:
            out[fixture] = ReportInterval(fixture, None, "none", note="no colour reports")
            continue
        if len(g) < MIN_GAPS:
            out[fixture] = ReportInterval(
                fixture, None, "none", gaps=len(g), note=f"too few gaps ({len(g)} < {MIN_GAPS})"
            )
            continue
        med = _median(g)
        on = sum(1 for x in g if abs(x - med) <= CLUSTER_BAND * med) / len(g)
        if on < CLUSTER_MIN:
            out[fixture] = ReportInterval(
                fixture,
                None,
                "none",
                gaps=len(g),
                on_cadence=on,
                note=(
                    f"gaps do not cluster ({on:.0%} within {CLUSTER_BAND:.0%} "
                    f"of the median {med:.2f} s)"
                ),
            )
            continue
        out[fixture] = ReportInterval(
            fixture,
            round(med, 3),
            "measured",
            gaps=len(g),
            on_cadence=round(on, 3),
            p90_s=round(_quantile(g, 0.9), 3),
        )
    return out


def inherit(
    measured: Mapping[str, ReportInterval],
    labels: Mapping[str, str | None],
    fixtures: Iterable[str],
) -> dict[str, ReportInterval]:
    """Every fixture in ``fixtures`` with a value: its own measurement when it has one, else
    the median of the measured units of its model label as ``inherited`` (the first such
    unit in fixture order is named as the source), else ``none``. A label whose measured
    units disagree beyond ``SAME_MODEL_TOL`` seeds nothing and says so."""
    by_label: dict[str, list[ReportInterval]] = {}
    for f, r in measured.items():
        label = labels.get(f)
        if r.source == "measured" and r.seconds is not None and label:
            by_label.setdefault(label, []).append(r)
    out: dict[str, ReportInterval] = {}
    for f in fixtures:
        own = measured.get(f)
        if own is not None and own.source == "measured":
            out[f] = own
            continue
        label = labels.get(f)
        units = by_label.get(label or "", [])
        if not units:
            note = (
                own.note if own is not None else "no observation, and no measured unit of its model"
            )
            out[f] = ReportInterval(f, None, "none", gaps=own.gaps if own else 0, note=note)
            continue
        values = [u.seconds for u in units if u.seconds is not None]
        lo, hi = min(values), max(values)
        if hi - lo > SAME_MODEL_TOL * lo:
            out[f] = ReportInterval(
                f,
                None,
                "none",
                gaps=own.gaps if own else 0,
                note=f"measured units of model {label!r} disagree ({lo:.2f}..{hi:.2f} s); no seed",
            )
            continue
        source = sorted(units, key=lambda u: u.fixture)[0]
        out[f] = ReportInterval(
            f,
            round(_median(values), 3),
            "inherited",
            inherited_from=source.fixture,
            note=(own.note if own is not None and own.note else None),
        )
    return out


def labels_of(model: HomeModel) -> dict[str, str | None]:
    """fixture -> material model label, for ``inherit`` from a Home Model."""
    return {
        fid: fixture_model_label(model, fid)
        for room in model.rooms.values()
        for fid in room.fixtures
    }


def predict(trip: Trip, interval: float | None) -> float:
    """When the settling report is due: the fade's end, or the fixture's next report if
    that is later. An unknown interval predicts the fade end alone."""
    if interval is None:
        return trip.fade_end_s
    return max(trip.fade_end_s, trip.last_report_s + interval)


def residuals(trips: Iterable[Trip], intervals: Mapping[str, ReportInterval]) -> list[float]:
    """How late each settling report landed beyond its prediction (never below 0)."""
    out: list[float] = []
    for t in trips:
        r = intervals.get(t.fixture)
        out.append(max(0.0, t.settled_s - predict(t, r.seconds if r else None)))
    return out


def jitter(
    trips: Sequence[Trip],
    intervals: Mapping[str, ReportInterval],
    quantile: float = JITTER_QUANTILE,
) -> Jitter:
    """The home's delivery jitter: the ``quantile`` of the residuals over the trips, or
    declined below ``MIN_TRIPS``."""
    if len(trips) < MIN_TRIPS:
        return Jitter(
            None, len(trips), quantile, note=f"too few trips ({len(trips)} < {MIN_TRIPS})"
        )
    res = residuals(trips, intervals)
    return Jitter(round(_quantile(res, quantile), 3), len(trips), quantile, round(max(res), 3))


def replay(
    trips: Sequence[Trip],
    intervals: Mapping[str, ReportInterval],
    jitter_s: float | None,
) -> Replay:
    """What arming at ``prediction + jitter`` does on these trips: how many still arm before
    the device converges, how long the consumer waits, and when a stuck fixture is acted on.
    A ``None`` jitter contributes nothing (the zero-init the consumer must survive)."""
    j = jitter_s or 0.0
    waits: list[float] = []
    arms: list[float] = []
    false_arms = 0
    for t in trips:
        r = intervals.get(t.fixture)
        arm = predict(t, r.seconds if r else None) + j
        waits.append(max(0.0, arm - t.tripped_s))
        arms.append(max(arm, t.tripped_s))
        if max(arm, t.tripped_s) < t.settled_s:
            false_arms += 1
    if not trips:
        return Replay(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0)
    return Replay(
        len(trips),
        false_arms,
        round(_quantile(waits, 0.5), 2),
        round(_quantile(waits, 0.9), 2),
        round(max(waits), 2),
        round(_quantile(arms, 0.5), 2),
        round(max(arms), 2),
    )


def stands_down(
    transition_s: float, linger_s: float, interval: float | None, jitter_s: float | None
) -> bool:
    """True when a consumer arming at ``fade end + R_f + J`` can never complete before the
    next command re-arms it: the fixture is re-commanded every ``linger_s`` and its arm is
    longer than that, so the comparator stands down on it while it scenes. Correct rather
    than a constraint (a repaint would only ever interrupt a fade), but worth naming per
    fixture so nobody reads silence as health."""
    return transition_s + (interval or 0.0) + (jitter_s or 0.0) >= linger_s - 1.0
