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

* Command with a zero transition and obtain the device's **own** value by **reading** it.
  Spontaneous attribute reports are not a channel at all: a Zigbee light reports colour only
  when someone bound and configured that reporting, which Zigbee2MQTT does for on/off and
  level but not for colour (the Hue lights that did report on the reference installation did
  so through a leftover binding of Hue's private cluster, which Zigbee2MQTT stopped creating in
  2026-05 because those reports carry in-between states). A read of the colour attributes
  0.4 s after the command is answered by every device tried, in under 0.1 s.
* Take an answer only once a **second read agrees** with it. A device with a ramp of its own
  answers the first read part-way along it, and a single read would take that for the
  device's clip; two that disagree mean it is still moving, and it is read again until two
  agree or the sample runs out with no answer.
* Recognise the transport's optimistic echo **by value** before the read, and trust the value
  the read brings back even when it equals the command; the one exception is the colour from
  before the command, which is the read landing early or a device that will never apply it
  (``unapplied``), and is read again.
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
    ct_range_k: [2000, 6535]              # measured with it; see section 11
```

Validation rejects a polygon that is not convex, not counter-clockwise, too small, or on a
material without the `xy` capability; a `rgb_clamp` rule on anything but a triangle; an
`inherited_from` that does not name a measured fixture of the same material model label; two
measured units of one label further apart than 0.02 (one measurement is wrong, or the label
is); and a `ct_range_k` that is not an ordered pair inside 1000-20000 K or sits on a material
without `cct`. `fixture_clip(model, id)` resolves a fixture to its polygon and rule, or to the
identity; `fixture_cct_range` to its measured range, else its material's declaration;
`with_fixture_gamut` records a measurement as a functional update, the way the tuning UI
writes the model.

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

**Invisible, or forced.** A dark fixture holds each probe colour only for the length of a
sample (a read-back 0.4 s after the command and a second one at once to confirm it, re-read
while the device has not applied or settled; a fixture takes about fifteen seconds). Three
things end a measurement early, restore the fixture
to its intended colour with *no* transition, and withhold the polygon: the room's presence
entity turning on (watched through the host, ahead of the render that follows presence), a
command that turns the fixture on (a foreign `state: ON` on its own topic or any group it is
in), and the device reporting itself on. So a fixture turned on during a measurement comes on
at its intended colour, except for the one path that gives no warning, a switch bound straight
to the bulb, where the probe colour shows until the state report arrives (a few hundred
milliseconds). The forced mode (`--force`) measures whatever the fixture's state and whoever
is in the room, visibly; a transport that cannot carry colour to a dark fixture (section 7's
entity channel) is measured in that mode only.

**What a device can do wrong is diagnosed, not endured.** Consecutive probes clip to
different places, so a device that answers its first two commands with the colour it was
resting at has not applied either: it does not apply colour while off (Hue does, under its
execute-if-off default; many other makes do not). The measurement stops there with that
reason, and in the forced mode it instead switches the fixture on, starts over, puts the
colour back and turns it off again (colour first, since such a device would otherwise come
back on showing the last probe). Two more diagnoses end an opening the same way: no answer
to any read-back (not reachable this way, which a busy transport can also look like, so the
loop gives it a second tick before setting it aside) and a device still changing colour when
every sample runs out (a ramp too slow to measure). Each is the verdict's `aborted`, with
`NOT_APPLIED_WHILE_OFF`, `NOT_APPLIED_LIT`, `NO_READ_ANSWER` and `NEVER_SETTLES` as the
prefixes a caller can test. Nothing here guesses: a device the measurement cannot read ends
with no polygon and the identity comparator, never a wrong one.

`pascl.shell.gamut_runtime.tick` is one pass: read the host through the binding (which light
entities are on, which presence entities are on), read each coordinator's device list, pick,
reach the fixture the way its transport allows (`channel_for`), measure with the room's
presence watched, refuse a record the model would not validate with, write the model, let the
measurement travel. `run` is the loop; a fixture a tick sets aside (no way in, forced mode
only, or one of the diagnoses above) is not picked again in that run.

    HA_TOKEN=... pascl gamut auto --model home.yaml --binding binding.yaml \
        --ha-url http://homeassistant:8123 --write home.yaml [--interval 300] [--once] [--force]

`pascl gamut status --model F [--device-ids TSV]`, `pascl gamut pick ...`, `pascl gamut
inherit ...` and `pascl gamut plan` are the pure views from the command line; `pascl gamut ids`
prints every fixture's device identity, model id and firmware from the coordinators, in the
shape `status` and `pick` take.

## 6. Device identity (`pascl.shell.z2m`, `pascl.shell.ha_light`)

The identity a polygon is bound to is the device's IEEE address as its Zigbee2MQTT coordinator
lists it in the retained `bridge/devices`, suffixed with the endpoint for a fixture that is one
endpoint of a device. A topic is a name the user can change; the address survives a rename and
changes with the hardware, which is exactly what a bound polygon needs. The same list carries
the firmware build, recorded with the measurement as information for a reader comparing units,
never as a staleness trigger. The retained `bridge/groups` names the groups a device belongs
to, whose `/set` topics the measurement watches as foreign. A light reached through a host
entity is bound to the entity registry's `unique_id` (a Matter node, a bridge's light id),
with the device registry's software version as its firmware.

## 7. Running measurements (`pascl.shell`): channels, links, the hub

The measurement talks to a fixture through a `DeviceChannel` (command a colour, read it back,
observe, restore, switch on for a forced measurement, identity, firmware, whether a dark
fixture can be reached). Two channels exist:

* `pascl.shell.z2m`, one Zigbee2MQTT light or endpoint: the `/set` and `/get` topics, the echo,
  read-back and unapplied rules, the endpoint rule, the foreign-command, lit and occupancy
  guards, snapshot, `light_up` and restore. A colour reaches a dark device (and one that
  ignores it stays dark and is diagnosed as such), so this is the channel of the invisible
  measurement.
* `pascl.shell.ha_light`, any light the host exposes as an entity (Matter, a Hue bridge, ZHA,
  Lutron's colour devices if any: the transport underneath does not matter): `light.turn_on`
  with no transition, `homeassistant.update_entity` to read back, the entity's state changes
  relayed by the link. `light.turn_on` turns a dark fixture on, so this channel is measured in
  the forced mode only. A dimmer-only load (a Lutron Caseta dimmer, say) has no xy capability
  and never reaches the roster; a colour-capable Lutron device (the Ketra line) comes through
  this channel, or a native LEAP channel later, in the forced mode for the same reason.

The runtime chooses the channel by the fixture's transport (`channel_for`), and the airtime
key it shares: the coordinator's base topic for Zigbee2MQTT, the transport id otherwise.

`pascl.shell.gamut_measure` drives a `Probe` over a channel with the timing rules, against the
injected clock, and restores the fixture whatever happens. Links: `pascl.shell.ha_mqtt`
through Home Assistant (REST `mqtt.publish`, the `mqtt/subscribe` WebSocket, entity watches
and service calls on the same socket, `pip install pascl[ha]`) and `pascl.shell.paho_mqtt`
straight to a broker (`pip install pascl[mqtt]`).

**Many at once.** One link is one socket, and a channel drains it, so `pascl.shell.hub` fans
one link out: every channel gets a view with its own queue, one reader thread routes each
message to the views subscribed to its topic, and `measure_many` runs one thread per fixture.

**How many at once is measured, not assumed** (`pascl.shell.airtime`). A coordinator's
airtime budget depends on the radio, the mesh, the firmware and whatever else is on the air,
and the evidence is in every sample: a device that answers its first read-back within the
latency budget is on a transport with room to spare; one that needs a second read, or none,
is on one that is queuing. So the number in flight per key (a coordinator's base topic, a
transport id) is a window driven the way TCP drives its own: additive increase after a streak
of first-read answers under the budget, multiplicative decrease on the first retry or timeout,
from one up to a ceiling (`--parallel N`). A decrease never interrupts a measurement already
running; it stops the next launch until the window has room. `measure_adaptively` reports
what each window did. On the reference installation the window on the living-room
chandelier's coordinator climbed from one to six within the first minute and measured all
fifteen bulbs in 68 s with 355 of 360 samples prompt, three times the budget the earlier
estimate had allowed. A fixture on a busy coordinator is still best measured with its render
held: the forced pass over 43 lit fixtures took 2 m 35 s with the rooms' renders paused.

    HA_TOKEN=... pascl gamut measure --model home.yaml --binding binding.yaml \
        --fixture den_strip --fixture den_pendant_1 [--parallel 6] [--force] \
        --ha-url http://homeassistant:8123 --write home.yaml

reaches each fixture the way its transport allows, measures them in rounds, prints each
verdict with every rule's fit, says whether a seed it held is confirmed, and records the
polygons in the model (refusing any the model would not validate with). Measured this way on
the reference installation's storeroom strip (off, 24 of 24 answers): the same triangle its
provisioning tool had recorded, to 2e-05. That tool is now a wrapper over these modules.

## 8. What the palettes can show (`pascl palette check`)

    pascl palette check --model home.yaml

lists every palette colour some measured fixture cannot show, with the colour that renders
instead: static entries per family (bulbs, strips) and, for the solar white palettes, the
worst excursion along each calibration's warm-to-cool and warm-to-night arc. Fixtures that
share a polygon and rule are reported together. Nothing at runtime depends on it (the
comparators are exact about reachability); it makes the author's choice visible, and the
exit status is non-zero when there is one.

The same report covers colour temperature (section 11): the part of the solar arc a
white-only fixture (`cct` without `xy`) parks at its range's end, since it has no xy to fall
back to; and every range in force that is not credible for a physical emitter (outside
1500-10000 K), which is a transport's placeholder to be measured, not a colour to move. Both
are information: the parked arc is the render's own floor, and neither fails the check.

## 9. When to judge: the report cadence (pascl.estimator.reporting)

A comparator that judges a fixture against its commanded colour has a second question after
WHERE the device can land: WHEN it has landed. A command with a transition keeps the device
moving for that long, and the device says where it is only at its own report cadence, so a
comparator armed at a fixed delay races the last report and wins some of the time, re-sending
the colour the fade has just reached (on the reference installation, one 30 s crossfade in ten,
about 180 no-op frames a day, and a visible snap mid-fade at any transition longer than the arm).

The cadence is not declared: Zigbee2MQTT configures level and on/off reporting on those bulbs and
nothing for colour, and the ~10 s colour interval is the firmware's own. So it is observed, in
the same spirit as the polygon. estimate takes fades (a transition and the times the fixture
reported a NEW colour after the command) and returns each fixture's report interval as the median
gap between consecutive reports inside the fade, with the gaps it rests on and the fraction on
cadence. It declines rather than guesses: too few gaps, no colour reports at all (a device whose
transport only ever echoes the command), or gaps that describe no single interval (a 5 / 10 / 20 s
mix). inherit lets a measured value seed the unmeasured units of the same model label, naming the
source, and refuses a label whose units disagree; it never seeds a fixture that was commanded and
reported nothing, because that fixture's own evidence says it does not report.

With the interval known, the settling report is predictable from the fixture's last report: the
first report slot (last report + k * R_f, k >= 1) at or after the fade's end. One slot ahead is
not enough in general: a comparator with a fixed delay trips a whole slot before the end of a
long transition, and the slot after that is the settling one (on the reference installation a
one-slot prediction armed 0.9 s before the bulbs' settling slot at a 40 s transition, sixteen
no-op frames in four ticks, none mid-fade). What lands beyond that prediction is the host's delivery
jitter, not the device's, and jitter takes a high quantile of it home-wide over the trips a
fixed-delay comparator would have lost (declined below MIN_TRIPS). replay says what arming at
prediction plus jitter would have done on those trips: how many still arm early, how long the
consumer waits, when a genuinely stuck fixture is acted on. stands_down names the regime where
the arm is longer than the interval at which the fixture is re-commanded, so the comparator can
only ever interrupt a fade there and should say so per fixture rather than fall silent.

The failure direction is the design: an unknown interval or jitter contributes nothing, so a
consumer arms at the fade end as before (no-op frames at worst, never late for a stuck device),
and a value can only ever move the arm later. On the reference installation the two-evening
derivation measured 10.04 s on every Hue strip, spot and BR30 and 10.37 s on the A19/A21 bulbs,
a jitter of 3.9 s at the 99th percentile, and zero to two false arms in 223 trips where the fixed
delay had lost all of them.

## 10. After the fade, the runtime reads (`pascl.core.settle`)

Section 9 answers WHEN a device that reports will have reported. The reference installation
then established that reporting is not something a transport promises: Zigbee2MQTT configures
level and on/off reporting on a bulb and nothing for colour; the fixtures there that report a
colour at all do so through a leftover binding of Hue's private cluster that the coordinator
stopped creating in 2026-05, and 27 of the 85 never report one, so for those the host holds the
transport's echo of the last command and a comparator fed by reports is blind. A cadence
derived from observation is a model of a behaviour the transport does not owe anyone.

So the runtime asks. After a render command's fade has ended it reads the colour back, once,
with the same `/get` the measurement uses (`READ_COLOUR_PAYLOAD` in `pascl.shell.z2m`: one
ZCL frame answering colour mode, xy and colour temperature together, the device's whole state
republished on its base topic), and the comparator arms after the answer has had time to land.
The rule, and the numbers the reference installation's generated templates carry verbatim:

- the fade ends at the command plus its transition (`fade_end`), and a fixture with several
  commands in flight stops moving at the latest of them (`fade_end_of`), whichever channel each
  moved: brightness and colour are separate commands on most transports, and the comparator
  judges the fixture. The reference installation derives that fade end from three timestamps
  plus the scene transition (its scope's settling stamp when the fixture was commanded by that
  render, the fixture's colour cache write, its brightness clock write); until 2026-09-18 the
  brightness one was missing and a brightness-only fade whose scope a later, shorter pass had
  re-stamped repainted at its fade end with no hold (a no-op frame);
- the read goes out `READ_MARGIN_S` (1.5 s) after that, covering the device's coarse
  transition ticks and a host whose command time runs ahead of the frame (`read_at`);
- the comparator arms `READ_ALLOWANCE_S` (4.0 s) after the read: the queue when every colour
  fixture on one radio was commanded in the same render, plus a transport retry (`arm_at`);
- a read is issued only when it can say something (`worth_reading`): the fixture is lit and its
  colour intent moved past the comparator's tolerance on some axis since the previous command.
  A fade that moved the intent by less cannot produce a divergence the comparator would act on.
  On the reference installation that gate keeps about one colour command in twenty-five, some
  2,300-3,000 reads a day fleet-wide, each a unicast request and answer of 50-80 ms;
- one due time per fixture (`ReadSchedule`): a fixture commanded again before its read is due
  is read once, after the later fade.

The failure direction is unchanged from before reads existed: an unanswered read leaves the
comparator with what the host already held, and only the allowance is waited for beyond the
fade. An answered read replaces the echo with the device's own state, which is what makes the
comparator honest on fixtures that never report, and it carries the level too, so the brightness
comparator (`levels.md`) gets an exact device-unit reading from the same frame.

Section 9's estimator stays as a diagnostic of a transport's reporting, and as the arm's input on
an installation that chooses to trust reports; on the reference installation the arm is this
section's, and the report interval and jitter describe the reports it no longer waits for.

## 11. The colour-temperature range is derived, tier by tier (`Gamut.ct_range_k`, `ct_sources`)

A fixture that takes a colour temperature clips one outside its range exactly as it clips a
chromaticity outside its polygon, and the range the host declares is what the device advertises,
not what it does. On the reference installation 22 bulbs of one family advertise 1000-20000 K
over a physical ~2000-6500 K; the render, flooring the night white at the declaration, sent them
1475 K and the emitters showed 2000 K. So the range is DERIVED, like the polygon, and never
looked up: each end is the answer of the first tier in an ordered chain of device evidence that
can speak for it, and the tier is recorded with the value (`ct_sources`, one entry per end, P10
of DERIVED_PARAMETERS). Every tier is the polygon's rule in one dimension, command past the end
and observe where the device lands; what differs between tiers is only HOW the landing is
observed, which is the transport's business, never what the value is.

1. **Probed** (`probed`). `CT_PROBES_MIRED` (50 and 1000 mired, coolest first) commanded and
   read back under the polygon's timing rules (`take_ct_sample`): a device that clips its
   attribute answers its limits. On a Zigbee2MQTT transport the 63 bulbs declaring 153-500
   mired answer exactly that, because the transport clamps commands to the declared limits
   before sending; the answer is the transport's clamp of the device's declaration, and it
   agrees with the declaration by construction.
2. **Raw chromaticity under a colour-temperature command** (tested, declines on Hue, NOT
   built). If a device's `currentX` / `currentY` attributes followed the emitter, a dark
   `color_temp` command past the floor followed by an attribute read would give the floor's
   chromaticity, invisibly, and a CCT from it. Live on an LCA014 (2026-09-17): 1000 mired
   stored `(0.6528, 0.3445)`, the Planckian point of 1000 K; 50 mired stored `(0.2539,
   0.2602)`, 20000 K's. The chromaticity attributes follow the STORED colour temperature, not
   the emitter, so this tier has nothing to say on a Hue and is not implemented; it is recorded
   here so no one measures it twice.
3. **Observed** (`observed`, `pascl.estimator.ct_observed`). Any report channel that carries
   the emitter's value (a native push that clamps, a bridge, a firmware that clamps the
   attribute on the way out) yields (commanded, reported) pairs while the runtime does what it
   does every night, command past the floor. The estimator takes the floor as the value the
   device reports when commanded warmer than it, consistently, the ceiling likewise, and
   declines per end as a first-class answer: no clip observed (the pairs cannot tell an
   unexercised end from a channel that repeats the command, so the reason names the extreme
   command the device repeated), too few clipped pairs (`MIN_SUPPORT`), clipped reports that
   disagree (`SPREAD_TOL`), reports past the candidate (`CONTRADICTION_RATIO` of the support),
   or a value outside `CREDIBLE_KELVIN`. Thresholds are ratios (P3), authority counts only
   the pairs that show the clip (P7), a published end carries its support and its
   contradictions. `pascl gamut observe-ct --pairs` records a published end on the gamut where
   the end it holds came from a weaker tier (`ct_observed.apply`: a probed end stands, with a
   note if they disagree). On the reference installation this tier DECLINES for the 22: the
   attribute is stored unclipped and the native state push carries the same attribute, so
   nine days of the night arc in the store (`tests/fixtures/ct_pairs_echo.json`, one bulb,
   889 pairs to 676 mired) show every report repeating its command. That fixture is the
   suite's real-data case (P8) and it must stay a decline.
4. **Declared** (`declared`). The limits the device declares, `colorTempPhysicalMin` / `Max`,
   read in one frame (`read_ct_limits`; on a host entity its `min_color_temp_kelvin` /
   `max_color_temp_kelvin`), taken when credible (`CREDIBLE_KELVIN`, 1500-10000 K). A bulb
   declaring 50-1000 mired has declared the attribute's whole span, not its emitter, and is
   declined by name. A lit fixture whose limits are credible is not probed at all, since the
   probes would flash it for a check the dark case makes invisibly.
5. **Inherited** (`inherited`). An end no tier could speak for on this fixture, copied from a
   fixture of the same model label whose own end has a source (`inherit_ct`; a polygon seed
   copies the range with the polygon, `inherit`). Exact from its first minute, and the
   fixture's own observation replaces it.
6. **The author** (`author`, `Material.cct_range_k`). The last tier, outside the gamut record:
   the material's declaration, the owner's word from a specification sheet, which
   `fixture_cct_range` falls through to for an end nothing else bounds, labelled as such by
   `fixture_cct_sources`. It is a first-class tier with four properties, because a product
   that only measures has no honest state for "the transport proved this cannot be
   observed" and a product that only declares launders placeholders:
   - *Declared on the model label, not per fixture.* One line on the material, where a
     product's UI would ask ("this bulb model's white range"), and every unit of the label
     takes it, as polygon seeds do.
   - *Labelled `author` at every consumer* (`fixture_cct_sources`, the render's
     `cct_floor`, `pascl palette check`), so a reader can always tell it from `probed`,
     `observed`, `declared` and `inherited` (P10).
   - *Falsifiable.* A fixture's own probed or observed end outranks it the day a firmware or
     a transport reports the emitter (`fixture_cct_range` resolves the gamut's end first),
     and `ct_consistency` reports an author value a fixture's own end contradicts by more
     than `AUTHOR_CT_TOL`, the way `confirm_seed` reports a polygon seed a measurement
     contradicts.
   - *Asked for only after the chain has proven unobservability, with the proof beside the
     answer.* Every tier that declines an end records a `CtDecline` on the gamut (tier, end,
     verdict, reason). The verdict is `unobservable` when the evidence proves the tier can
     never bound that end on this device (a probe answered with its own value; a declaration
     that is the attribute's whole span; a report channel that repeated a command past any
     emitter's end, warmer than `CREDIBLE_KELVIN`'s floor), and `inconclusive` when the tier
     got no answer it could judge (a probe unanswered, no limits read, an arc that never
     reached the end, pairs too few or too noisy). An unsourced end is UNOBSERVABLE
     (`unobservable`) only when every tier in `CT_TRIED_TIERS` declined it with proof; with a
     tier untried or inconclusive it is NOT KNOWN YET (`untried` names the tiers), a
     measurement or an observation away, never a question for the author. The two states
     never read alike (P2). `pascl palette check` carries the question only in the first,
     once per material, with the proof: "what is the white range of material X? Nothing on
     this transport can bound it". The author tier is never populated from a transport: the
     reference installation's private model once carried the device's 1000-20000 K
     placeholder in the author's slot, and the check flags exactly that as a placeholder to
     replace with the vendor's value, which is what was done.

**The render's policy floor** (`cct_floor`, `pascl.core.render`). What a fresh install shows
before the author answers matters: a room rendering mixed whites and ambers reads as broken. An
unbounded floor therefore renders whites the way its co-rendered bounded siblings do, the highest
known floor among the room's other colour-temperature fixtures, else `DEFAULT_MIN_KELVIN`
(2000 K, the warmest white most tunable emitters reach). It is labelled `policy`, needs no author,
is a render rule never written into the fixture's range, fails in the safe visible direction (a
calibrated amber in xy rather than a colour temperature the device clips to a wrong white; the
credible band's own floor, 1500 K, would fail the other way), and the author's value replaces it
the moment it is declared. The 22 bulbs below are the case that motivated it.

What the reference installation taught, in two evenings: no Hue bulb clips the colour-temperature
ATTRIBUTE, lit or dark. It stores what it is sent and answers a read with that (every one of the
22 answered 50 and 1000 to the two probes, lit as well as dark); its chromaticity attributes are
the Planckian point of that stored value; and its native state push carries the stored value
too. The emitter clips physically and nothing on the wire says where: on this transport the
floor is unobservable, not merely unobserved, and that distinction is the product answer. For
such a device the chain declines all the way to the author with proof at every tier, the
question is asked once per model label, and the owner's answer (the vendor's 2000-6500 K,
declared 2026-09-17) is carried as `author`, falsifiable, on the material. An earlier note in
this file and in the estimator claimed the native push reported the clamped value (501 mired for
a commanded 678); the store B pairs refute it and it is withdrawn.

`ct_range_source` (tiers 1 and 4, from one measurement) and `ct_observed.observe` (tier 3, from
pairs) are the pure judges; `ct_declines` and `ct_observed.apply` write the declines;
`measure_ct` runs the measurement and `Verdict.ct_source` / `Verdict.ct_declined` carry its
tier and its declines; `gamut_from` writes them on a fresh gamut and `with_measured_ct` folds a
later measurement into a held one tier by tier (a probed range replaces everything, a declared
one fills only weaker ends, a declined one changes no end and records its declines beside the
observed tier's). The model validates a range against its sources: an end with no source is
unbounded and carries the span end (`KELVIN_MIN` / `KELVIN_MAX`), at least one end of a recorded
range has a source, a tier never both supplies an end and declines it, and `fixture_cct_range`
resolves each end on its own (gamut tier, else the material, else None). Consumers: the render
floors a white at the resolved floor, else the policy floor; a comparator judges the device
against the intent clipped to the resolved range, an unknown end clipping nothing, as it judges
chromaticity against the intent clipped to the polygon. A material without `cct` is never probed (the runtime passes
`ct=False`); an interruption during the range stage keeps a polygon already measured and declines
the range with a note; `pascl gamut measure --ct-only` runs tiers 1 and 4 alone, in seconds, for
a fixture whose polygon is on record.

Two rules of DERIVED_PARAMETERS worth naming for this parameter. P6: the precision any tier needs
is set by the consumers, a comparator's 50 K band and the render's floor decision, which the
observed tier's ratio tolerances meet comfortably at 2000 K (1% of 500 mired is 20 K). P10:
every published end carries its tier, so a reader of the model can always tell probed from
observed from declared from inherited from the author's word.
