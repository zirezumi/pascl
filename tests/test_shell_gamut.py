"""The measurement driver over a simulated device channel, and the Zigbee2MQTT channel's
message rules against the message shapes real devices produced."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime

from pascl.clock import ManualClock
from pascl.core.gamut import XY, Polygon, project
from pascl.shell.gamut_measure import (
    ANSWER_TIMEOUT,
    DIAGNOSE_AFTER,
    MAX_READS,
    NEVER_SETTLES,
    NO_READ_ANSWER,
    NOT_APPLIED_LIT,
    NOT_APPLIED_WHILE_OFF,
    READ_AFTER,
    Observation,
    gamut_from,
    measure,
    take_sample,
)
from pascl.shell.z2m import (
    Z2MDeviceChannel,
    bridge_devices,
    group_set_topics,
    parse_bridge_devices,
    parse_bridge_groups,
    split_set_topic,
)

TRIANGLE: Polygon = ((0.153185, 0.047547), (0.691493, 0.308293), (0.169986, 0.699992))
QUAD: Polygon = ((0.1532, 0.0475), (0.40, 0.12), (0.6915, 0.3083), (0.17, 0.70))


class FakeDevice:
    """A device channel that clips to a hidden polygon. It echoes every command at once and
    answers with its real colour only when read (``reports`` False), or on its own after a
    delay (``reports`` True), the two behaviours real devices showed. ``observe`` advances the
    injected clock as a blocking call would."""

    identity = "fake-device-1"
    firmware = "1.116.3"
    needs_lit = False

    def __init__(
        self,
        clock: ManualClock,
        poly: Polygon,
        *,
        reports: bool = False,
        lit: bool = False,
        foreign_at: int | None = None,
        lit_at: int | None = None,
        ct_range: tuple[int, int] | None = (153, 500),
    ) -> None:
        self.clock = clock
        self.poly = poly
        self.reports = reports
        self.lit = lit
        self.foreign_at = foreign_at
        self.lit_at = lit_at
        self.pending: list[tuple[float, Observation]] = []
        self.commands = 0
        self.ct_commands = 0
        self.reads = 0
        self.restored = False
        self.lit_up = 0
        self.current: XY = (0.4, 0.4)
        # the hidden colour-temperature range, mireds (coolest, warmest); None: no such thing,
        # a colour temperature is ignored and a read answers the resting value
        self.ct_range = ct_range
        self.current_ct = 370
        self.ct_in_flight = False

    def is_lit(self) -> bool | None:
        return self.lit

    def command_ct(self, mired: int) -> None:
        self.ct_commands += 1
        self.ct_in_flight = True
        self.pending.append((self.clock.monotonic() + 0.05, Observation("echo")))
        if self.ct_range is not None:
            self.current_ct = min(max(mired, self.ct_range[0]), self.ct_range[1])

    def command(self, xy: XY) -> None:
        self.commands += 1
        self.ct_in_flight = False
        now = self.clock.monotonic()
        self.pending.append((now + 0.05, Observation("echo")))
        self.current = project(xy, self.poly)
        if self.reports:
            self.pending.append((now + 0.4, Observation("device", self.current)))
        if self.foreign_at is not None and self.commands == self.foreign_at:
            self.pending.append((now + 0.3, Observation("foreign")))
            self.foreign_at = None
        if self.lit_at is not None and self.commands == self.lit_at:
            self.pending.append((now + 0.3, Observation("lit")))

    def read(self) -> None:
        self.reads += 1
        when = self.clock.monotonic() + 0.05
        if self.ct_in_flight:
            self.pending.append((when, Observation("device", trusted=True, mired=self.current_ct)))
            return
        # a read-back is the device's own value, even when it equals the command
        self.pending.append((when, Observation("device", self.current, trusted=True)))

    def light_up(self) -> bool:
        self.lit_up += 1
        self.lit = True
        return True

    def observe(self, seconds: float) -> list[Observation]:
        self.clock.advance(seconds)
        now = self.clock.monotonic()
        due = [ob for t, ob in self.pending if t <= now]
        self.pending = [(t, ob) for t, ob in self.pending if t > now]
        return due

    def restore(self, transition: float = 0.2) -> None:
        self.restored = True
        self.restore_transition = transition


def _clock() -> ManualClock:
    return ManualClock(datetime(2026, 9, 17, 14, 0, tzinfo=UTC))


def test_measure_recovers_the_polygon_by_reading_back() -> None:
    clock = _clock()
    dev = FakeDevice(clock, TRIANGLE)
    v = measure(dev, clock=clock)
    assert v is not None and v.polygon is not None
    for hidden in TRIANGLE:
        assert min(abs(hidden[0] - h[0]) + abs(hidden[1] - h[1]) for h in v.polygon) < 1e-5
    assert v.unanswered == 0
    # never answered before the read, so every sample read, and every answer was confirmed
    # by a second read that agreed; a device that applies at once is never switched on
    assert dev.reads == 2 * (dev.commands + dev.ct_commands) and dev.lit_up == 0
    assert not any("read-back" in n or "changing" in n for n in v.notes)
    assert dev.restored
    # the colour-temperature range came with it: 153 and 500 mired are 6535 and 2000 K
    assert dev.ct_commands == 2 and v.ct_range_k == (2000, 6535)
    assert v.ct_answers == ((50, 153), (1000, 500))
    g = gamut_from(v, dev, clock)
    assert g is not None and g.bound_to == "fake-device-1" and g.measured is not None
    assert g.vertices == v.polygon
    assert g.clip_rule == "closest" and g.firmware == "1.116.3" and g.inherited_from is None
    assert g.model_error == v.model_error and g.ct_range_k == (2000, 6535)


def test_the_colour_temperature_range_has_its_own_switches_and_declines_honestly() -> None:
    clock = _clock()
    # a material without cct: no probe goes out and no range is recorded
    dev = FakeDevice(clock, TRIANGLE)
    v = measure(dev, clock=clock, ct=False)
    assert v is not None and v.polygon is not None
    assert dev.ct_commands == 0 and v.ct_range_k is None and v.ct_answers == ()
    # the range alone, for a fixture whose polygon is already known: no colour probe
    dev = FakeDevice(clock, TRIANGLE)
    v = measure(dev, clock=clock, xy=False)
    assert v is not None and v.polygon is None and v.aborted is None
    assert dev.commands == 0 and dev.ct_commands == 2 and v.ct_range_k == (2000, 6535)
    assert dev.restored and gamut_from(v, dev, clock) is None
    # a device that ignores a colour temperature answers its resting value to both probes:
    # not a range, declined with the evidence in the notes
    dev = FakeDevice(clock, TRIANGLE, ct_range=None)
    v = measure(dev, clock=clock)
    assert v is not None and v.polygon is not None and v.ct_range_k is None
    assert v.ct_answers == ((50, 370), (1000, 370))
    assert any("range declined" in n and "50->370" in n for n in v.notes)
    # a device that takes anything it is sent (or a transport echoing the command as the
    # device's answer) claims 1000-20000 K: not a physical limit, declined
    dev = FakeDevice(clock, TRIANGLE, ct_range=(1, 100000))
    v = measure(dev, clock=clock)
    assert v is not None and v.ct_range_k is None and v.ct_answers == ((50, 50), (1000, 1000))
    # the range probes never run after an aborted polygon measurement
    dev = FakeDevice(clock, TRIANGLE, lit_at=3)
    v = measure(dev, clock=clock)
    assert v is not None and v.aborted is not None and dev.ct_commands == 0


def test_an_interruption_during_the_range_probes_keeps_the_polygon() -> None:
    clock = _clock()

    class LitDuringCt(FakeDevice):
        def command_ct(self, mired: int) -> None:
            super().command_ct(mired)
            self.pending.append((self.clock.monotonic() + 0.3, Observation("lit")))

    dev = LitDuringCt(clock, TRIANGLE)
    v = measure(dev, clock=clock)
    assert v is not None and v.polygon is not None and v.aborted is None
    assert v.ct_range_k is None and dev.ct_commands == 1
    assert any("range not measured: fixture turned on" in n for n in v.notes)
    assert dev.restored and dev.restore_transition == 0.0  # put back at once
    # the same interruption on a range-only run is the run's abort
    dev = LitDuringCt(clock, TRIANGLE)
    v = measure(dev, clock=clock, xy=False)
    assert v is not None and v.aborted is not None and "turned on" in v.aborted


def test_the_read_back_ends_the_sample_and_a_spontaneous_report_is_a_fallback() -> None:
    clock = _clock()
    dev = FakeDevice(clock, TRIANGLE, reports=True)
    t0 = clock.monotonic()
    v = measure(dev, clock=clock)
    assert v is not None and v.polygon is not None and len(v.polygon) == 3
    # every sample read back and confirmed (the report came at the same instant and did not
    # replace it), and a fixture takes well under a minute: the exposed window per sample is
    # the read delay plus one round trip
    assert dev.reads == 2 * (dev.commands + dev.ct_commands)
    assert clock.monotonic() - t0 < (24 + 2) * (READ_AFTER + 0.5)

    class ReadsUnanswered(FakeDevice):
        def read(self) -> None:
            self.reads += 1

    dev2 = ReadsUnanswered(clock, TRIANGLE, reports=True)
    v2 = measure(dev2, clock=clock)
    assert v2 is not None and v2.polygon is not None and len(v2.polygon) == 3
    # every read retried, the report carried the sample
    assert dev2.reads == 3 * (dev2.commands + dev2.ct_commands)


def test_probe_inside_the_true_gamut_is_answered_by_the_read_back() -> None:
    """The edge probe outside a cut corner falls inside a four-primary gamut; the device
    shows it exactly, which by value looks like the echo. The read-back is trusted, the
    corner is found, and the measurement terminates."""
    clock = _clock()
    dev = FakeDevice(clock, QUAD)
    v = measure(dev, clock=clock)
    assert v is not None and v.polygon is not None and len(v.polygon) == 4
    for hidden in QUAD:
        assert min(abs(hidden[0] - h[0]) + abs(hidden[1] - h[1]) for h in v.polygon) < 1e-5
    assert v.unanswered == 0 and v.aborted is None


def test_fixture_turning_on_aborts_and_withholds_the_polygon() -> None:
    clock = _clock()
    dev = FakeDevice(clock, TRIANGLE, lit_at=5)
    v = measure(dev, clock=clock)
    assert v is not None and v.polygon is None and v.aborted is not None
    assert "turned on" in v.aborted and any("aborted" in n for n in v.notes)
    assert dev.commands == 5 and dev.restored and dev.restore_transition == 0.0
    assert gamut_from(v, dev, clock) is None
    # a fixture measured lit on purpose is not aborted by its own ON state, and a sample is
    # not cut short by it either: the read-back still answers every probe
    dev2 = FakeDevice(clock, TRIANGLE, lit=True, lit_at=5)
    v2 = measure(dev2, allow_lit=True, clock=clock)
    assert v2 is not None and v2.polygon is not None and v2.aborted is None
    assert v2.unanswered == 0 and dev2.reads == 2 * (dev2.commands + dev2.ct_commands)

    class AlwaysLit(FakeDevice):
        def command(self, xy: XY) -> None:
            super().command(xy)
            self.pending.append((self.clock.monotonic() + 0.01, Observation("lit")))

    dev3 = AlwaysLit(clock, TRIANGLE, lit=True)
    v3 = measure(dev3, allow_lit=True, clock=clock)
    assert v3 is not None and v3.polygon is not None and v3.unanswered == 0


def test_abort_when_stops_between_samples() -> None:
    clock = _clock()
    dev = FakeDevice(clock, TRIANGLE)
    calls = 0

    def room_filled() -> str | None:
        nonlocal calls
        calls += 1
        return "room den became occupied" if calls > 3 else None

    v = measure(dev, clock=clock, abort_when=room_filled)
    assert v is not None and v.polygon is None and v.aborted == "room den became occupied"
    assert dev.commands == 3 and dev.restored


def test_lit_fixture_is_skipped_unless_allowed() -> None:
    clock = _clock()
    dev = FakeDevice(clock, TRIANGLE, lit=True)
    assert measure(dev, clock=clock) is None
    assert dev.commands == 0 and not dev.restored  # nothing was touched, nothing to put back
    v = measure(dev, allow_lit=True, clock=clock)
    assert v is not None and v.polygon is not None


def test_foreign_command_makes_the_sample_be_taken_again() -> None:
    clock = _clock()
    dev = FakeDevice(clock, TRIANGLE, foreign_at=3)
    v = measure(dev, clock=clock)
    assert v is not None and v.polygon is not None
    assert dev.commands == 24 + 1


def test_take_sample_times_out_on_a_silent_device() -> None:
    clock = _clock()

    class Silent(FakeDevice):
        def read(self) -> None:
            self.reads += 1

    dev = Silent(clock, TRIANGLE)
    t0 = clock.monotonic()
    s = take_sample(dev, (0.95, 0.04), clock)
    assert s.xy is None and s.echo_only and not s.foreign and not s.unapplied
    assert dev.reads == MAX_READS
    assert READ_AFTER <= clock.monotonic() - t0 <= ANSWER_TIMEOUT + 0.3
    # a whole measurement does not endure it: two silent samples and it stops, with the reason
    dev2 = Silent(clock, TRIANGLE)
    v = measure(dev2, clock=clock)
    assert v is not None and v.polygon is None and v.aborted is not None
    assert v.aborted.startswith(NO_READ_ANSWER) and dev2.commands == DIAGNOSE_AFTER
    assert dev2.restored


class Ramps(FakeDevice):
    """A device with a ramp of its own: it slides from the colour it showed to the clipped
    target over ``ramp_s`` seconds whatever transition was commanded, and a read answers
    wherever it is at that moment."""

    def __init__(self, clock: ManualClock, poly: Polygon, *, ramp_s: float) -> None:
        super().__init__(clock, poly)
        self.ramp_s = ramp_s
        self.origin: XY = self.current
        self.target: XY = self.current
        self.t0 = clock.monotonic()

    def command(self, xy: XY) -> None:
        self.origin = self.at(self.clock.monotonic())
        super().command(xy)
        self.target = self.current
        self.t0 = self.clock.monotonic()

    def at(self, now: float) -> XY:
        f = min(1.0, (now - self.t0) / self.ramp_s)
        return (
            self.origin[0] + f * (self.target[0] - self.origin[0]),
            self.origin[1] + f * (self.target[1] - self.origin[1]),
        )

    def read(self) -> None:
        self.reads += 1
        now = self.clock.monotonic()
        self.pending.append((now + 0.05, Observation("device", self.at(now), trusted=True)))


def test_a_device_with_its_own_ramp_is_read_until_it_settles() -> None:
    """The first read lands part-way along the ramp and the confirming read disagrees; the
    driver keeps reading until two agree, and the answer is where the device settled, so the
    polygon is exact. A single read would have taken the part-way value for the device's
    clip."""
    clock = _clock()
    dev = Ramps(clock, TRIANGLE, ramp_s=1.0)
    s = take_sample(dev, (0.95, 0.04), clock)
    assert s.trusted and s.confirmed and s.moved and s.xy is not None
    assert abs(s.xy[0] - TRIANGLE[1][0]) < 1e-6 and abs(s.xy[1] - TRIANGLE[1][1]) < 1e-6
    assert 3 <= dev.reads <= 7
    dev2 = Ramps(clock, TRIANGLE, ramp_s=1.0)
    v = measure(dev2, clock=clock)
    assert v is not None and v.polygon is not None and v.aborted is None
    for hidden in TRIANGLE:
        assert min(abs(hidden[0] - h[0]) + abs(hidden[1] - h[1]) for h in v.polygon) < 1e-5
    assert v.model_error is not None and v.model_error < 1e-4
    assert any("ramps on its own" in n for n in v.notes)


def test_a_device_that_never_settles_is_diagnosed() -> None:
    clock = _clock()
    dev = Ramps(clock, TRIANGLE, ramp_s=5.0)  # longer than a sample
    s = take_sample(dev, (0.95, 0.04), clock)
    assert s.xy is None and s.moved and not s.trusted and not s.unapplied
    dev2 = Ramps(clock, TRIANGLE, ramp_s=5.0)
    v = measure(dev2, clock=clock)
    assert v is not None and v.polygon is None and v.aborted is not None
    assert v.aborted.startswith(NEVER_SETTLES) and dev2.commands == DIAGNOSE_AFTER


class KeepsColourWhileOff(FakeDevice):
    """A device that ignores a colour command while it is off (no execute-if-off), the way
    many non-Hue bulbs do; a read then answers the colour it kept."""

    def command(self, xy: XY) -> None:
        before = self.current
        super().command(xy)
        self.applied = self.lit
        if not self.applied:
            self.current = before

    def read(self) -> None:
        self.reads += 1
        kind = "device" if self.applied else "unapplied"
        self.pending.append(
            (self.clock.monotonic() + 0.05, Observation(kind, self.current, trusted=self.applied))
        )


def test_a_device_that_keeps_its_colour_while_off_is_diagnosed_then_measured_lit() -> None:
    clock = _clock()
    dev = KeepsColourWhileOff(clock, TRIANGLE)
    s = take_sample(dev, (0.95, 0.04), clock)
    assert s.xy is None and s.unapplied and s.echo_only and not s.trusted
    # invisible mode: two samples answered with the resting colour, and it stops, saying why
    dev = KeepsColourWhileOff(clock, TRIANGLE)
    v = measure(dev, clock=clock)
    assert v is not None and v.polygon is None and v.aborted is not None
    assert v.aborted.startswith(NOT_APPLIED_WHILE_OFF) and "forced mode" in v.aborted
    assert dev.commands == DIAGNOSE_AFTER and dev.lit_up == 0 and dev.restored
    # forced mode: the same diagnosis switches it on and the measurement starts over, lit
    dev = KeepsColourWhileOff(clock, TRIANGLE)
    v = measure(dev, allow_lit=True, clock=clock)
    assert v is not None and v.polygon is not None and v.aborted is None
    for hidden in TRIANGLE:
        assert min(abs(hidden[0] - h[0]) + abs(hidden[1] - h[1]) for h in v.polygon) < 1e-5
    assert dev.lit_up == 1 and dev.commands == DIAGNOSE_AFTER + 24 and v.unanswered == 0
    assert any("switched on for the measurement" in n for n in v.notes)
    assert dev.restored

    class NeverTakesColour(KeepsColourWhileOff):
        def light_up(self) -> bool:
            self.lit_up += 1
            return True  # on, but colour is still ignored

        def command(self, xy: XY) -> None:
            super().command(xy)
            self.applied = False

    dev2 = NeverTakesColour(clock, TRIANGLE)
    v2 = measure(dev2, allow_lit=True, clock=clock)
    assert v2 is not None and v2.polygon is None and v2.aborted is not None
    assert v2.aborted.startswith(NOT_APPLIED_LIT) and dev2.lit_up == 1
    assert dev2.commands == 2 * DIAGNOSE_AFTER

    class CannotLight(KeepsColourWhileOff):
        def light_up(self) -> bool:
            return False

    dev3 = CannotLight(clock, TRIANGLE)
    v3 = measure(dev3, allow_lit=True, clock=clock)
    assert v3 is not None and v3.aborted is not None
    assert v3.aborted.startswith(NOT_APPLIED_WHILE_OFF) and "cannot switch it on" in v3.aborted


# ---------------------------------------------------------------------------------------------
# The Zigbee2MQTT channel
# ---------------------------------------------------------------------------------------------


class FakeLink:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self.subscribed: list[str] = []
        self.inbox: list[tuple[str, str]] = []

    def publish(self, topic: str, payload: str) -> None:
        self.published.append((topic, payload))

    def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)

    def drain(self, seconds: float) -> Iterable[tuple[str, str]]:
        out, self.inbox = self.inbox, []
        return out

    def deliver(self, topic: str, payload: object) -> None:
        self.inbox.append((topic, json.dumps(payload) if not isinstance(payload, str) else payload))


def test_split_set_topic() -> None:
    assert split_set_topic("z2m-3/dev/set") == ("z2m-3/dev", None)
    assert split_set_topic("z2m-1/dev/bottom/set") == ("z2m-1/dev", "bottom")


def test_single_endpoint_echo_and_answer() -> None:
    link = FakeLink()
    ch = Z2MDeviceChannel(link, "z2m-4/strip/set", identity="0xabc")
    assert link.subscribed == ["z2m-4/strip", "z2m-4/strip/set"]
    ch.command((0.95, 0.04))
    assert link.published[-1] == (
        "z2m-4/strip/set",
        '{"color": {"x": 0.95, "y": 0.04}, "transition": 0}',
    )
    link.deliver("z2m-4/strip/set", '{"color": {"x": 0.95, "y": 0.04}, "transition": 0}')  # our own
    link.deliver("z2m-4/strip", {"color": {"x": 0.95, "y": 0.04}, "state": "OFF"})  # the echo
    link.deliver("z2m-4/strip", {"color": {"x": 0.6914930952925917, "y": 0.3082932784008545}})
    link.deliver("z2m-4/strip/set", '{"brightness": 120}')  # somebody else's command
    obs = ch.observe(0.1)
    assert [o.kind for o in obs] == ["echo", "device", "foreign"]
    assert obs[1].xy == (0.6914930952925917, 0.3082932784008545)
    assert not obs[1].trusted
    ch.read()
    assert link.published[-1] == ("z2m-4/strip/get", '{"color": {"x": "", "y": ""}}')
    # after the read, the state message carries the device's answer: the same value as the
    # command means the device reached it, and it is the device's own, trusted
    link.deliver("z2m-4/strip", {"color": {"x": 0.95, "y": 0.04}, "state": "OFF"})
    obs = ch.observe(0.1)
    assert [(o.kind, o.trusted) for o in obs] == [("device", True)]
    assert obs[0].xy == (0.95, 0.04)


def test_resting_colour_reported_late_is_not_an_answer() -> None:
    """A bulb at rest on a warm white: the snapshot's answer (or any full-state republish)
    carrying that colour during the first sample is not the device's answer to the probe."""
    link = FakeLink()
    ch = Z2MDeviceChannel(link, "z2m-3/bulb/set")
    link.deliver(
        "z2m-3/bulb",
        {"state": "OFF", "color_mode": "color_temp", "color": {"x": 0.4726, "y": 0.413}},
    )
    assert ch.is_lit() is False
    assert link.published[-1] == ("z2m-3/bulb/get", '{"state": "", "color": {"x": "", "y": ""}}')
    ch.command((0.95, 0.04))
    link.deliver("z2m-3/bulb", {"color": {"x": 0.4726, "y": 0.413}, "state": "OFF"})  # late
    link.deliver("z2m-3/bulb", {"color": {"x": 0.95, "y": 0.04}, "state": "OFF"})  # echo
    assert [o.kind for o in ch.observe(0.1)] == ["echo"]
    ch.read()
    link.deliver("z2m-3/bulb", {"color": {"x": 0.6915, "y": 0.3083}, "state": "OFF"})
    obs = ch.observe(0.1)
    assert [(o.kind, o.trusted) for o in obs] == [("device", True)]


