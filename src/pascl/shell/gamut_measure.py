"""Run a gamut measurement over a device channel.

The impure half of ``pascl.estimator.gamut``: the protocol there decides which colours to
command and what the answers add up to; this module commands them, waits for the device, and
applies the timing rules a real device needs. It talks to the device through a small
:class:`DeviceChannel`, so a transport adapter (``pascl.shell.z2m``) and a test double look the
same to it, and the only time it reads is the injected :class:`Clock`.

Timing rules, all measured on the reference installation's fleet: a device applies a zero
transition at once but may report the change late or never, so ``READ_AFTER`` seconds after
the command the channel is asked to read the value back, again every ``READ_RETRY`` while the
device has not answered or answers with the colour it showed before the command (the channel
passes that on as ``unapplied``). An answer is taken only once a second read agrees with it:
a device with a ramp of its own answers a first read part-way and the two disagree, and the
driver keeps reading until two in a row agree or the sample runs out, in which case it has no
answer. A spontaneous report is a fallback that starts a ``SETTLE`` window in which a later
value supersedes it; ``ANSWER_TIMEOUT`` bounds the whole sample; a foreign command on the
device's own topic during a sample invalidates it, and it is taken once more.

Three things a device can do on its very first samples are diagnosed rather than endured for
a whole measurement, each with its reason in the verdict: keep its resting colour under every
command (it does not apply colour while off; in the forced mode the driver switches it on and
starts over, otherwise it says so and stops), answer nothing at all (not reachable this way),
or still be changing colour when the sample runs out (a ramp too slow to measure).

After the polygon, the colour-temperature range: the limits the device declares
(``colorTempPhysicalMin`` / ``Max``, one read, ``read_ct_limits``) and two probes past any real
device's ends (``CT_PROBES_MIRED``), commanded and read back under the same timing rules
(``take_ct_sample``); credible probe answers are where the device clips, credible declared
limits are its own word when it clips nothing, and anything else is declined
(``ct_range_from``). A material without a colour temperature is not probed (``ct=False``), and
``xy=False`` measures the range alone.

A measured fixture holds a probe colour for as long as a sample lasts, and a fixture that is
turned on then shows that colour. Three things end a measurement early, all of them meant to
put the fixture back to its intended colour BEFORE a turn-on can show anything, and all of them
withhold the polygon: the room's presence flipping on (the channel's ``occupied``
observation, ahead of the render that will follow it), a command that turns the fixture on
(``lit``, from a foreign ``state: ON`` on the device or its groups, or from the device's own
state report), and whatever the caller's ``abort_when`` watches. The restore on an abort snaps
with no transition. A turn-on that gives no warning at all (a switch bound straight to the
bulb) still shows the probe colour until the state report arrives, a few hundred milliseconds;
keeping samples short is what bounds that.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Final, Literal, Protocol

from pascl.clock import Clock, SystemClock
from pascl.core.gamut import XY
from pascl.estimator.gamut import (
    CT_PROBES_MIRED,
    Probe,
    Verdict,
    credible,
    ct_range_from,
    is_echo,
    kelvin_range,
)
from pascl.model import Gamut

#: Seconds after the command before the first read-back: the device applies a zero transition
#: within a Zigbee round trip, and every device tried answered a read in under 0.1 s.
READ_AFTER: Final = 0.4
#: Seconds between read-backs while the device has not answered (or answered with the colour
#: it showed before the command, i.e. had not applied it yet), and between two that disagree.
READ_RETRY: Final = 0.4
#: Read-backs that go unanswered before the driver stops asking within a sample.
MAX_READS: Final = 3
#: Read-backs in one sample while the device does answer but has not applied the command or
#: has not settled: enough to span ANSWER_TIMEOUT at READ_RETRY.
MAX_SETTLE_READS: Final = 7
ANSWER_TIMEOUT: Final = 3.0
SETTLE: Final = 1.5
PAUSE: Final = 0.15
RETAKE_PAUSE: Final = 1.0
POLL: Final = 0.1
#: The shortest wait a channel is asked for; a real link has nothing useful to say sooner.
MIN_WAIT: Final = 0.05
#: Consecutive opening samples of one kind before the driver concludes something about the
#: device: consecutive probes clip to different places, so a device that answers its resting
#: colour twice running has not applied either command.
DIAGNOSE_AFTER: Final = 2

#: The abort reasons the driver diagnoses, as prefixes of ``Verdict.aborted``.
NOT_APPLIED_WHILE_OFF: Final = "colour is not applied while the fixture is off"
NOT_APPLIED_LIT: Final = "colour is not applied even with the fixture on"
NO_READ_ANSWER: Final = "no answer to a colour read-back"
NEVER_SETTLES: Final = "still changing colour when the sample ran out"


@dataclass(frozen=True)
class Observation:
    """One thing the channel saw: the device's own colour, the transport's echo of a command,
    a read-back that still shows the colour from before the command (``unapplied``), a
    command on this device's topic that the measurement did not send, the fixture being or
    reporting itself ON, or the room it lights becoming occupied."""

    kind: Literal["device", "echo", "unapplied", "foreign", "lit", "occupied"]
    xy: XY | None = None
    trusted: bool = False
    """The channel read this value back from the device, so it is the device's own even when
    it equals the command (the device reached it); an untrusted value equal to the command is
    taken for the echo."""
    mired: int | None = None
    """The device's colour temperature, for an observation made under a colour-temperature
    command (``command_ct``); ``xy`` is then None."""
    ct_limits: tuple[int, int] | None = None
    """The colour-temperature limits the device declares (coolest, warmest; mireds), answering
    ``read_ct_limits``."""


class DeviceChannel(Protocol):
    """What a transport must offer to measure one fixture."""

    @property
    def identity(self) -> str | None:
        """The transport's identity for the device, bound into the measurement."""

    @property
    def firmware(self) -> str | None:
        """The device's firmware as the transport reports it; None when it cannot tell."""

    @property
    def needs_lit(self) -> bool:
        """True when the transport cannot carry a colour to a dark fixture (its command
        would turn the fixture on), so the fixture is measured in the forced mode only."""

    def is_lit(self) -> bool | None:
        """Whether the fixture is on; None when the transport cannot tell."""

    def command(self, xy: XY) -> None:
        """Command the colour with a zero transition."""

    def command_ct(self, mired: int) -> None:
        """Command a colour temperature with a zero transition; the observations that follow
        carry ``mired`` (see :func:`take_ct_sample`)."""

    def read(self) -> None:
        """Ask the device to report its current colour (a colour temperature comes with it)."""

    def read_ct_limits(self) -> None:
        """Ask the device for the colour-temperature limits it declares (its physical minimum
        and maximum, mireds); the answer is an observation carrying ``ct_limits``."""

    def observe(self, seconds: float) -> list[Observation]:
        """Block up to ``seconds`` and return what arrived, classified."""

    def light_up(self) -> bool:
        """Switch the fixture on so a device that keeps its colour while off can be measured
        visibly (the forced mode); ``restore`` then turns it off again. False when the
        transport cannot."""

    def restore(self, transition: float = 0.2) -> None:
        """Put the fixture back as it was before the measurement, over ``transition``
        seconds (zero on an abort: the intended colour must be there before a turn-on)."""


