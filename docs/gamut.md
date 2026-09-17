# Colour gamut calibration

A fixture cannot render every chromaticity a palette can name. Commanded a point outside its
gamut, the device clips to a reachable point and reports that. A comparator that judges the
device against the authored intent therefore disagrees for as long as the intent is out of
reach, and a correction loop built on it re-sends the same unreachable colour on every visit.
The reference installation measured that loop at 44 % of all colour corrections before it
modelled reachability.

PASCL's answer is exact rather than permissive, and it has these parts.

## 1. The comparator rule (`pascl.core.gamut`)

The intent stays authored on the wire and in the cache. Every comparator judges the device
against the intent **clipped** onto the fixture's gamut the way the device clips it
(`clip`, `diverged`): the closest point of the convex polygon for every fixture measured so far
(`project`), or one of the other rules a device may follow. For an in-gamut intent the clip is
the identity, so nothing changes; for an out-of-gamut one the tolerance sits around the point
the device can actually reach. A wrong polygon fails loud (the comparator fires and stays on),
a missing one degrades to the identity, which is the behaviour without the calibration. The
projection reproduces the device's own clip to within 2e-05 on every fixture measured so far.

## 2. The polygon is measured, never looked up (`pascl.estimator.gamut`)

No brand table. The runtime commands colours far outside any real emitter's gamut, with no
transition, and reads back what the device says it now shows; the convex hull of the answers
is the polygon, bound to the transport's identity for that device. The procedure is a pure
state machine (`Probe`), so a simulated device that clips to a hidden polygon must be recovered
exactly, and it is:

* **Far pass.** Eighteen points around the chromaticity diagram, ordered so consecutive points
  clip to different regions (a device reports an attribute only when it changes). Their
  answers form the first hull.
* **Vertex pass.** One probe beyond each hull vertex, along the ray from the centroid, refines
  it.
* **Edge pass.** One probe just outside each edge midpoint checks that the device clips to the
  closest point of that edge.
* **Corner search.** An edge the device does *not* clip to hides a vertex the far points missed
  (a fourth or fifth primary). The lines of the two neighbouring edges are intersected, a probe
  is placed just beyond that point along the bisector of their outward normals, and the device
  answers with the vertex itself. The new edges are checked in turn, bounded by a round limit
  and the vertex ceiling.

A three-primary emitter takes 24 probes; the four-primary test shapes take 27 to 31. A device
that answers nothing but the transport's echo of the command, or always the same point,
measures nothing and stays unmeasured.

**What is evidence.** An answer equal to the command is the transport's echo and is discarded,
unless the adapter marks it *trusted*: it read the value back from the device, so equality
means the device *reached* the command (a probe that fell inside the true gamut, as the edge
probe outside a cut corner does). Without that distinction a multi-primary device looped the
protocol forever. An untrusted answer equal to the previous one is the device's late report of
the colour it showed before, not an answer. An edge whose probe got no usable answer is
recorded as unverified rather than asked again.

**The clip rule is measured too.** Every rule in `CLIP_RULES` (`closest`, `toward_white`,
`rgb_clamp`) is fitted to the answers, and the one that reproduces the device is recorded with
its residual as `model_error` (`fit_clip_rule`). The polygon is the same under every rule (the
answers are on its boundary whatever the rule); what differs is *where* on the boundary an
unreachable command lands, which is what a comparator has to predict. The fit is robust: an
answer inside the hull is never a clip of an outside command (a colour from elsewhere reached
the device during that sample), and an answer no rule accounts for at all (a stale report) is
counted rather than allowed to pick the rule. A device that really follows another rule
disagrees with the wrong one on most answers, and nothing is trimmed from that disagreement.

What the adapter has to provide, learned the hard way on the reference installation:

* Command with a zero transition and obtain the device's **own** value. Spontaneous attribute
  reports are not a reliable channel (some devices report at most once per ~10 s, some never);
  a read of the colour attributes one second after the command answers on every device tried.
* Recognise the transport's optimistic echo **by value** before the read, and trust the value
  the read brings back even when it equals the command.
* On a multi-endpoint device the read may land under the transport's unsuffixed key while the
  endpoint key keeps the echo, and be republished stale afterwards: take it only after your own
  read and only if it moved.
* Read the state and the colour in ONE request before measuring; two requests answered
  separately let the second answer, the resting colour, arrive during the first sample.
* Watch every topic a command can reach the device through, the device's own and its groups',
  and take a sample again after a foreign command.
* Never measure a lit fixture without pausing whatever renders it; measure dark fixtures in
  empty rooms and restore the cached intent afterwards, so the calibration is invisible. Abort
  if the fixture lights part-way or the room fills.

## 3. The model carries it (`Fixture.gamut`)

```yaml
den_strip:
  material: color_strip
  gamut:
    vertices: [[0.153185, 0.047547], [0.691493, 0.308293], [0.169986, 0.699992]]
    bound_to: "0x0017880100aa0001"        # the device's IEEE address (+ /endpoint)
    measured: "2026-09-16T21:46:14+00:00"
    model_error: 0.00002
    clip_rule: closest
    inherited_from: null                  # or the measured fixture this was copied from
    firmware: "1.163.1"                   # information, never a staleness trigger
```