def test_a_read_that_lands_before_the_command_applied_is_read_again() -> None:
    """The read-back answers with the colour the device showed before the command: it has
    not applied it yet. The channel passes that on as ``unapplied``, the driver reads again,
    and the second read's answer is the trusted one."""
    link = FakeLink()
    ch = Z2MDeviceChannel(link, "z2m-3/bulb/set")
    link.deliver("z2m-3/bulb", {"state": "OFF", "color": {"x": 0.4726, "y": 0.413}})
    assert ch.is_lit() is False
    ch.command((0.95, 0.04))
    link.deliver("z2m-3/bulb", {"color": {"x": 0.95, "y": 0.04}, "state": "OFF"})  # echo
    assert [o.kind for o in ch.observe(0.1)] == ["echo"]
    ch.read()
    link.deliver("z2m-3/bulb", {"color": {"x": 0.4726, "y": 0.413}, "state": "OFF"})  # too early
    obs = ch.observe(0.1)
    assert [(o.kind, o.xy, o.trusted) for o in obs] == [("unapplied", (0.4726, 0.413), False)]
    ch.read()
    link.deliver("z2m-3/bulb", {"color": {"x": 0.6915, "y": 0.3083}, "state": "OFF"})
    assert [(o.kind, o.trusted) for o in ch.observe(0.1)] == [("device", True)]
    # the driver, end to end: the first read is too early, the second answers
    clock = _clock()
    early = [0]

    class SlowToApply(FakeDevice):
        def read(self) -> None:
            self.reads += 1
            if self.reads == 1:
                early[0] += 1
                return  # nothing comes back the first time (the channel dropped it)
            self.pending.append(
                (self.clock.monotonic() + 0.05, Observation("device", self.current, trusted=True))
            )

    dev = SlowToApply(clock, TRIANGLE)
    s = take_sample(dev, (0.95, 0.04), clock)
    # the dropped read, the answer, the confirming read: one retry, then confirmed
    assert s.trusted and s.xy is not None and s.confirmed and dev.reads == 3
    assert s.retries == 1 and not s.moved and not s.unapplied


