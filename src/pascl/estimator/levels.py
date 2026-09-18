"""The level-report check: does a fixture report the host's rescale of the level it was sent?

:mod:`pascl.core.levels` assumes it does, and converts the host's attribute back into device
units on that assumption. The assumption is a property of the transport, not of the fixture,
so on a healthy installation every fixture's verdict is the same one; a fixture whose reports
stop being the rescale of its commands (a firmware that quantises, a transport with another
scale, a device answering for a different load) is the one place the converted comparison can
still be wrong, and this check is how it gets named. Pure: it takes pairs of (sent, reported)
the shell collected and judges them. It declines below a sample floor and never seeds a
fixture from another, because a verdict here is evidence about one device's reports.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from pascl.core.levels import HOST_SCALE, ZIGBEE_SCALE, host_brightness

MIN_PAIRS: Final = 20
"""Fewer settled reports than this and the check declines rather than guesses."""

EXACT_MIN: Final = 0.90
"""Share of reports that must be exactly the rescale (or exactly the sent level) for a verdict."""

RESCALE_KNEE: Final = 127
"""The first device level the host's rescale moves; below it rescale and identity coincide."""


@dataclass(frozen=True)
class LevelPair:
    """One command and the report that followed it once the device had settled."""

    sent: int
    """The level put on the wire, in device units."""
    reported: int
    """The host's brightness attribute for the device afterwards, in the host's units."""


@dataclass(frozen=True)
class LevelReport:
    fixture: str
    pairs: int
    exact: float
    """Share of reports exactly ``host_brightness(sent, scale)``."""
    within_one: float
    """Share within one of that."""
    identity: float
    """Share exactly equal to ``sent``, i.e. reported with no rescale at all."""
    verdict: str
    """``rescale`` (the conversion holds), ``identity`` (the transport does not rescale, compare
    at the host's scale), ``irregular`` (neither: name the fixture), ``declined``."""
    note: str
    mismatches: tuple[tuple[int, int], ...]
    """The most common (sent, reported) pairs that were not the rescale, worst first."""


def assess(fixture: str, pairs: Sequence[LevelPair], scale: int = ZIGBEE_SCALE) -> LevelReport:
    n = len(pairs)
    if n < MIN_PAIRS:
        note = f"{n} settled reports, need {MIN_PAIRS}"
        return LevelReport(fixture, n, 0.0, 0.0, 0.0, "declined", note, ())
    exact = within = ident = 0
    off: Counter[tuple[int, int]] = Counter()
    for p in pairs:
        expected = host_brightness(p.sent, scale)
        if p.reported == expected:
            exact += 1
        else:
            off[(p.sent, p.reported)] += 1
        if abs(p.reported - expected) <= 1:
            within += 1
        if p.reported == p.sent:
            ident += 1
    ex, wi, idn = exact / n, within / n, ident / n
    top = tuple(k for k, _ in off.most_common(4))
    knee = all(p.sent < RESCALE_KNEE for p in pairs)
    if ex >= EXACT_MIN:
        note = "reports are the host's rescale of the sent level"
        if knee and scale != HOST_SCALE:
            note += f"; every sample below level {RESCALE_KNEE}, where rescale and identity agree"
        return LevelReport(fixture, n, ex, wi, idn, "rescale", note, top)
    if idn >= EXACT_MIN:
        note = f"reports equal the sent level, no rescale: compare at scale {HOST_SCALE}"
        return LevelReport(fixture, n, ex, wi, idn, "identity", note, top)
    note = (
        f"only {ex:.0%} of reports are the rescale ({wi:.0%} within one): this device does not "
        "report the level it was sent as the transport presents it"
    )
    return LevelReport(fixture, n, ex, wi, idn, "irregular", note, top)


def fleet(reports: Sequence[LevelReport]) -> dict[str, int]:
    """Verdict counts, the one line a daily health read wants."""
    return dict(Counter(r.verdict for r in reports))
