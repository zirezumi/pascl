"""After the fade: when the runtime reads a fixture's colour back, and when a comparator may
judge it.

A render command with a transition keeps the device moving for that long, and a drift
comparator that judges the device before the fade ends re-sends the colour the fade is about
to reach. The end of the fade is known from the command; what is NOT known is when the device
will say where it landed. No transport PASCL runs on declares that: Zigbee2MQTT configures
level and on/off reporting on a bulb and nothing for colour, and on the reference installation
the fixtures that report colour at all do so through a leftover binding the coordinator no
longer creates, while 27 of 85 never report a colour and the host holds the transport's echo of
the last command for them. A comparator fed by reports is therefore blind on part of any fleet
and late on the rest, and an arm derived from observed report cadence (``pascl.estimator.
reporting``) is a model of behaviour the transport does not promise.

So the runtime asks. After a command's fade has ended it READS the colour back, once, and the
comparator arms after the answer has had time to land. The rule, identical in the engine and
on the reference installation:

* ``fade_end`` is the command's time plus its transition.
* The read goes out at ``fade_end + READ_MARGIN_S``. The margin covers the device stepping the
  transition in coarse ticks (Hue: 100 ms) and a host whose notion of the command time runs a
  little ahead of the frame reaching the device.
* The read is issued only when it can say something: the fixture is lit (a dark fixture's
  colour is not judged) and its colour intent moved past the comparator's tolerance since the
  previous command (:func:`worth_reading`). A fade that moved the intent by less than the
  tolerance cannot produce a divergence the comparator would act on, so reading it is airtime
  for nothing; on the reference installation that gate keeps roughly one colour command in
  twenty-five.
* The comparator arms at ``fade_end + READ_MARGIN_S + READ_ALLOWANCE_S``. The allowance is the
  time a read's answer needs when every fixture on a coordinator was commanded in the same
  render and their reads queue at the same fade end (23 colour fixtures on one radio on the
  reference installation), plus one transport-level retry.

Every command counts, whichever channel it moved. Brightness and colour are separate commands
on most transports, a brightness-only fade keeps the device moving exactly as a colour fade
does, and a comparator judges the fixture, not a channel, so a fixture's fade end is the latest
over the commands in flight on it (:func:`fade_end_of`). The reference installation derives
that from three timestamps, each plus the scene transition: its scope's settling stamp (when
the fixture was commanded by that render), the fixture's colour cache write and its brightness
clock write. Until 2026-09-18 the brightness one was missing there, and a brightness-only fade
whose scope was re-stamped by a later, shorter pass repainted at its fade end with no hold.

A fixture commanded again before its read is due is not read twice: :class:`ReadSchedule`
keeps one due time per fixture and a new command moves it. The reads themselves are the
transport's business (``pascl.shell.z2m.READ_COLOUR_PAYLOAD``); this module only says when.

The failure direction: a read that goes unanswered leaves the comparator with whatever the host
already held, exactly the position it was in before reads existed, and the arm's allowance is
the only thing a stuck device waits for beyond the fade. A read that IS answered replaces the
transport's echo with the device's own state, which is what makes the comparator honest on the
fixtures that never report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from pascl.core.gamut import XY

READ_MARGIN_S: Final = 1.5
"""Seconds after the fade end before the colour is read back."""

READ_ALLOWANCE_S: Final = 4.0
"""Seconds after the read goes out that a comparator waits for its answer before judging."""

DRIFT_TOLERANCE: Final = 0.003
"""The comparator's per-axis xy tolerance on the reference installation: a colour intent that
moved less than this on both axes since the previous command is not worth a read."""


def fade_end(commanded_at: float, transition_s: float) -> float:
    """When a command's transition ends; a negative or missing transition ends at once."""
    return commanded_at + max(0.0, transition_s)


def fade_end_of(*commands: tuple[float, float]) -> float:
    """When a fixture with several commands in flight stops moving: the latest of their fade
    ends, each command a ``(commanded_at, transition_s)`` pair on whatever channel it moved
    (brightness, colour, or one carrying both). No commands: ``0.0``, the fade ended long ago."""
    return max((fade_end(at, transition) for at, transition in commands), default=0.0)


def read_at(fade_end: float) -> float:
    """When the colour is read back after a fade that ends at ``fade_end``."""
    return fade_end + READ_MARGIN_S


def arm_at(fade_end: float) -> float:
    """When a comparator may first judge a fixture whose fade ends at ``fade_end``."""
    return fade_end + READ_MARGIN_S + READ_ALLOWANCE_S


def worth_reading(
    previous: XY | None, xy: XY, *, lit: bool = True, tol: float = DRIFT_TOLERANCE
) -> bool:
    """Whether a command that moved a fixture's colour intent from ``previous`` to ``xy``
    deserves a read-back: the fixture is lit and the intent moved past the comparator's
    tolerance on either axis. An unknown previous intent is read (the first command after a
    restart says nothing about where the device was)."""
    if not lit:
        return False
    if previous is None:
        return True
    return abs(xy[0] - previous[0]) > tol or abs(xy[1] - previous[1]) > tol


@dataclass
class ReadSchedule:
    """The read due time of every fixture with a fade in flight.

    A new command for a fixture replaces its pending read (the first fade's answer would be
    overwritten by the second command anyway); :meth:`due` hands out, in due order, and forgets
    the fixtures whose read time has come. Times are seconds on whatever clock the caller uses
    for the command times, monotonic or wall; only differences matter here.
    """

    _due: dict[str, float] = field(default_factory=dict)

    def commanded(self, fixture: str, commanded_at: float, transition_s: float) -> float:
        """Record a command; returns the read's due time."""
        when = read_at(fade_end(commanded_at, transition_s))
        self._due[fixture] = when
        return when

    def forget(self, fixture: str) -> None:
        """Drop a pending read (the fixture went dark, or was re-commanded by something that
        will read it itself)."""
        self._due.pop(fixture, None)

    def due(self, now: float) -> list[str]:
        """The fixtures whose read time has come, earliest first; each is returned once."""
        ready = sorted((when, fixture) for fixture, when in self._due.items() if when <= now)
        for _, fixture in ready:
            del self._due[fixture]
        return [fixture for _, fixture in ready]

    def pending(self) -> dict[str, float]:
        """A copy of the outstanding due times, by fixture."""
        return dict(self._due)

    def next_due(self) -> float | None:
        """The earliest outstanding due time, or ``None`` with nothing scheduled: what a
        driver sleeps until."""
        return min(self._due.values(), default=None)