def test_state_on_is_reported_as_lit() -> None:
    link = FakeLink()
    ch = Z2MDeviceChannel(link, "z2m-4/strip/set")
    ch.command((0.95, 0.04))
    link.deliver("z2m-4/strip", {"state": "ON", "brightness": 120})  # a switch, no colour
    assert [o.kind for o in ch.observe(0.1)] == ["lit"]
    link.deliver("z2m-4/strip", {"state": "ON", "color": {"x": 0.6915, "y": 0.3083}})
    assert [o.kind for o in ch.observe(0.1)] == ["lit", "device"]
    # on a two-endpoint device only this endpoint's state counts
    ch2 = Z2MDeviceChannel(link, "z2m-1/sconce/top/set")
    ch2.command((0.95, 0.04))
    link.deliver("z2m-1/sconce", {"state_bottom": "ON", "state_top": "OFF"})
    assert ch2.observe(0.1) == []
    link.deliver("z2m-1/sconce", {"state_bottom": "OFF", "state_top": "ON"})
    assert [o.kind for o in ch2.observe(0.1)] == ["lit"]


BRIDGE_DEVICES = [
    {"friendly_name": "Coordinator", "ieee_address": "0x00124b0029e5a1c2", "type": "Coordinator"},
    {
        "friendly_name": "strip",
        "ieee_address": "0x0017880100aa0001",
        "model_id": "LST002",
        "manufacturer": "Signify Netherlands B.V.",
        "software_build_id": "1.116.3",
        "definition": {"model": "7190130", "vendor": "Philips", "description": "Hue LightStrip"},
    },
    {
        "friendly_name": "sconce",
        "ieee_address": "0x0017880100aa0002",
        "model_id": "046677585235",
        "software_build_id": "1.122.2",
        "definition": None,
    },
    "not a device",
]