@dataclass(frozen=True)
class Sample:
    xy: XY | None
    echo_only: bool
    foreign: bool
    lit: bool = False
    trusted: bool = False
    """The value was read back from the device (see :class:`Observation`)."""
    occupied: bool = False
    reads: int = 0
    """How many read-backs went out in all, the confirming one included."""
    latency: float | None = None
    """Seconds from the first read-back to the first trusted answer; None without one."""
    retries: int = 0
    """Read-backs that went out before the first trusted answer, beyond the first: the device
    was slow to apply or to answer, the first sign of a transport with no airtime to spare."""
    confirmed: bool = False
    """A second read-back agreed with the answer."""
    moved: bool = False
    """Two read-backs in a row disagreed: the device was still changing colour. With ``xy``
    it settled afterwards; without, it never did within the sample."""
    unapplied: bool = False
    """Every answered read-back showed the colour from before the command and nothing else
    came: the device did not apply the command (it keeps its colour while off, or ignores
    the command)."""
    mired: int | None = None
    """The answer to a colour-temperature sample (:func:`take_ct_sample`); ``xy`` is None."""


def take_sample(
    channel: DeviceChannel,
    xy: XY,
    clock: Clock,
    previous: XY | None = None,
    *,
    interrupt: bool = True,
) -> Sample:
    """Command one colour and collect the device's answer under the timing rules. An
    untrusted value equal to ``previous`` (the last sample's answer) is the device's late
    report of the colour it showed before, not an answer to this command: it is set aside
    so the read-back still goes out. A trusted answer (a read-back) is confirmed by a second
    read-back sent at once: two that agree end the sample; two that disagree mean the device
    is still changing, and it is read again after ``READ_RETRY`` until two agree or the
    sample runs out with no answer. A read-back that still shows the colour from before the
    command is ``unapplied``; the driver reads again, and a sample answered that way alone
    says the device did not apply the command. A spontaneous report is kept only until the
    settle window closes without a trusted answer. With ``interrupt`` the sample also ends
    the moment the fixture is lit or the room occupied, so the driver can restore at once;
    the forced mode passes False, since a lit fixture says so in every message."""
    channel.command(xy)
    start = clock.monotonic()
    end = start + ANSWER_TIMEOUT
    first: float | None = None  # when a spontaneous report opened the settle window
    last: XY | None = None  # the spontaneous value on hand
    candidate: XY | None = None  # the latest trusted answer, awaiting a second that agrees
    settled: XY | None = None
    echo = foreign = lit = occupied = moved = False
    reads = answered = unapplied = retries = 0
    first_read: float | None = None
    answered_at: float | None = None
    next_read = start + READ_AFTER
    while clock.monotonic() < end and settled is None:
        now = clock.monotonic()
        if now >= next_read and reads < MAX_SETTLE_READS and reads - answered < MAX_READS:
            channel.read()
            reads += 1
            if first_read is None:
                first_read = now
            next_read = now + READ_RETRY
        for ob in channel.observe(max(MIN_WAIT, min(POLL, end - now))):
            if ob.kind == "foreign":
                foreign = True
            elif ob.kind == "lit":
                lit = True
            elif ob.kind == "occupied":
                occupied = True
            elif ob.kind == "unapplied":
                answered += 1
                unapplied += 1
            elif ob.kind == "echo" or ob.xy is None or (not ob.trusted and is_echo(xy, ob.xy)):
                echo = True
            elif not ob.trusted and previous is not None and is_echo(previous, ob.xy):
                continue  # stale: the previous colour reported late
            elif ob.trusted:
                answered += 1
                if candidate is None:
                    answered_at = clock.monotonic()
                    retries = max(0, reads - 1)
                    candidate = ob.xy
                    next_read = clock.monotonic()  # confirm at once
                elif is_echo(candidate, ob.xy):
                    settled = ob.xy
                else:
                    moved = True
                    candidate = ob.xy
                    next_read = clock.monotonic() + READ_RETRY
            elif candidate is None:
                if first is None:
                    first = clock.monotonic()
                    end = min(end, first + SETTLE)
                last = ob.xy
        if interrupt and (lit or occupied):
            break
    latency = None
    if first_read is not None and answered_at is not None:
        latency = max(0.0, answered_at - first_read)
    if candidate is None:
        retries = max(0, reads - 1)
    if settled is not None:
        return Sample(
            settled, False, foreign, lit, True, occupied, reads, latency, retries, True, moved
        )
    if candidate is not None and not moved:
        # one trusted answer, and the confirming read-back went unanswered
        return Sample(candidate, False, foreign, lit, True, occupied, reads, latency, retries)
    if candidate is not None:
        # the device was still changing colour when the sample ran out: no answer
        return Sample(
            None, False, foreign, lit, False, occupied, reads, latency, retries, False, True
        )
    if last is not None:
        return Sample(last, False, foreign, lit, False, occupied, reads, None, retries)
    return Sample(
        None, echo, foreign, lit, False, occupied, reads, None, retries, unapplied=unapplied > 0
    )