Validation rejects a polygon that is not convex, not counter-clockwise, too small, or on a
material without the `xy` capability; a `rgb_clamp` rule on anything but a triangle; an
`inherited_from` that does not name a measured fixture of the same material model label; and
two measured units of one label further apart than 0.02 (one measurement is wrong, or the
label is). `fixture_clip(model, id)` resolves a fixture to its polygon and rule, or to the
identity; `with_fixture_gamut` records a measurement as a functional update, the way the
tuning UI writes the model.

## 4. Seeds: a measurement travels with its provenance

Two fixtures whose materials carry the same model label are the same hardware by the author's
declaration. A measured polygon therefore seeds every unmeasured (or rebound) fixture of the
same label as `inherited`, with `inherited_from` naming the source, so a new fixture is exact
from its first minute (`seeds`, `inherit`, `pascl gamut inherit`). That is data with
provenance, not brand knowledge: the engine never learns what the label means, only that the
author called two fixtures the same. The seed is a seed: the runtime measures the fixture
itself when it can (last, after the unmeasured and the rebound), and `confirm_seed` says
whether the measurement agrees within 0.003 or, loudly, contradicts it. When a source is
re-measured its copies are refreshed; `consistency` lists measured units of one label that
disagree by more than 0.005 and copies that no longer match their source.

On the reference installation every unit of every label measured the identical polygon (eleven
labels, up to nine units each, max vertex deviation 0.000000), so a seed has never been wrong
there. It is still a seed, because the measurement is cheap and the label is only a label.

## 5. When the runtime measures (`statuses`, `pick_target`, `pascl.shell.gamut_runtime`)

The runtime keeps a status per colour fixture: `measured`, `inherited`, `unmeasured`, or
`rebound` (the polygon is bound to a device identity the transport no longer reports, i.e. the
fixture was replaced). Whenever a fixture is dark and its room unoccupied, `pick_target` names
the next one to measure: unmeasured before rebound before inherited. Measuring a dark fixture in
an empty room is invisible and self-restoring, which is what lets the runtime do this on its
own, the way its daylight calibrator takes empty-and-dark windows. A gamut does not drift, so
staleness is device identity, not age.

`pascl.shell.gamut_runtime.tick` is one pass: read the host through the binding (which light
entities are on, which presence entities are on), read each coordinator's device list, pick,
measure with the room's presence watched for an abort, refuse a record the model would not
validate with, write the model, let the measurement travel. `run` is the loop.

    HA_TOKEN=... pascl gamut auto --model home.yaml --binding binding.yaml \
        --ha-url http://homeassistant:8123 --write home.yaml [--interval 300] [--once]

`pascl gamut status --model F [--device-ids TSV]`, `pascl gamut pick ...`, `pascl gamut
inherit ...` and `pascl gamut plan` are the pure views from the command line; `pascl gamut ids`
prints every fixture's device identity, model id and firmware from the coordinators, in the
shape `status` and `pick` take.

## 6. Device identity (`pascl.shell.z2m`)

The identity a polygon is bound to is the device's IEEE address as its Zigbee2MQTT coordinator
lists it in the retained `bridge/devices`, suffixed with the endpoint for a fixture that is one
endpoint of a device. A topic is a name the user can change; the address survives a rename and
changes with the hardware, which is exactly what a bound polygon needs. The same list carries
the firmware build, recorded with the measurement as information for a reader comparing units,
never as a staleness trigger. The retained `bridge/groups` names the groups a device belongs
to, whose `/set` topics the measurement watches as foreign.

## 7. Running one measurement (`pascl.shell`)

`pascl.shell.gamut_measure` drives a `Probe` over a `DeviceChannel` with the timing rules
(read back after 1 s without a report, a 1.5 s settle after the first device value, a 6 s
ceiling, a re-take after a foreign command), against the injected clock, and restores the
fixture whatever happens. `pascl.shell.z2m` is the Zigbee2MQTT channel: the `/set` and `/get`
topics, the echo and read-back rules, the endpoint rule, the foreign-command and lit guards,
snapshot and restore, over an injected MQTT link. `pascl.shell.ha_mqtt` is one such link,
through Home Assistant's REST `mqtt.publish` and `mqtt/subscribe` WebSocket
(`pip install pascl[ha]`); `pascl.shell.paho_mqtt` is the other, straight to a broker
(`pip install pascl[mqtt]`).

    HA_TOKEN=... pascl gamut measure --model home.yaml --binding binding.yaml \
        --fixture den_strip --ha-url http://homeassistant:8123 --write home.yaml

resolves the fixture's command topic from the binding, looks its device up, measures it
(refusing a lit fixture unless `--allow-lit`), prints the verdict with every rule's fit, says
whether a seed it held is confirmed, and records the polygon in the model. Measured this way on
the reference installation's storeroom strip (off, 72 s, 24 of 24 answers): the same triangle
its provisioning tool had recorded, to 2e-05. That tool is now a wrapper over these modules.

## 8. What the palettes can show (`pascl palette check`)

    pascl palette check --model home.yaml

lists every palette colour some measured fixture cannot show, with the colour that renders
instead: static entries per family (bulbs, strips) and, for the solar white palettes, the
worst excursion along each calibration's warm-to-cool and warm-to-night arc. Fixtures that
share a polygon and rule are reported together. Nothing at runtime depends on it (the
comparators are exact about reachability); it makes the author's choice visible.