def test_bridge_devices_discovery_binds_the_ieee_address() -> None:
    infos = parse_bridge_devices(json.dumps(BRIDGE_DEVICES))
    assert set(infos) == {"Coordinator", "strip", "sconce"}
    assert infos["strip"].model_id == "LST002" and infos["strip"].firmware == "1.116.3"
    assert infos["strip"].description == "Hue LightStrip"
    assert infos["sconce"].manufacturer is None and infos["sconce"].description is None
    assert parse_bridge_devices("not json") == {}
    link = FakeLink()
    link.deliver("z2m-4/bridge/devices", BRIDGE_DEVICES)
    assert set(bridge_devices(link, "z2m-4")) == {"Coordinator", "strip", "sconce"}
    assert link.subscribed[-1] == "z2m-4/bridge/devices"
    # the channel: identity from the address, endpoint-suffixed on a multi-endpoint device
    link.deliver("z2m-4/bridge/devices", BRIDGE_DEVICES)
    ch = Z2MDeviceChannel(link, "z2m-4/strip/set")
    assert ch.identity is None and ch.firmware is None
    info = ch.discover("z2m-4")
    assert info is not None and ch.identity == "0x0017880100aa0001" and ch.firmware == "1.116.3"
    link.deliver("z2m-1/bridge/devices", BRIDGE_DEVICES)
    ch2 = Z2MDeviceChannel(link, "z2m-1/sconce/top/set")
    assert ch2.discover("z2m-1") is not None
    assert ch2.identity == "0x0017880100aa0002/top" and ch2.firmware == "1.122.2"
    # an explicit identity is kept; an unlisted device leaves it as it was
    link.deliver("z2m-1/bridge/devices", BRIDGE_DEVICES)
    ch3 = Z2MDeviceChannel(link, "z2m-1/sconce/set", identity="given")
    assert ch3.discover("z2m-1") is not None and ch3.identity == "given"
    link.deliver("z2m-1/bridge/devices", BRIDGE_DEVICES)
    ch4 = Z2MDeviceChannel(link, "z2m-1/unknown/set")
    assert ch4.discover("z2m-1") is None and ch4.identity is None
    assert ch4.discover("other-base") is None


