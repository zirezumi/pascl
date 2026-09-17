"""Run a gamut measurement over a device channel.

The impure half of ``pascl.estimator.gamut``: the protocol there decides which colours to
command and what the answers add up to; this module commands them, waits for the device, and
applies the timing rules a real device needs. It talks to the device through a small
:class:`DeviceChannel`, so a transport adapter (``pascl.shell.z2m``) and a test double look the
same to it, and the only time it reads is the injected :class:`Clock`.

Timing rules, all measured on the reference installation's fleet: a device applies a zero
transition at once but may report the change late or never, so after ``READ_AFTER`` seconds
without a device answer the channel is asked to read the value back; the first device value
starts a ``SETTLE`` window in which a later value supersedes it; ``ANSWER_TIMEOUT`` bounds the
whole sample; a foreign command on the device's own topic during a sample invalidates it, and
it is taken once more.

Two things end a measurement early, and both withhold the polygon: the fixture turning ON
while it is being measured (a switch, a render, a group command: every probe colour would be
on the wall from then on), and whatever the caller's ``abort_when`` watches, such as the room
becoming occupied. The fixture is restored either way.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Final, Literal, Protocol

from pascl.clock import Clock, SystemClock
from pascl.core.gamut import XY
from pascl.estimator.gamut import Probe, Verdict, is_echo
from pascl.model import Gamut

READ_AFTER: Final = 1.0
ANSWER_TIMEOUT: Final = 6.0
SETTLE: Final = 1.5
PAUSE: Final = 0.15
RETAKE_PAUSE: Final = 2.0
POLL: Final = 0.25


@dataclass(frozen=True)
class Observation:
    """One thing the channel saw: the device's own colour, the transport's echo of a command,
    a command on this device's topic that the measurement did not send, or the fixture
    reporting itself ON."""

    kind: Literal["device", "echo", "foreign", "lit"]
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

    def is_lit(self) -> bool | None:
        """Whether the fixture is on; None when the transport cannot tell."""

    def command(self, xy: XY) -> None:
        """Command the colour with a zero transition."""

    def read(self) -> None:
        """Ask the device to report its current colour."""

    def observe(self, seconds: float) -> list[Observation]:
        """Block up to ``seconds`` and return what arrived, classified."""

    def restore(self) -> None:
        """Put the fixture back as it was before the measurement."""


@dataclass(frozen=True)
class Sample:
    xy: XY | None
    echo_only: bool
    foreign: bool
    lit: bool = False
    trusted: bool = False
    """The value was read back from the device (see :class:`Observation`)."""


def take_sample(channel: DeviceChannel, xy: XY, clock: Clock, previous: XY | None = None) -> Sample:
    """Command one colour and collect the device's answer under the timing rules. An
    untrusted value equal to ``previous`` (the last sample's answer) is the device's late
    report of the colour it showed before, not an answer to this command: it is set aside
    so the read-back still goes out."""
    channel.command(xy)
    start = clock.monotonic()
    end = start + ANSWER_TIMEOUT
    first: float | None = None
    last: XY | None = None
    trusted = False
    echo = False
    foreign = False
    lit = False
    read_sent = False
    while clock.monotonic() < end:
        if not read_sent and first is None and clock.monotonic() >= start + READ_AFTER:
            channel.read()
            read_sent = True
        for ob in channel.observe(max(0.05, min(POLL, end - clock.monotonic()))):
            if ob.kind == "foreign":
                foreign = True
            elif ob.kind == "lit":
                lit = True
            elif ob.kind == "echo" or ob.xy is None or (not ob.trusted and is_echo(xy, ob.xy)):
                echo = True
            elif not ob.trusted and previous is not None and is_echo(previous, ob.xy):
                continue  # stale: the previous colour reported late
            else:
                if first is None:
                    first = clock.monotonic()
                    end = first + SETTLE
                last, trusted = ob.xy, ob.trusted
        if first is not None and clock.monotonic() > first + SETTLE:
            break
    return Sample(last, echo and last is None, foreign, lit, trusted)


def measure(
    channel: DeviceChannel,
    *,
    allow_lit: bool = False,
    clock: Clock | None = None,
    probe: Probe | None = None,
    abort_when: Callable[[], str | None] | None = None,
) -> Verdict | None:
    """Measure the fixture on ``channel``. None when it is lit and ``allow_lit`` is false: a
    lit fixture shows every probe colour, and whatever renders it must be paused first. The
    measurement aborts, withholding the polygon, if the fixture turns on part-way (unless
    ``allow_lit``) or ``abort_when`` returns a reason between samples (the caller watching,
    say, the room's presence). The fixture is restored whatever happens."""
    clk = clock or SystemClock()
    pr = probe or Probe()
    if channel.is_lit() and not allow_lit:
        return None
    aborted: str | None = None
    previous: XY | None = None
    try:
        while (step := pr.next()) is not None:
            if abort_when is not None and (aborted := abort_when()) is not None:
                break
            sample = take_sample(channel, step.xy, clk, previous)
            if sample.foreign and not (sample.lit and not allow_lit):
                _wait(channel, clk, RETAKE_PAUSE)
                sample = take_sample(channel, step.xy, clk, previous)
            if sample.lit and not allow_lit:
                aborted = "fixture turned on during the measurement"
                break
            pr.answer(step, sample.xy, trusted=sample.trusted)
            if sample.xy is not None:
                previous = sample.xy
            _wait(channel, clk, PAUSE)
    finally:
        channel.restore()
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
        channel.observe(max(0.05, min(POLL, end - clock.monotonic())))


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
