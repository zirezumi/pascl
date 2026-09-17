"""The unattended calibration loop against a simulated host.

The simulation is the host as the runtime sees it: entity states over the REST shape, a
retained ``bridge/devices`` per coordinator, and per device the Zigbee2MQTT message rules
(an optimistic echo on ``/set``, the clipped colour on ``/get``), driven through the real
:class:`Z2MDeviceChannel`. What the loop must get right: pick only a dark fixture in an empty
room, bind the polygon to the IEEE address, record the firmware, refuse a record the model
would not validate with, seed same-model siblings, abort when the room fills, and stop when
nothing is left.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import yaml

from pascl.clock import ManualClock
from pascl.core.gamut import XY, Polygon, project
from pascl.harness.binding import expand, load_binding
from pascl.model import HomeModel, load
from pascl.shell.gamut_measure import NOT_APPLIED_WHILE_OFF, measure
from pascl.shell.gamut_runtime import (
    Tick,
    command_topics,
    device_ids,
    device_infos,
    group_topics,
    host_view,
    presence_entities,
    run,
    tick,
)
from pascl.shell.z2m import Z2MDeviceChannel

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo_home.yaml"
BINDING = ROOT / "examples" / "demo_binding.yaml"
TRIANGLE: Polygon = ((0.153185, 0.047547), (0.691493, 0.308293), (0.169986, 0.699992))
FAR: Polygon = ((0.153185, 0.047547), (0.691493, 0.308293), (0.219986, 0.699992))


class SimulatedHome:
    """An MQTT link plus REST states, with Zigbee2MQTT devices that clip to hidden polygons."""

    def __init__(
        self,
        polygons: dict[str, Polygon],
        states: dict[str, str],
        *,
        keeps_colour_off: set[str] | None = None,
    ) -> None:
        self.polygons = polygons
        self.states = dict(states)
        self.inbox: list[tuple[str, str]] = []
        self.published: list[tuple[str, str]] = []
        self.subscribed: list[str] = []
        self.current: dict[str, XY] = {}
        #: devices that ignore a colour command while off (no execute-if-off)
        self.keeps_colour_off = keeps_colour_off or set()
        self.lit: set[str] = {
            e.removeprefix("light.")
            for e, s in states.items()
            if s == "on" and e.startswith("light.")
        }
        self.devices = [
            {
                "friendly_name": name,
                "ieee_address": f"0x00{i:014x}",
                "model_id": "LCA001",
                "software_build_id": "1.116.3",
            }
            for i, name in enumerate(polygons, 1)
        ]
        self.occupy_after: int | None = None
        self.state_reads = 0

    # -- MqttLink ------------------------------------------------------------------------
    def publish(self, topic: str, payload: str) -> None:
        self.published.append((topic, payload))
        parts = topic.split("/")
        base, name, verb = "/".join(parts[:2]), parts[1], parts[-1]
        body = json.loads(payload)
        if name not in self.polygons:
            return
        state = "ON" if name in self.lit else "OFF"
        if verb == "set" and "state" in body and "color" not in body:
            if body["state"] == "ON":
                self.lit.add(name)
            else:
                self.lit.discard(name)
            state = "ON" if name in self.lit else "OFF"
            x, y = self.current.get(name, (0.4, 0.4))
            self.inbox.append((base, json.dumps({"state": state, "color": {"x": x, "y": y}})))
        elif verb == "set" and "color" in body:
            xy = (float(body["color"]["x"]), float(body["color"]["y"]))
            if name in self.lit or name not in self.keeps_colour_off:
                self.current[name] = project(xy, self.polygons[name])
            self.inbox.append((base, json.dumps({"color": body["color"], "state": state})))
        elif verb == "get" and "color" in body:
            x, y = self.current.get(name, (0.4, 0.4))
            self.inbox.append((base, json.dumps({"color": {"x": x, "y": y}, "state": state})))
        elif verb == "get" and "state" in body:
            x, y = self.current.get(name, (0.4, 0.4))
            self.inbox.append(
                (base, json.dumps({"state": state, "color_mode": "xy", "color": {"x": x, "y": y}}))
            )

    def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)
        if topic.endswith("/bridge/devices"):
            self.inbox.append((topic, json.dumps(self.devices)))

    def drain(self, seconds: float) -> Iterator[tuple[str, str]]:
        out, self.inbox = self.inbox, []
        yield from out

    # -- REST and entity watching ----------------------------------------------------------
    def get_states(self) -> dict[str, str]:
        self.state_reads += 1
        if self.occupy_after is not None and self.state_reads > self.occupy_after:
            return {**self.states, "binary_sensor.den_presence": "on"}
        return dict(self.states)

    def watch_entity(self, entity_id: str) -> str:
        topic = f"ha/state/{entity_id}"
        self.subscribed.append(topic)
        return topic

    def presence(self, entity_id: str, state: str) -> None:
        """A watched presence entity changes state (relayed like the real link does)."""
        self.states[entity_id] = state
        self.inbox.append((f"ha/state/{entity_id}", state))

    def close(self) -> None:
        pass


def _model() -> HomeModel:
    return load(DEMO.read_text(encoding="utf-8"))


def _index(model: HomeModel) -> object:
    return expand(load_binding(BINDING.read_text(encoding="utf-8")), model)


def _clock() -> ManualClock:
    from datetime import UTC, datetime

    return ManualClock(datetime(2026, 9, 17, 14, 0, tzinfo=UTC))


class TimedChannel(Z2MDeviceChannel):
    """The real channel, with the injected clock advanced by every observe as a blocking
    drain would advance the wall clock."""

    clock: ManualClock

    def observe(self, seconds: float) -> list[object]:  # type: ignore[override]
        self.clock.advance(seconds)
        return super().observe(seconds)


def _factory(clock: ManualClock) -> object:
    def make(link: object, topic: str, watch: list[str], occupancy: list[str]) -> TimedChannel:
        ch = TimedChannel(link, topic, watch=watch, occupancy=occupancy)  # type: ignore[arg-type]
        ch.clock = clock
        return ch

    return make


DARK_EMPTY = {
    "light.den_pendant_1": "off",
    "light.den_pendant_2": "off",
    "light.den_strip": "off",
    "binary_sensor.den_presence": "off",
    "binary_sensor.house_presence": "off",
    "binary_sensor.pantry_presence": "off",
}


def test_host_view_and_device_ids() -> None:
    model = _model()
    index = _index(model)
    view = host_view(model, index, {**DARK_EMPTY, "light.den_strip": "on"})  # type: ignore[arg-type]
    assert view.lit == {"den_strip"} and view.occupied == set()
    # a space's presence occupies every room in it
    view = host_view(model, index, {**DARK_EMPTY, "binary_sensor.house_presence": "on"})  # type: ignore[arg-type]
    assert "den" in view.occupied and "pantry" not in view.occupied
    home = SimulatedHome({"den_pendant_1": TRIANGLE, "den_strip": TRIANGLE}, DARK_EMPTY)
    infos = device_infos(model, home)
    assert set(infos) == {"den_pendant_1", "den_strip"}
    assert device_ids(model, infos) == {
        "den_pendant_1": "0x0000000000000001",
        "den_strip": "0x0000000000000002",
    }
    assert home.subscribed.count("z2m-1/bridge/devices") == 1  # one read per coordinator
    topics = command_topics(model, index)  # type: ignore[arg-type]
    assert topics["den_strip"] == "z2m-1/den_strip/set"
    # the groups a fixture belongs to are watched: a command there reaches it unseen
    assert group_topics(model, index, "den_pendant_1") == ["z2m-1/den/set", "z2m-1/den_bulbs/set"]  # type: ignore[arg-type]
    assert group_topics(model, index, "den_strip") == ["z2m-1/den/set"]  # type: ignore[arg-type]


def test_group_command_during_a_sample_is_foreign() -> None:
    model = _model()
    index = _index(model)
    clock = _clock()
    home = SimulatedHome({"den_pendant_1": TRIANGLE}, DARK_EMPTY)
    make = _factory(clock)
    channel = make(home, "z2m-1/den_pendant_1/set", group_topics(model, index, "den_pendant_1"), [])  # type: ignore[operator]
    assert "z2m-1/den/set" in home.subscribed
    channel.command((0.95, 0.04))
    home.inbox.append(("z2m-1/den/set", '{"brightness": 120}'))
    kinds = [o.kind for o in channel.observe(0.1)]
    assert kinds == ["echo", "foreign"]
    # a group command that turns the light on is the fixture being lit, before any report
    channel.command((0.04, 0.94))
    home.inbox.append(("z2m-1/den/set", '{"state": "ON", "brightness": 120, "transition": 2}'))
    kinds = [o.kind for o in channel.observe(0.1)]
    assert kinds == ["echo", "foreign", "lit"]


def test_presence_return_aborts_ahead_of_the_render() -> None:
    """The room's presence entity flips on: the measurement aborts on that relayed event and
    restores with no transition, before the render that follows presence reaches the wire."""
    model = _model()
    index = _index(model)
    clock = _clock()
    home = SimulatedHome({"den_pendant_1": TRIANGLE}, DARK_EMPTY)
    assert presence_entities(model, index, "den") == [  # type: ignore[arg-type]
        "binary_sensor.den_presence",
        "binary_sensor.den_switch_presence",
        "binary_sensor.house_presence",
    ]
    make = _factory(clock)
    occupancy = [home.watch_entity(e) for e in presence_entities(model, index, "den")]  # type: ignore[arg-type]
    channel = make(home, "z2m-1/den_pendant_1/set", [], occupancy)  # type: ignore[operator]
    channel.snapshot()
    commands_before = len(home.published)
    home.presence("binary_sensor.den_presence", "on")
    v = measure(channel, clock=clock)
    assert v is not None and v.aborted is not None and "occupied" in v.aborted
    # one probe went out, then the restore, with no transition
    sets = [p for t, p in home.published[commands_before:] if t == "z2m-1/den_pendant_1/set"]
    assert len(sets) == 2 and '"transition": 0.0' in sets[-1] and '"color"' in sets[-1]
    # in the forced mode the same event is ignored
    home2 = SimulatedHome({"den_pendant_1": TRIANGLE}, DARK_EMPTY)
    occupancy = [home2.watch_entity(e) for e in presence_entities(model, index, "den")]  # type: ignore[arg-type]
    channel2 = make(home2, "z2m-1/den_pendant_1/set", [], occupancy)  # type: ignore[operator]
    channel2.snapshot()
    home2.presence("binary_sensor.den_presence", "on")
    v2 = measure(channel2, allow_lit=True, clock=clock)
    assert v2 is not None and v2.polygon is not None and v2.aborted is None


def test_tick_measures_records_binds_and_seeds(tmp_path: Path) -> None:
    model = _model()
    index = _index(model)
    clock = _clock()
    home = SimulatedHome(
        {"den_pendant_1": TRIANGLE, "den_pendant_2": TRIANGLE, "den_strip": TRIANGLE}, DARK_EMPTY
    )
    out = tmp_path / "home.yaml"
    m2, t = tick(model, index, home, write=out, clock=clock, channel_factory=_factory(clock))  # type: ignore[arg-type]
    assert t.picked is not None and t.picked.fixture == "den_pendant_1"
    assert t.verdict is not None and t.verdict.polygon is not None and t.written
    g = m2.rooms["den"].fixtures["den_pendant_1"].gamut
    assert g is not None
    assert g.bound_to == "0x0000000000000001" and g.firmware == "1.116.3"
    assert g.clip_rule == "closest" and g.inherited_from is None
    for hidden in TRIANGLE:
        assert min(abs(hidden[0] - h[0]) + abs(hidden[1] - h[1]) for h in g.vertices) < 1e-5
    # the measurement travelled to the other pendant (same label), not to the strip
    assert [(s.fixture, s.source) for s in t.seeds] == [("den_pendant_2", "den_pendant_1")]
    g2 = m2.rooms["den"].fixtures["den_pendant_2"].gamut
    assert g2 is not None and g2.inherited_from == "den_pendant_1"
    assert g2.bound_to == "0x0000000000000002"
    assert t.remaining == 2  # the strip unmeasured, the pendant a seed to confirm
    assert load(out.read_text(encoding="utf-8")) == m2
    # the fixture was put back as the snapshot found it
    assert home.published[-1][0] == "z2m-1/den_pendant_1/set"
    assert "transition" in home.published[-1][1]


def test_tick_skips_lit_and_occupied_and_aborts_when_the_room_fills() -> None:
    model = _model()
    index = _index(model)
    clock = _clock()
    lit = {**DARK_EMPTY, "light.den_pendant_1": "on"}
    home = SimulatedHome({"den_pendant_1": TRIANGLE, "den_strip": TRIANGLE}, lit)
    _, t = tick(model, index, home, write=None, clock=clock, channel_factory=_factory(clock))  # type: ignore[arg-type]
    assert t.picked is not None and t.picked.fixture == "den_pendant_2"  # dark, unmeasured
    occupied = {**DARK_EMPTY, "binary_sensor.den_presence": "on"}
    home = SimulatedHome({"den_pendant_1": TRIANGLE}, occupied)
    _, t = tick(model, index, home, write=None, clock=clock, channel_factory=_factory(clock))  # type: ignore[arg-type]
    assert t.picked is None and t.remaining == 3
    # the room fills part-way: the measurement aborts and nothing is written
    home = SimulatedHome({"den_pendant_1": TRIANGLE}, DARK_EMPTY)
    home.occupy_after = 4
    m2, t = tick(model, index, home, write=None, clock=clock, channel_factory=_factory(clock))  # type: ignore[arg-type]
    assert (
        t.verdict is not None and t.verdict.aborted is not None and "occupied" in t.verdict.aborted
    )
    assert m2 == model and not t.written


def test_tick_sets_aside_a_device_that_keeps_its_colour_while_off_and_force_lights_it() -> None:
    model = _model()
    index = _index(model)
    clock = _clock()
    # the pendants are lit, so the strip is the pick; it ignores colour while off
    states = {**DARK_EMPTY, "light.den_pendant_1": "on", "light.den_pendant_2": "on"}
    home = SimulatedHome({"den_strip": TRIANGLE}, states, keeps_colour_off={"den_strip"})
    m2, t = tick(model, index, home, write=None, clock=clock, channel_factory=_factory(clock))  # type: ignore[arg-type]
    assert t.picked is not None and t.picked.fixture == "den_strip" and t.set_aside
    assert t.verdict is not None and t.verdict.aborted is not None
    assert t.verdict.aborted.startswith(NOT_APPLIED_WHILE_OFF) and m2 == model
    assert "den_strip" not in home.lit  # never switched on outside the forced mode
    probes = [p for p in home.published if p[0] == "z2m-1/den_strip/set" and "color" in p[1]]
    assert len(probes) == 2 + 1  # two commands answered with the resting colour, the restore
    # the loop does not pick it again: with the pendants lit the second tick finds nothing
    # (and would keep resting, so the report stops the loop there)
    ticks: list[Tick] = []
    from pascl.shell import gamut_runtime

    original = gamut_runtime.tick

    def timed_tick(*args: object, **kw: object) -> object:
        kw.setdefault("clock", clock)
        kw.setdefault("channel_factory", _factory(clock))
        return original(*args, **kw)  # type: ignore[arg-type]

    class EnoughError(Exception):
        pass

    def report(t: Tick) -> None:
        ticks.append(t)
        if len(ticks) == 2:
            raise EnoughError

    gamut_runtime.tick = timed_tick  # type: ignore[assignment]
    try:
        run(model, index, home, write=None, interval_s=60, report=report, sleep=lambda _s: None)  # type: ignore[arg-type]
    except EnoughError:
        pass
    finally:
        gamut_runtime.tick = original
    assert ticks[0].set_aside and ticks[1].picked is None
    # forced: switched on, measured from the start, colour put back, then off again (the lit
    # pendants are eligible in the forced mode, so they are set aside by hand here)
    home = SimulatedHome({"den_strip": TRIANGLE}, states, keeps_colour_off={"den_strip"})
    pendants = {"den_pendant_1", "den_pendant_2"}
    m3, t = tick(
        model,
        index,  # type: ignore[arg-type]
        home,
        write=None,
        clock=clock,
        channel_factory=_factory(clock),
        force=True,
        exclude=pendants,
    )
    assert t.picked is not None and t.picked.fixture == "den_strip" and not t.set_aside
    assert t.verdict is not None and t.verdict.polygon is not None
    g = m3.rooms["den"].fixtures["den_strip"].gamut
    assert g is not None
    for hidden in TRIANGLE:
        assert min(abs(hidden[0] - h[0]) + abs(hidden[1] - h[1]) for h in g.vertices) < 1e-5
    assert any("switched on" in n for n in t.notes)
    assert "den_strip" not in home.lit
    tail = [p[1] for p in home.published if p[0] == "z2m-1/den_strip/set"][-2:]
    assert "color" in tail[0] and json.loads(tail[1]) == {"state": "OFF", "transition": 0.2}


def test_tick_refuses_a_record_the_model_would_not_validate_with() -> None:
    model = _model()
    index = _index(model)
    clock = _clock()
    # pendant 1 measured on a unit whose polygon is 0.05 off its label-mate's; the strip is
    # lit so the second tick reaches the seeded pendant
    states = {**DARK_EMPTY, "light.den_strip": "on"}
    home = SimulatedHome({"den_pendant_1": TRIANGLE, "den_pendant_2": FAR}, states)
    m2, t1 = tick(model, index, home, write=None, clock=clock, channel_factory=_factory(clock))  # type: ignore[arg-type]
    assert t1.picked is not None and t1.picked.fixture == "den_pendant_1" and t1.verdict is not None
    # the seed for pendant 2 is now the triangle; its own measurement contradicts it and the
    # model's same-label limit, so the record is refused and the seed kept
    m3, t2 = tick(m2, index, home, write=None, clock=clock, channel_factory=_factory(clock))  # type: ignore[arg-type]
    assert t2.picked is not None and t2.picked.fixture == "den_pendant_2"
    assert t2.seed_gap is not None and t2.seed_gap > 0.04
    assert any("CONTRADICTED" in n for n in t2.notes) and any("refused" in n for n in t2.notes)
    assert m3 == m2


def test_run_stops_when_everything_is_measured(tmp_path: Path) -> None:
    model = _model()
    index = _index(model)
    clock = _clock()
    home = SimulatedHome(
        {"den_pendant_1": TRIANGLE, "den_pendant_2": TRIANGLE, "den_strip": TRIANGLE}, DARK_EMPTY
    )
    ticks: list[object] = []
    sleeps: list[float] = []
    from pascl.shell import gamut_runtime

    original = gamut_runtime.tick

    def timed_tick(*args: object, **kw: object) -> object:
        kw.setdefault("clock", clock)
        kw.setdefault("channel_factory", _factory(clock))
        return original(*args, **kw)  # type: ignore[arg-type]

    gamut_runtime.tick = timed_tick  # type: ignore[assignment]
    try:
        final = run(
            model,
            index,  # type: ignore[arg-type]
            home,
            write=tmp_path / "home.yaml",
            interval_s=60,
            report=ticks.append,
            sleep=sleeps.append,
        )
    finally:
        gamut_runtime.tick = original
    # pendant 1 measured (seeding pendant 2), the strip measured, then the seed confirmed,
    # then one idle tick that finds nothing left
    assert len(ticks) == 4 and sleeps == [60, 60, 60]
    for fid in ("den_pendant_1", "den_pendant_2", "den_strip"):
        g = final.rooms["den"].fixtures[fid].gamut
        assert g is not None and g.inherited_from is None
    written = yaml.safe_load((tmp_path / "home.yaml").read_text(encoding="utf-8"))
    assert written["home_model"]["rooms"]["den"]["fixtures"]["den_pendant_2"]["gamut"]["bound_to"]