BRIDGE_GROUPS = [
    {
        "friendly_name": "office",
        "id": 1,
        "members": [
            {"ieee_address": "0x0017880100aa0001", "endpoint": 11},
            {"ieee_address": "0x0017880100aa0002", "endpoint": 11},
            {"ieee_address": "0x0017880100aa0002", "endpoint": 12},
        ],
    },
    {
        "friendly_name": "office_bulbs",
        "id": 2,
        "members": [{"ieee_address": "0x0017880100aa0002", "endpoint": 11}],
    },
    {"friendly_name": "empty", "id": 3, "members": []},
    "not a group",
]


def test_group_topics_from_the_coordinators_group_list() -> None:
    groups = parse_bridge_groups(json.dumps(BRIDGE_GROUPS))
    assert set(groups) == {"office", "office_bulbs", "empty"}
    assert groups["office"][0] == ("0x0017880100aa0001", 11)
    link = FakeLink()
    link.deliver("z2m-1/bridge/groups", BRIDGE_GROUPS)
    assert group_set_topics(link, "z2m-1", "0x0017880100aa0002") == [
        "z2m-1/office/set",
        "z2m-1/office_bulbs/set",
    ]
    link.deliver("z2m-1/bridge/groups", BRIDGE_GROUPS)
    assert group_set_topics(link, "z2m-1", "0x0017880100aa0001") == ["z2m-1/office/set"]
    link.deliver("z2m-1/bridge/groups", BRIDGE_GROUPS)
    assert group_set_topics(link, "z2m-1", "0x0000000000000000") == []
    # a channel watching those topics reports a command there as foreign
    link.deliver("z2m-1/bridge/groups", BRIDGE_GROUPS)
    watch = group_set_topics(link, "z2m-1", "0x0017880100aa0001")
    ch = Z2MDeviceChannel(link, "z2m-1/strip/set", watch=watch)
    assert "z2m-1/office/set" in link.subscribed
    ch.command((0.95, 0.04))
    link.deliver("z2m-1/office/set", '{"state": "ON", "brightness": 200}')
    assert [o.kind for o in ch.observe(0.1)] == [
        "foreign",
        "lit",
    ]  # a group ON: foreign, and the fixture is being lit


