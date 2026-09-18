# Brightness levels: one unit system

Companion to `gamut.md`. That document is about colour a device cannot reach; this one is about
a level the host presents in a different unit than the one the device holds. Both are the same
kind of rule: a comparator judges a device against what it can actually show, in the units it
actually reports, and never widens a tolerance to paper over the difference.

## 1. The units (`pascl.core.levels`)

PASCL commands a level in the device's own units: a Zigbee level 1..254 (`curves.MIN_LEVEL` to
`curves.MAX_LEVEL`). That integer goes into the transport payload, into the intent cache and into
the publish clock. A host that fronts the device with its own scale presents the report rescaled:
Home Assistant maps a Zigbee2MQTT level 0..254 onto 0..255 as `round(level * 255 / 254)`, equal to
the level below 127 and one above it from 127 up.

The rule: **convert at the boundary, compare in device units.**

- `host_brightness(level, scale)`: what the host shows for a device level.
- `device_level(attribute, scale)`: the inverse, exact for every level (the rescale is injective).
- `brightness_diverged(reported, intended, tolerance)`: the comparator, on levels.
  `BRIGHTNESS_TOLERANCE` is 2: two steps of 254 are invisible, and it is the band the render's
  own lag must stay inside.

Why a tolerance cannot absorb the unit: a render that is allowed to lag the intent by 2 (the
tolerance) keeps every at-rest report inside the band only if the report is read in the same
units. Read through the host's rescale, a report on a falling ramp above level 126 sits at
`intent + 3`, and the comparator trips on every such ramp. Measured on the reference installation
before the conversion: 82% of all brightness drift repaints (3,026 of 3,694 over 7.8 days), every
one at level >= 127 on a falling ramp with the render exactly 2 behind. A wider band was tried
there earlier and reverted: at >3 a genuine three-step drift is never repaired.

## 2. The shell presents the level (`pascl.shell.ha_light.level_of`)

`level_of(state, scale)` reads a host light state and returns the device's level, with the
transport's scale: 254 for a Zigbee light, 255 for a light the host drives in its own units
(Lutron through `light.turn_on`). `HALightChannel.level()` is the same read on a bound channel.
This is the only place the host's presentation is undone; nothing above the shell sees an
attribute.

## 3. The check (`pascl.estimator.levels`)

The conversion assumes the device reports the level it was sent, as the transport presents it.
That is a property of the transport, and on the reference installation it holds for every
fixture (95.98% of 34,837 settled reports exactly the rescale, the rest within one). The check
takes pairs of (sent, reported) the shell collected once the device settled and returns a verdict
per fixture: `rescale` (the conversion holds), `identity` (the transport does not rescale: compare
that fixture at the host's scale), `irregular` (a firmware that quantises, a device answering for
another load: the one place the converted comparison can still be wrong, so it is named), or
`declined` under `MIN_PAIRS`. It never seeds one fixture from another: a verdict is evidence
about one device's reports. `fleet()` is the one-line daily read; a fixture leaving `rescale` is
the tell.