def take_ct_sample(
    channel: DeviceChannel, mired: int, clock: Clock, *, interrupt: bool = True
) -> Sample:
    """Command one colour temperature and collect the device's answer, in mireds, under the
    same timing rules as :func:`take_sample`: read back after ``READ_AFTER``, confirmed by a
    second read-back that agrees, read again after ``READ_RETRY`` while two disagree, and a
    read-back that still shows the value from before the command is ``unapplied``. Only a
    read-back counts: a spontaneous colour-temperature report is a transport's clamp of the
    command as often as the device's word, and the range must be the device's."""
    channel.command_ct(mired)
    start = clock.monotonic()
    end = start + ANSWER_TIMEOUT
    candidate: int | None = None
    settled: int | None = None
    foreign = lit = occupied = moved = False
    reads = answered = unapplied = retries = 0
    first_read: float | None = None
    answered_at: float | None = None
    next_read = start + READ_AFTER
    while clock.monotonic() < end and settled is None:
        now = clock.monotonic()
        if now >= next_read and reads < MAX_SETTLE_READS and reads - answered < MAX_READS:
            channel.read()
            reads += 1
            if first_read is None:
                first_read = now
            next_read = now + READ_RETRY
        for ob in channel.observe(max(MIN_WAIT, min(POLL, end - now))):
            if ob.kind == "foreign":
                foreign = True
            elif ob.kind == "lit":
                lit = True
            elif ob.kind == "occupied":
                occupied = True
            elif ob.mired is None:
                continue  # a colour observation, or an echo: not this sample's evidence
            elif ob.kind == "unapplied":
                answered += 1
                unapplied += 1
            elif ob.kind == "device" and ob.trusted:
                answered += 1
                if candidate is None:
                    answered_at = clock.monotonic()
                    retries = max(0, reads - 1)
                    candidate = ob.mired
                    next_read = clock.monotonic()  # confirm at once
                elif candidate == ob.mired:
                    settled = ob.mired
                else:
                    moved = True
                    candidate = ob.mired
                    next_read = clock.monotonic() + READ_RETRY
        if interrupt and (lit or occupied):
            break
    latency = None
    if first_read is not None and answered_at is not None:
        latency = max(0.0, answered_at - first_read)
    if candidate is None:
        retries = max(0, reads - 1)
    if settled is not None:
        return Sample(
            None, False, foreign, lit, True, occupied, reads, latency, retries, True, moved,
            mired=settled,
        )  # fmt: skip
    if candidate is not None and not moved:
        # one trusted answer, and the confirming read-back went unanswered
        return Sample(
            None, False, foreign, lit, True, occupied, reads, latency, retries, mired=candidate
        )
    if candidate is not None:
        # still changing when the sample ran out: no answer
        return Sample(
            None, False, foreign, lit, False, occupied, reads, latency, retries, False, True
        )
    return Sample(
        None, False, foreign, lit, False, occupied, reads, None, retries, unapplied=unapplied > 0
    )