def test_multi_endpoint_read_lands_under_the_unsuffixed_key() -> None:
    """The message sequence a two-endpoint wall light produced: the endpoint key keeps the
    echo, the read answer arrives unsuffixed, and a stale unsuffixed value is republished with
    the next command's echo."""
    link = FakeLink()
    ch = Z2MDeviceChannel(link, "z2m-1/sconce/top/set")
    ch.command((0.02, 0.7))
    # the echo message carries the STALE unsuffixed value from an earlier read
    link.deliver(
        "z2m-1/sconce",
        {
            "color": {"x": 0.6662546730754558, "y": 0.2960708018616007},
            "color_top": {"x": 0.02, "y": 0.7},
        },
    )
    assert [o.kind for o in ch.observe(0.1)] == ["echo"]
    ch.read()
    link.deliver(
        "z2m-1/sconce",
        {
            "color": {"x": 0.16987869077592127, "y": 0.6961318379491874},
            "color_top": {"x": 0.02, "y": 0.7},
        },
    )
    obs = ch.observe(0.1)
    assert [o.kind for o in obs] == ["device"]
    assert obs[0].xy == (0.16987869077592127, 0.6961318379491874)
    # the other endpoint's constant colour never moves, so it is never taken as this one's:
    # seen once before the read (the echo message), the same value after it is not an answer
    ch.command((0.95, 0.04))
    link.deliver(
        "z2m-1/sconce", {"color": {"x": 0.445, "y": 0.4067}, "color_top": {"x": 0.95, "y": 0.04}}
    )
    assert [o.kind for o in ch.observe(0.1)] == ["echo"]
    ch.read()
    link.deliver(
        "z2m-1/sconce", {"color": {"x": 0.445, "y": 0.4067}, "color_top": {"x": 0.95, "y": 0.04}}
    )
    assert [o.kind for o in ch.observe(0.1)] == ["echo"]


