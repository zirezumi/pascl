"""Brightness levels: one unit system for what PASCL commands and what a host reports.

PASCL commands a level in the device's own units: Zigbee's 1..254 (``curves.MIN_LEVEL`` to
``curves.MAX_LEVEL``), the integer that goes into the transport payload, into the intent cache
and into the publish clock. A host that fronts the device with its own scale presents the
device's report rescaled: Home Assistant's ``light`` entity maps a Zigbee2MQTT level 0..254 onto
0..255 as ``round(level * 255 / 254)``, which equals the level below 127 and is one above it
from 127 up. Measured on the reference installation (2026-09-17, 34,837 settled reports after a
publish): 95.98% are exactly that function of the commanded level, the rest within one, no
fixture deviating.

A comparator that reads the host's attribute against a device-unit intent is therefore
comparing two scales, and above level 126 it carries a built-in +1. With the render allowed to
lag the intent by :data:`BRIGHTNESS_TOLERANCE`, that +1 is the third step and the comparator
trips on every falling ramp: 82% of the reference installation's brightness drift repaints were
exactly this. The rule this module encodes: **convert at the boundary, compare in device
units.** The host shell turns a reported attribute back into the device's level with
:func:`device_level` (exact: the rescale is injective, so the inverse recovers every level),
and every judgement of a report against an intent goes through :func:`brightness_diverged` on
levels. Nothing here widens a tolerance to absorb a unit; a wider band was tried on the
reference installation and reverted because it blinds the net to a genuine three-step drift.
"""

from __future__ import annotations

from typing import Final

HOST_SCALE: Final = 255
"""The top of Home Assistant's brightness presentation."""

ZIGBEE_SCALE: Final = 254
"""The top of a Zigbee level, the units PASCL commands in and the value Zigbee2MQTT reports."""

BRIGHTNESS_TOLERANCE: Final = 2
"""Steps of device level a report may sit from its intent before it counts as diverged. The
consumer's tolerance (two steps of 254 are invisible), and the number the render's own lag
must stay inside: a render that publishes at a gap of three keeps every at-rest report inside
this band, and a report one step further is a device that did not go where it was sent."""


def host_brightness(level: int, scale: int = ZIGBEE_SCALE) -> int:
    """What a host with ``HOST_SCALE`` shows for a device level on a ``scale``-topped transport.
    Identity when the transport already speaks the host's scale."""
    if scale == HOST_SCALE:
        return level
    return min(round(level * HOST_SCALE / scale), HOST_SCALE)


def device_level(attribute: float | None, scale: int = ZIGBEE_SCALE) -> int | None:
    """The device's level behind a host brightness attribute; ``None`` stays ``None``. Exact
    for every level 0..``scale``, because the host's rescale is injective and this is its
    inverse."""
    if attribute is None:
        return None
    if scale == HOST_SCALE:
        return round(attribute)
    return min(max(round(attribute * scale / HOST_SCALE), 0), scale)


def brightness_diverged(
    reported: int | None, intended: int | None, tolerance: int = BRIGHTNESS_TOLERANCE
) -> bool:
    """Whether a device's reported level (device units, see :func:`device_level`) sits outside
    the tolerance of its intended level. An unreadable side counts as diverged: the repair
    re-asserts what it knows, which is the safe direction for a watchdog."""
    if reported is None or intended is None:
        return True
    return abs(reported - intended) > tolerance
