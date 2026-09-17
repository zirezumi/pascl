"""Run a gamut measurement over a device channel.

The impure half of ``pascl.estimator.gamut``: the protocol there decides which colours to
command and what the answers add up to; this module commands them, waits for the device, and
applies the timing rules a real device needs. It talks to the device through a small
:class:`DeviceChannel`, so a transport adapter (``pascl.shell.z2m``) and a test double look the
same to it, and the only time it reads is the injected :class:`Clock`.

Timing rules, all measured on the reference installation's fleet: a device applies a zero
transition at once but may report the change late or never, so ``READ_AFTER`` seconds after
the command the channel is asked to read the value back, again every ``READ_RETRY`` while the
device has not answered (a read that lands before the device applied the command answers with
the colour it showed before, which the channel does not pass on), and the read's answer ends
the sample; a spontaneous report is a fallback that starts a ``SETTLE`` window in which a later
value supersedes it; ``ANSWER_TIMEOUT`` bounds the whole sample; a foreign command on the
device's own topic during a sample invalidates it, and it is taken once more.

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
from pascl.estimator.gamut import Probe, Verdict, is_echo
from pascl.model import Gamut

#: Seconds after the command before the first read-back: the device applies a zero transition
#: within a Zigbee round trip, and every device tried answered a read in under 0.1 s.
READ_AFTER: Final = 0.4
#: Seconds between read-backs while the device has not answered (or answered with the colour
#: it showed before the command, i.e. had not applied it yet).
READ_RETRY: Final = 0.4
MAX_READS: Final = 3
ANSWER_TIMEOUT: Final = 3.0
SETTLE: Final = 1.5
PAUSE: Final = 0.15
RETAKE_PAUSE: Final = 1.0
POLL: Final = 0.1
#: The shortest wait a channel is asked for; a real link has nothing useful to say sooner.
MIN_WAIT: Final = 0.05


@dataclass(frozen=True)
class Observation:
    """One thing the channel saw: the device's own colour, the transport's echo of a command,
    a command on this device's topic that the measurement did not send, the fixture being or
    reporting itself ON, or the room it lights becoming occupied."""

    kind: Literal["device", "echo", "foreign", "lit", "occupied"]
    xy: XY | None = None
    trusted: bool = False
    """The channel read this value back from the device, so it is the device's own even when
    it equals the command (the device reached it); an untrusted value equal to the command is
    taken for the echo."""


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

    def read(self) -> None:
        """Ask the device to report its current colour."""

    def observe(self, seconds: float) -> list[Observation]:
        """Block up to ``seconds`` and return what arrived, classified."""

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
    """How many read-backs went out: more than one means the device was slow to apply or to
    answer, the first sign of a transport with no airtime to spare."""
    latency: float | None = None
    """Seconds from the first read-back to the trusted answer; None without one."""


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
    so the read-back still goes out. A trusted answer (a read-back) ends the sample at once;
    a spontaneous report is kept only until the settle window closes without one. With
    ``interrupt`` the sample also ends the moment the fixture is lit or the room occupied,
    so the driver can restore at once; the forced mode passes False, since a lit fixture
    says so in every message."""
    channel.command(xy)
    start = clock.monotonic()
    end = start + ANSWER_TIMEOUT
    first: float | None = None
    last: XY | None = None
    trusted = False
    echo = False
    foreign = False
    lit = False
    occupied = False
    reads = 0
    first_read: float | None = None
    answered_at: float | None = None
    next_read = start + READ_AFTER
    while clock.monotonic() < end and not trusted:
        now = clock.monotonic()
        if reads < MAX_READS and now >= next_read:
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
            elif ob.kind == "echo" or ob.xy is None or (not ob.trusted and is_echo(xy, ob.xy)):
                echo = True
            elif not ob.trusted and previous is not None and is_echo(previous, ob.xy):
                continue  # stale: the previous colour reported late
            elif ob.trusted:
                last, trusted = ob.xy, True
                answered_at = clock.monotonic()
            elif not trusted:
                if first is None:
                    first = clock.monotonic()
                    end = min(end, first + SETTLE)
                last = ob.xy
        if interrupt and (lit or occupied):
            break
    latency = None
    if trusted and first_read is not None and answered_at is not None:
        latency = max(0.0, answered_at - first_read)
    return Sample(last, echo and last is None, foreign, lit, trusted, occupied, reads, latency)


def measure(
    channel: DeviceChannel,
    *,
    allow_lit: bool = False,
    clock: Clock | None = None,
    probe: Probe | None = None,
    abort_when: Callable[[], str | None] | None = None,
    on_sample: Callable[[Sample], None] | None = None,
) -> Verdict | None:
    """Measure the fixture on ``channel``. None when it is lit and ``allow_lit`` is false: a
    lit fixture shows every probe colour on the wall. With ``allow_lit`` (the forced mode)
    the fixture is measured whatever its state and whoever is in the room, visibly.

    Without it the measurement is meant to be invisible, and it aborts, restoring the
    fixture at once with no transition and withholding the polygon, on the first sign that
    it would not be: the room becoming occupied (``occupied``, ahead of the render that
    follows presence), a command that turns the fixture on or the fixture reporting itself on
    (``lit``), or ``abort_when`` returning a reason between samples."""
    clk = clock or SystemClock()
    pr = probe or Probe()
    if not allow_lit and (channel.needs_lit or channel.is_lit()):
        return None
    aborted: str | None = None
    previous: XY | None = None
    try:
        while (step := pr.next()) is not None:
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
            pr.answer(step, sample.xy, trusted=sample.trusted)
            if sample.xy is not None:
                previous = sample.xy
            _wait(channel, clk, PAUSE)
    finally:
        channel.restore(0.0 if aborted is not None else 0.2)
    verdict = pr.verdict()
    if aborted is None:
        return verdict
    return replace(
        verdict,
        polygon=None,
        model_error=None,
        fits=(),
        notes=(*verdict.notes, f"aborted: {aborted}"),
        aborted=aborted,
    )


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
    )