def test_snapshot_is_lit_and_restore() -> None:
    link = FakeLink()
    ch = Z2MDeviceChannel(link, "z2m-1/sconce/bottom/set")
    link.deliver(
        "z2m-1/sconce",
        {
            "state_bottom": "ON",
            "color_mode_bottom": "color_temp",
            "color_temp_bottom": 346,
            "color_bottom": {"x": 0.445, "y": 0.4067},
            "state_top": "OFF",
        },
    )
    assert ch.is_lit() is True
    ch.restore()
    assert link.published[-1] == (
        "z2m-1/sconce/bottom/set",
        '{"color_temp": 346, "transition": 0.2}',
    )
    link2 = FakeLink()
    ch2 = Z2MDeviceChannel(link2, "z2m-4/strip/set")
    link2.deliver(
        "z2m-4/strip", {"state": "OFF", "color_mode": "xy", "color": {"x": 0.434, "y": 0.383}}
    )
    assert ch2.is_lit() is False
    ch2.restore()
    assert link2.published[-1] == (
        "z2m-4/strip/set",
        '{"color": {"x": 0.434, "y": 0.383}, "transition": 0.2}',
    )


def test_light_up_switches_on_and_restore_puts_the_colour_back_before_switching_off() -> None:
    """A device measured lit is one that keeps its colour while off: the colour goes back
    first, while it is on, or it would come back on later showing the last probe."""
    link = FakeLink()
    ch = Z2MDeviceChannel(link, "z2m-4/strip/set")
    link.deliver(
        "z2m-4/strip", {"state": "OFF", "color_mode": "xy", "color": {"x": 0.434, "y": 0.383}}
    )
    assert ch.is_lit() is False
    assert ch.light_up() is True
    assert link.published[-1] == ("z2m-4/strip/set", '{"state": "ON", "transition": 0}')
    ch.command((0.95, 0.04))
    link.deliver("z2m-4/strip", {"state": "ON", "color": {"x": 0.95, "y": 0.04}})  # the echo, lit
    assert [o.kind for o in ch.observe(0.1)] == ["lit", "echo"]
    ch.restore(0.0)
    assert link.published[-2:] == [
        ("z2m-4/strip/set", '{"color": {"x": 0.434, "y": 0.383}, "transition": 0.0}'),
        ("z2m-4/strip/set", '{"state": "OFF", "transition": 0.0}'),
    ]
    ch.restore()  # switched off once; a later restore is the colour alone
    assert link.published[-1] == (
        "z2m-4/strip/set",
        '{"color": {"x": 0.434, "y": 0.383}, "transition": 0.2}',
    )