#: The reasons the range stage declines, as prefixes of the verdict's notes.
CT_STORED_UNCLIPPED: Final = "the device stores a colour temperature unclipped"
CT_DECLINED: Final = "colour temperature range declined"


def read_ct_limits(channel: DeviceChannel, clock: Clock) -> tuple[int, int] | None:
    """The limits the device declares (coolest, warmest; mireds), or None when it did not
    answer within ``ANSWER_TIMEOUT``. Other observations that arrive meanwhile are dropped:
    nothing is in flight that they could be evidence for."""
    channel.read_ct_limits()
    end = clock.monotonic() + ANSWER_TIMEOUT
    while clock.monotonic() < end:
        for ob in channel.observe(max(MIN_WAIT, min(POLL, end - clock.monotonic()))):
            if ob.ct_limits is not None:
                return ob.ct_limits
    return None


def measure_ct(
    channel: DeviceChannel,
    clock: Clock,
    *,
    allow_lit: bool = False,
    lit: bool | None = None,
    abort_when: Callable[[], str | None] | None = None,
    on_sample: Callable[[Sample], None] | None = None,
) -> tuple[
    tuple[int, int] | None,
    tuple[tuple[int, int | None], ...],
    tuple[int, int] | None,
    tuple[str, ...],
    str | None,
]:
    """The colour-temperature range of the fixture on ``channel``: the limits it declares
    (one read) and the two probes in ``CT_PROBES_MIRED``, commanded and read back, a foreign
    command retaking a sample once. Returns the range (``ct_range_from``, None when declined),
    the probe answers, the declared limits, notes for the verdict, and the interruption that
    cut it short, if one did (the caller restores at once; the fixture is not put back here).
    Runs after the polygon protocol, or on its own for a fixture whose polygon is already
    known.

    The probes are where a device that clips its attribute lands; a device that stores the
    command unclipped answers the probe's own value, lit or dark (every Hue bulb on the
    reference installation does), and its range is then its declared limits when those are
    credible, nothing otherwise. A lit fixture whose limits are credible is not probed (the
    probes would flash it for a check the dark case makes invisibly)."""
    answers: dict[int, int | None] = {}
    notes: list[str] = []
    if abort_when is not None and (reason := abort_when()) is not None:
        return None, (), None, (), reason
    limits = read_ct_limits(channel, clock)
    declared = kelvin_range(limits)
    if limits is None:
        notes.append("the device did not answer the colour temperature limits read")
    skip_probes = bool(lit) and credible(declared)
    for mired in () if skip_probes else CT_PROBES_MIRED:
        if abort_when is not None and (reason := abort_when()) is not None:
            return None, tuple(answers.items()), limits, tuple(notes), reason
        sample = take_ct_sample(channel, mired, clock, interrupt=not allow_lit)
        if on_sample is not None:
            on_sample(sample)
        interrupted = (sample.lit or sample.occupied) and not allow_lit
        if sample.foreign and not interrupted:
            _wait(channel, clock, RETAKE_PAUSE)
            sample = take_ct_sample(channel, mired, clock, interrupt=not allow_lit)
            if on_sample is not None:
                on_sample(sample)
            interrupted = (sample.lit or sample.occupied) and not allow_lit
        if interrupted:
            reason = (
                "room became occupied during the measurement"
                if sample.occupied and not sample.lit
                else "fixture turned on during the measurement"
            )
            return None, tuple(answers.items()), limits, tuple(notes), reason
        answers[mired] = sample.mired
        if sample.mired is None:
            what = "not applied" if sample.unapplied else "unanswered"
            notes.append(f"colour temperature probe {mired} mired {what}")
        _wait(channel, clock, PAUSE)
    rng = ct_range_from(answers, limits)
    unclipped = bool(answers) and all(v == m for m, v in answers.items())
    if unclipped:
        notes.append(f"{CT_STORED_UNCLIPPED}: the probes answered their own values")
    if rng is None:
        if limits is not None and not credible(declared):
            lo, hi = limits
            notes.append(
                f"{CT_DECLINED}: the device declares {lo}-{hi} mired"
                + (
                    f" ({declared[0]}-{declared[1]} K)"
                    if declared is not None
                    else " (not a range)"
                )
                + ", the attribute's whole span rather than its emitter; declare the range "
                "in the model from the vendor's specification"
            )
        elif answers and all(v is not None for v in answers.values()):
            got = ", ".join(f"{m}->{v}" for m, v in answers.items())
            notes.append(f"{CT_DECLINED}: the probes answered {got} and no limits were declared")
        else:
            notes.append(f"{CT_DECLINED}: no usable answer")
    elif declared is not None and rng != declared:
        notes.append(
            f"the probes ({rng[0]}-{rng[1]} K) and the declared limits "
            f"({declared[0]}-{declared[1]} K) disagree; the probes stand"
        )
    return rng, tuple(answers.items()), limits, tuple(notes), None


