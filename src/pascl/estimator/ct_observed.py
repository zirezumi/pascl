"""The colour-temperature range OBSERVED in use (docs/gamut.md section 11, the third tier).

A device that clips a colour temperature somewhere its attributes do not show may still say
where it landed through some channel: a native state push carrying the emitter's value, a
bridge, a firmware that clamps the attribute on the way out. Any such channel yields
(commanded, reported) pairs in mireds, and this estimator takes the floor as the value the
device reports when commanded warmer than it, consistently, and the ceiling likewise at the
cool end. It is transport-agnostic: a shell gathers the pairs, this module judges them.

It declines PER END, as a first-class answer (DERIVED_PARAMETERS P2), and says which kind of
decline it is: no pair shows a clip at that end (the commands never went past it, or the
channel echoes the command: the pairs cannot tell those apart, so the reason names the
warmest or coolest command the device repeated); too few pairs show it (``MIN_SUPPORT``);
the clipped reports disagree (``SPREAD_TOL``); reports contradict the candidate (a report
warmer than a supposed floor, more than ``CONTRADICTION_RATIO`` of the support); or the
value is not one an emitter could have (``CREDIBLE_KELVIN``). Every threshold is a ratio (P3),
authority counts only the pairs that inform the end (P7), and an end that is published
carries its support and its contradictions with it (P10).

On the reference installation every channel repeats the stored command for the 22 bulbs
that needed this tier (the attribute is stored unclipped and the native push carries the
same attribute), so its real-data fixture in the tests is a decline of the first kind, and
it must stay one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from statistics import median
from typing import Final, Literal

from pascl.core.color import kelvin_to_mired, mired_to_kelvin
from pascl.model.schema import CREDIBLE_KELVIN, KELVIN_MAX, KELVIN_MIN, CtDecline, Gamut

#: A report within this ratio of its command is the command: a host truncates kelvin to mired
#: and back (1 mired at 500), a device quantises its own value by about as much.
AGREE_TOL: Final = 0.01
#: Pairs showing the clip, agreeing with the median, before an end is published.
MIN_SUPPORT: Final = 5
#: The clipped reports must sit within this ratio of their median, or they disagree.
SPREAD_TOL: Final = 0.02
#: Reports past the candidate (warmer than a floor, cooler than a ceiling) tolerated, as a
#: fraction of the support: a stale echo or two, never a pattern.
CONTRADICTION_RATIO: Final = 0.1

Pair = tuple[int, int]
"""(commanded, reported) colour temperature, both in mireds, for one settled command."""

End = Literal["floor", "ceiling"]


@dataclass(frozen=True)
class EndVerdict:
    """What the pairs say about one end of the range."""

    end: End
    kelvin: int | None
    """The end, kelvin, when published; None when declined."""
    mired: int | None
    """The same value in mireds (the median of the clipped reports)."""
    support: int
    """Pairs that show the clip at this end and agree with the median (the authority)."""
    disagreeing: int
    """Pairs that show a clip at this end but not at the median."""
    contradictions: int
    """Reports past the candidate: warmer than a floor, cooler than a ceiling."""
    reason: str | None
    """Why the end was declined; None when published."""
    proof: bool = False
    """A decline that proves this channel cannot bound the end: the device was commanded
    past any emitter's end (beyond ``CREDIBLE_KELVIN``) and reported the command back, so
    the channel repeats what it is sent. Every other decline is inconclusive: the end was
    not exercised, or the reports were too few or too noisy to judge."""

    @property
    def published(self) -> bool:
        return self.kelvin is not None

    @property
    def verdict(self) -> str:
        """``published``, ``unobservable`` (a proven decline) or ``inconclusive``."""
        if self.published:
            return "published"
        return "unobservable" if self.proof else "inconclusive"


@dataclass(frozen=True)
class ObservedCt:
    floor: EndVerdict
    ceiling: EndVerdict
    pairs: int
    commanded_mired: tuple[int, int] | None
    """The span of commands seen, (coolest, warmest) mireds: how much of the range the
    evidence exercised (P1). None without pairs."""

    @property
    def range_k(self) -> tuple[int | None, int | None]:
        return (self.floor.kelvin, self.ceiling.kelvin)


def _clips_at(pair: Pair, end: End, tol: float) -> bool:
    """Whether the device answered this command on the near side of the end: cooler than a
    warm command (floor), warmer than a cool one (ceiling)."""
    commanded, reported = pair
    if reported <= 0 or commanded <= 0:
        return False
    if end == "floor":
        return commanded - reported > tol * reported
    return reported - commanded > tol * reported


def _past(reported: int, candidate: int, end: End, tol: float) -> bool:
    """A report on the far side of the candidate end, where nothing should be."""
    if end == "floor":
        return reported - candidate > tol * candidate
    return candidate - reported > tol * candidate


def _beyond_any_emitter(extreme: int, end: End) -> bool:
    """Whether a command lies past the end of the credible band: warmer than any emitter's
    floor, or cooler than any emitter's ceiling. A device reporting such a command as its
    own has proven its channel repeats what it is sent."""
    if end == "floor":
        return extreme > kelvin_to_mired(CREDIBLE_KELVIN[0])
    return extreme < kelvin_to_mired(CREDIBLE_KELVIN[1])


def _no_clip(pairs: Sequence[Pair], end: End) -> tuple[str, bool]:
    """The decline for an end no pair shows a clip at, and whether it is a proof. Inside the
    credible band the pairs cannot tell an unexercised end from a channel that repeats the
    command, so the reason names both; past it, the channel has repeated a command no
    emitter could show, which proves it."""
    if not pairs:
        return "no pairs", False
    extreme = max(c for c, _ in pairs) if end == "floor" else min(c for c, _ in pairs)
    side = "warmest" if end == "floor" else "coolest"
    if _beyond_any_emitter(extreme, end):
        return (
            f"the channel repeats the command: the {side} command, {extreme} mired "
            f"({mired_to_kelvin(extreme)} K), lies past any emitter's {end} and was reported "
            f"as commanded",
            True,
        )
    return (
        f"no clip observed at the {end}: the {side} command, {extreme} mired "
        f"({mired_to_kelvin(extreme)} K), was reported as commanded; either the {end} lies "
        f"beyond it or the channel repeats the command",
        False,
    )


def _judge_end(
    pairs: Sequence[Pair],
    end: End,
    *,
    agree_tol: float,
    min_support: int,
    spread_tol: float,
    contradiction_ratio: float,
) -> EndVerdict:
    clipped = [p for p in pairs if _clips_at(p, end, agree_tol)]
    if not clipped:
        why, proof = _no_clip(pairs, end)
        return EndVerdict(end, None, None, 0, 0, 0, why, proof)
    candidate = round(median(r for _, r in clipped))
    support = sum(1 for _, r in clipped if abs(r - candidate) <= spread_tol * candidate)
    disagreeing = len(clipped) - support
    contradictions = sum(1 for _, r in pairs if _past(r, candidate, end, agree_tol))
    kelvin = mired_to_kelvin(candidate)
    beyond = sum(1 for c, _ in pairs if _past(c, candidate, end, agree_tol))
    reason: str | None = None
    if support < min_support:
        reason = (
            f"{support} pair(s) show a clip at {candidate} mired ({kelvin} K) among {beyond} "
            f"commanded past it; {min_support} are needed"
        )
    elif disagreeing > contradiction_ratio * support:
        reason = (
            f"the clipped reports disagree: {support} at {candidate} mired, {disagreeing} "
            f"elsewhere (more than {contradiction_ratio:g} of the support)"
        )
    elif contradictions > contradiction_ratio * support:
        past = "warmer than" if end == "floor" else "cooler than"
        reason = (
            f"{contradictions} report(s) {past} the candidate {end} of {candidate} mired "
            f"({kelvin} K) against {support} supporting it"
        )
    elif not CREDIBLE_KELVIN[0] <= kelvin <= CREDIBLE_KELVIN[1]:
        reason = (
            f"a {end} of {kelvin} K is not one an emitter could have "
            f"(outside {CREDIBLE_KELVIN[0]}-{CREDIBLE_KELVIN[1]} K)"
        )
    if reason is not None:
        # A handful of stray reports differing from their command, against hundreds commanded
        # past any emitter's end and reported as commanded, is the echoing channel again.
        extreme = max(c for c, _ in pairs) if end == "floor" else min(c for c, _ in pairs)
        proof = (
            support < min_support
            and _beyond_any_emitter(extreme, end)
            and contradictions > contradiction_ratio * len(pairs)
        )
        if proof:
            reason = (
                f"the channel repeats the command: {contradictions} report(s) past any "
                f"candidate against {support} supporting one, the extreme command {extreme} "
                f"mired ({mired_to_kelvin(extreme)} K) past any emitter's {end}"
            )
        return EndVerdict(end, None, candidate, support, disagreeing, contradictions, reason, proof)
    return EndVerdict(end, kelvin, candidate, support, disagreeing, contradictions, None)


def observe(
    pairs: Sequence[Pair],
    *,
    agree_tol: float = AGREE_TOL,
    min_support: int = MIN_SUPPORT,
    spread_tol: float = SPREAD_TOL,
    contradiction_ratio: float = CONTRADICTION_RATIO,
) -> ObservedCt:
    """Judge both ends from the pairs (mireds, (commanded, reported), one per settled
    command). Pure: the same pairs always give the same verdict."""
    good = [(c, r) for c, r in pairs if c > 0 and r > 0]
    span = (min(c for c, _ in good), max(c for c, _ in good)) if good else None

    def judge(end: End) -> EndVerdict:
        return _judge_end(
            good,
            end,
            agree_tol=agree_tol,
            min_support=min_support,
            spread_tol=spread_tol,
            contradiction_ratio=contradiction_ratio,
        )

    return ObservedCt(
        floor=judge("floor"), ceiling=judge("ceiling"), pairs=len(good), commanded_mired=span
    )


#: The tiers an observed end may replace: anything weaker than the device's own clipped
#: attribute. A probed end stands, and an observation that disagrees with it is a note.
REPLACEABLE: Final = (None, "declared", "inherited")


def apply(gamut: Gamut, verdict: ObservedCt) -> tuple[Gamut, list[str]]:
    """The gamut with each published end recorded as ``observed`` where the end it holds came
    from a weaker tier (``REPLACEABLE``), each declined end recorded in ``ct_declined`` with
    the tier's verdict (replacing the observed tier's earlier record for that end), and
    notes for what was kept or declined. An end the gamut had no range for is filled in with
    the other end unbounded (the span end), which :func:`pascl.model.fixture_cct_range`
    reads as nothing known."""
    notes: list[str] = []
    lo, hi = gamut.ct_range_k if gamut.ct_range_k is not None else (KELVIN_MIN, KELVIN_MAX)
    lo_src, hi_src = gamut.ct_sources
    declined = [d for d in gamut.ct_declined if d.tier != "observed"]
    for i, ev in enumerate((verdict.floor, verdict.ceiling)):
        if not ev.published or ev.kelvin is None:
            notes.append(f"{ev.end}: {ev.verdict}: {ev.reason}")
            if (lo_src, hi_src)[i] != "observed":
                declined.append(
                    CtDecline(
                        "observed",
                        ev.end,
                        "unobservable" if ev.proof else "inconclusive",
                        ev.reason or "declined",
                    )
                )
            continue
        current_src = (lo_src, hi_src)[i]
        current = (lo, hi)[i]
        if current_src not in REPLACEABLE:
            if current != ev.kelvin:
                notes.append(
                    f"{ev.end}: observed {ev.kelvin} K ({ev.support} pairs) but the "
                    f"{current_src} value {current} K stands"
                )
            continue
        if i == 0:
            lo, lo_src = ev.kelvin, "observed"
        else:
            hi, hi_src = ev.kelvin, "observed"
        notes.append(
            f"{ev.end}: observed {ev.kelvin} K from {ev.support} pair(s)"
            + (f", {ev.contradictions} contradicting" if ev.contradictions else "")
        )
    record = tuple(declined)
    if lo_src is None and hi_src is None:
        return replace(gamut, ct_declined=record), notes
    if not lo < hi:
        notes.append(f"observed ends cross ({lo} K floor, {hi} K ceiling); nothing recorded")
        return replace(gamut, ct_declined=record), notes
    return (
        replace(gamut, ct_range_k=(lo, hi), ct_sources=(lo_src, hi_src), ct_declined=record),
        notes,
    )