def test_colour_temperature_is_commanded_read_and_classified_like_colour() -> None:
    """A Hue bulb sent 50 mired clips to 153 and answers it on the read; the echo repeats the
    command; a read landing before the device applied the command shows the value from
    before it and is unapplied. The read payload is the same one colour uses."""
    link = FakeLink()
    ch = Z2MDeviceChannel(link, "z2m-3/bulb/set")
    link.deliver("z2m-3/bulb", {"state": "OFF", "color_mode": "color_temp", "color_temp": 370})
    assert ch.is_lit() is False
    ch.command_ct(50)
    assert link.published[-1] == ("z2m-3/bulb/set", '{"color_temp": 50, "transition": 0}')
    link.deliver("z2m-3/bulb/set", '{"color_temp": 50, "transition": 0}')  # our own
    link.deliver("z2m-3/bulb", {"color_temp": 50, "color_mode": "color_temp", "state": "OFF"})
    link.deliver("z2m-3/bulb", {"color_temp": 370, "color_mode": "color_temp", "state": "OFF"})
    assert [o.kind for o in ch.observe(0.1)] == ["echo"]  # the resting value repeated: not new
    ch.read()
    assert link.published[-1] == ("z2m-3/bulb/get", '{"color": {"x": "", "y": ""}}')
    link.deliver("z2m-3/bulb", {"color_temp": 370, "color_mode": "color_temp", "state": "OFF"})
    obs = ch.observe(0.1)
    assert [(o.kind, o.mired, o.xy) for o in obs] == [("unapplied", 370, None)]
    link.deliver("z2m-3/bulb", {"color_temp": 153, "color_mode": "color_temp", "state": "OFF"})
    obs = ch.observe(0.1)
    assert [(o.kind, o.mired, o.trusted) for o in obs] == [("device", 153, True)]
    # a colour in the same message is not this sample's evidence, and a colour command
    # afterwards is classified as colour again
    ch.command((0.95, 0.04))
    ch.read()
    link.deliver("z2m-3/bulb", {"color": {"x": 0.6915, "y": 0.3083}, "color_temp": 153})
    obs = ch.observe(0.1)
    assert [(o.kind, o.xy, o.mired) for o in obs] == [("device", (0.6915, 0.3083), None)]


def test_multi_endpoint_colour_temperature_answer_lands_unsuffixed_too() -> None:
    link = FakeLink()
    ch = Z2MDeviceChannel(link, "z2m-1/sconce/top/set")
    ch.command_ct(1000)
    link.deliver("z2m-1/sconce", {"color_temp": 250, "color_temp_top": 1000})  # echo + stale
    assert [o.kind for o in ch.observe(0.1)] == ["echo"]
    ch.read()
    link.deliver("z2m-1/sconce", {"color_temp": 500, "color_temp_top": 1000})
    obs = ch.observe(0.1)
    assert [(o.kind, o.mired, o.trusted) for o in obs] == [("device", 500, True)]
    # the other endpoint's value, republished unsuffixed before the read, is never taken as
    # this one's answer when it comes again after it
    ch.command_ct(50)
    link.deliver("z2m-1/sconce", {"color_temp": 366, "color_temp_top": 50})
    assert [o.kind for o in ch.observe(0.1)] == ["echo"]
    ch.read()
    link.deliver("z2m-1/sconce", {"color_temp": 366, "color_temp_top": 50})
    assert [o.kind for o in ch.observe(0.1)] == ["echo"]
    link.deliver("z2m-1/sconce", {"color_temp": 153, "color_temp_top": 50})
    assert [(o.kind, o.mired) for o in ch.observe(0.1)] == [("device", 153)]


def test_the_range_is_measured_over_the_channel_with_the_real_message_shapes() -> None:
    """End to end over the Zigbee2MQTT channel: a simulated Hue bulb clamps to 153..500."""
    from pascl.shell.gamut_measure import take_ct_sample

    class Bulb(FakeLink):
        def __init__(self) -> None:
            super().__init__()
            self.ct = 370

        def publish(self, topic: str, payload: str) -> None:
            super().publish(topic, payload)
            body = json.loads(payload)
            if topic.endswith("/set") and "color_temp" in body:
                self.deliver("z2m-3/bulb", {"color_temp": body["color_temp"], "state": "OFF"})
                self.ct = min(max(body["color_temp"], 153), 500)
            elif topic.endswith("/get"):
                self.deliver("z2m-3/bulb", {"color_temp": self.ct, "state": "OFF"})

    clock = _clock()
    link = Bulb()
    ch = Z2MDeviceChannel(link, "z2m-3/bulb/set")

    class Ticking:
        def observe(self, seconds: float) -> list[Observation]:
            clock.advance(seconds)
            return ch.observe(seconds)

        def __getattr__(self, name: str) -> object:
            return getattr(ch, name)

    dev = Ticking()
    link.deliver("z2m-3/bulb", {"state": "OFF", "color_temp": 370})
    assert ch.snapshot() is not None
    s1 = take_ct_sample(dev, 50, clock)  # type: ignore[arg-type]
    assert s1.mired == 153 and s1.trusted and s1.confirmed
    s2 = take_ct_sample(dev, 1000, clock)  # type: ignore[arg-type]
    assert s2.mired == 500 and s2.trusted and s2.confirmed