def measure(
    channel: DeviceChannel,
    *,
    allow_lit: bool = False,
    clock: Clock | None = None,
    probe: Probe | None = None,
    abort_when: Callable[[], str | None] | None = None,
    on_sample: Callable[[Sample], None] | None = None,
    ct: bool = True,
    xy: bool = True,
) -> Verdict | None:
    """Measure the fixture on ``channel``. None when it is lit and ``allow_lit`` is false: a
    lit fixture shows every probe colour on the wall. With ``allow_lit`` (the forced mode)
    the fixture is measured whatever its state and whoever is in the room, visibly.

    Two things are measured, each on its own switch: the polygon (``xy``, the probe protocol)
    and, after it, the colour-temperature range (``ct``, the two probes of ``measure_ct``,
    for a material that takes a colour temperature). A run with ``xy`` off measures the
    range alone, for a fixture whose polygon is already known; its verdict carries no
    polygon. An interruption during the range probes keeps a polygon already measured and
    declines the range with a note, since the polygon's evidence is complete.

    Without it the measurement is meant to be invisible, and it aborts, restoring the
    fixture at once with no transition and withholding the polygon, on the first sign that
    it would not be: the room becoming occupied (``occupied``, ahead of the render that
    follows presence), a command that turns the fixture on or the fixture reporting itself on
    (``lit``), or ``abort_when`` returning a reason between samples.

    A device that answers nothing usable on its opening samples is diagnosed rather than
    driven through the whole protocol (``DIAGNOSE_AFTER`` samples of one kind before any
    answer): one that keeps its resting colour under every command does not apply colour
    while off, and in the forced mode the driver switches it on and starts over (``restore``
    turns it off again); one that answers no read-back is not reachable this way; one still
    changing colour when every sample runs out ramps too slowly. Each reason is the verdict's
    ``aborted`` (see the ``NOT_APPLIED_WHILE_OFF``, ``NOT_APPLIED_LIT``, ``NO_READ_ANSWER``
    and ``NEVER_SETTLES`` prefixes)."""
    clk = clock or SystemClock()
    pr = probe or Probe()
    # asked in every mode: the channel's snapshot is what restore puts back and what a
    # read-back that shows the colour from before the command is recognised against
    lit = channel.is_lit()
    if not allow_lit and (channel.needs_lit or lit):
        return None
    aborted: str | None = None
    previous: XY | None = None
    answered_any = False
    opening = {"unapplied": 0, "silent": 0, "unsettled": 0}
    lit_up = False
    unconfirmed = ramped = unsettled = 0
    ct_range: tuple[int, int] | None = None
    ct_answers: tuple[tuple[int, int | None], ...] = ()
    ct_limits: tuple[int, int] | None = None
    ct_notes: tuple[str, ...] = ()
    cut: str | None = None
    try:
        while xy and (step := pr.next()) is not None:
            if abort_when is not None and (aborted := abort_when()) is not None:
                break
            sample = take_sample(channel, step.xy, clk, previous, interrupt=not allow_lit)
            if on_sample is not None:
                on_sample(sample)
            interrupted = (sample.lit or sample.occupied) and not allow_lit
            if sample.foreign and not interrupted:
                _wait(channel, clk, RETAKE_PAUSE)
                sample = take_sample(channel, step.xy, clk, previous, interrupt=not allow_lit)
                if on_sample is not None:
                    on_sample(sample)
                interrupted = (sample.lit or sample.occupied) and not allow_lit
            if interrupted:
                aborted = (
                    "room became occupied during the measurement"
                    if sample.occupied and not sample.lit
                    else "fixture turned on during the measurement"
                )
                break
            if sample.xy is not None:
                answered_any = True
                if sample.trusted and not sample.confirmed:
                    unconfirmed += 1
                if sample.moved:
                    ramped += 1
            elif sample.moved:
                unsettled += 1
            diagnosis = None if answered_any or sample.foreign else _diagnose(sample, opening)
            if diagnosis == "unapplied" and not lit_up and allow_lit and channel.light_up():
                # measured lit instead, from the start; restore turns it off again
                lit_up = True
                opening = dict.fromkeys(opening, 0)
                pr.reset()
                previous = None
                _wait(channel, clk, RETAKE_PAUSE)
                continue
            if diagnosis is not None:
                aborted = _reason(diagnosis, lit_up=lit_up, allow_lit=allow_lit)
                break
            pr.answer(step, sample.xy, trusted=sample.trusted)
            if sample.xy is not None:
                previous = sample.xy
            _wait(channel, clk, PAUSE)
        if ct and aborted is None:
            ct_range, ct_answers, ct_limits, ct_notes, cut = measure_ct(
                channel,
                clk,
                allow_lit=allow_lit,
                lit=bool(lit) or lit_up,
                abort_when=abort_when,
                on_sample=on_sample,
            )
            if cut is not None:
                ct_notes = (*ct_notes, f"colour temperature range not measured: {cut}")
                if not xy:
                    aborted = cut
    finally:
        channel.restore(0.0 if aborted is not None or cut is not None else 0.2)
    verdict = pr.verdict()
    notes = [*verdict.notes, *ct_notes]
    if lit_up:
        notes.append("switched on for the measurement: the device keeps its colour while off")
    if unconfirmed:
        notes.append(
            f"{unconfirmed} answer(s) from a single read-back (the confirming read-back went "
            f"unanswered)"
        )
    if ramped:
        notes.append(
            f"{ramped} sample(s) settled only after the device was seen still changing "
            f"colour: it ramps on its own"
        )
    if unsettled:
        notes.append(
            f"{unsettled} sample(s) without an answer: the device was still changing colour "
            f"when the sample ran out"
        )
    if aborted is None:
        return replace(
            verdict,
            notes=tuple(notes),
            ct_range_k=ct_range,
            ct_answers=ct_answers,
            ct_limits=ct_limits,
        )
    return replace(
        verdict,
        polygon=None,
        model_error=None,
        fits=(),
        notes=(*notes, f"aborted: {aborted}"),
        aborted=aborted,
        ct_answers=ct_answers,
        ct_limits=ct_limits,
    )


