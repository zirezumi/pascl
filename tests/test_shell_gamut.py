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

    def __init__(
        self,
        clock: ManualClock,
        poly: Polygon,
        *,
        reports: bool = False,
        lit: bool = False,
        foreign_at: int | None = None,
        lit_at: int | None = None,
    ) -> None:
        self.clock = clock
        self.poly = poly
        self.reports = reports
        self.lit = lit
        self.foreign_at = foreign_at
        self.lit_at = lit_at
        self.pending: list[tuple[float, Observation]] = []
        self.commands = 0
        self.reads = 0
        self.restored = False

    def is_lit(self) -> bool | None:
        return self.lit

    def command(self, xy: XY) -> None:
        self.commands += 1
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
        # a read-back is the device's own value, even when it equals the command
        self.pending.append(
            (self.clock.monotonic() + 0.05, Observation("device", self.current, trusted=True))
        )

    def observe(self, seconds: float) -> list[Observation]:
        self.clock.advance(seconds)
        now = self.clock.monotonic()
        due = [ob for t, ob in self.pending if t <= now]
        self.pending = [(t, ob) for t, ob in self.pending if t > now]
        return due

    def restore(self) -> None:
        self.restored = True


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
    assert dev.reads == dev.commands  # never answered before the read, so every sample read
    assert dev.restored
    g = gamut_from(v, dev, clock)
    assert g is not None and g.bound_to == "fake-device-1" and g.measured is not None
    assert g.vertices == v.polygon
    assert g.clip_rule == "closest" and g.firmware == "1.116.3" and g.inherited_from is None
    assert g.model_error == v.model_error


def test_measure_uses_a_spontaneous_report_without_reading() -> None:
    clock = _clock()
    dev = FakeDevice(clock, TRIANGLE, reports=True)
    v = measure(dev, clock=clock)
    assert v is not None and v.polygon is not None and len(v.polygon) == 3
    assert dev.reads == 0


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
    assert dev.commands == 5 and dev.restored
    assert gamut_from(v, dev, clock) is None
    # a fixture measured lit on purpose is not aborted by its own ON state
    dev2 = FakeDevice(clock, TRIANGLE, lit=True, lit_at=5)
    v2 = measure(dev2, allow_lit=True, clock=clock)
    assert v2 is not None and v2.polygon is not None and v2.aborted is None


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
    assert s.xy is None and s.echo_only and not s.foreign
    assert dev.reads == 1
    assert READ_AFTER <= clock.monotonic() - t0 <= ANSWER_TIMEOUT + 0.3


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
    assert [o.kind for o in ch.observe(0.1)] == ["foreign"]


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