def _diagnose(sample: Sample, opening: dict[str, int]) -> str | None:
    """Count what an unanswered opening sample was, and name the kind that has recurred
    ``DIAGNOSE_AFTER`` times. None while nothing has."""
    if sample.xy is not None:
        return None
    if sample.unapplied:
        kind = "unapplied"
    elif sample.moved:
        kind = "unsettled"
    else:
        kind = "silent"
    opening[kind] += 1
    return kind if opening[kind] >= DIAGNOSE_AFTER else None


def _reason(kind: str, *, lit_up: bool, allow_lit: bool) -> str:
    n = DIAGNOSE_AFTER
    if kind == "unapplied" and lit_up:
        return (
            f"{NOT_APPLIED_LIT}: it kept its resting colour through {n} commands after being "
            f"switched on; the device does not take an xy colour this way"
        )
    if kind == "unapplied":
        how = (
            "this transport cannot switch it on"
            if allow_lit
            else "the forced mode switches it on to measure it"
        )
        return (
            f"{NOT_APPLIED_WHILE_OFF}: it kept its resting colour through the first {n} "
            f"commands; {how}"
        )
    if kind == "unsettled":
        return (
            f"{NEVER_SETTLES}: the device was still changing colour {ANSWER_TIMEOUT:g} s after "
            f"each of the first {n} commands; its own ramp is too slow to measure"
        )
    return f"{NO_READ_ANSWER} through the first {n} commands: the device is not reachable this way"


def _wait(channel: DeviceChannel, clock: Clock, seconds: float) -> None:
    """Let time pass by draining the channel, so nothing arriving meanwhile is lost."""
    end = clock.monotonic() + seconds
    while clock.monotonic() < end:
        channel.observe(max(MIN_WAIT, min(POLL, end - clock.monotonic())))


def gamut_from(
    verdict: Verdict, channel: DeviceChannel, clock: Clock | None = None
) -> Gamut | None:
    """The model record for a successful measurement: the polygon and the fitted clip rule,
    bound to the device, dated, with the firmware the transport reported. Never inherited:
    a fixture's own measurement replaces any seed it held."""
    if verdict.polygon is None:
        return None
    when = (clock or SystemClock()).now().isoformat(timespec="seconds")
    return Gamut(
        vertices=verdict.polygon,
        bound_to=channel.identity,
        measured=when,
        model_error=verdict.model_error,
        clip_rule=verdict.clip_rule,
        inherited_from=None,
        firmware=channel.firmware,
        ct_range_k=verdict.ct_range_k,
    )
