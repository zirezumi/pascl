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
from pascl.shell.gamut_runtime import (
    command_topics,
    device_ids,
    device_infos,
    group_topics,
    host_view,
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

    def __init__(self, polygons: dict[str, Polygon], states: dict[str, str]) -> None:
        self.polygons = polygons
        self.states = states
        self.inbox: list[tuple[str, str]] = []
        self.published: list[tuple[str, str]] = []
        self.subscribed: list[str] = []
        self.current: dict[str, XY] = {}
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
        if verb == "set" and "color" in body:
            xy = (float(body["color"]["x"]), float(body["color"]["y"]))
            self.current[name] = project(xy, self.polygons[name])
            self.inbox.append((base, json.dumps({"color": body["color"], "state": "OFF"})))
        elif verb == "get" and "color" in body:
            x, y = self.current.get(name, (0.4, 0.4))
            self.inbox.append((base, json.dumps({"color": {"x": x, "y": y}, "state": "OFF"})))
        elif verb == "get" and "state" in body:
            x, y = self.current.get(name, (0.4, 0.4))
            self.inbox.append(
                (base, json.dumps({"state": "OFF", "color_mode": "xy", "color": {"x": x, "y": y}}))
            )

    def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)
        if topic.endswith("/bridge/devices"):
            self.inbox.append((topic, json.dumps(self.devices)))

    def drain(self, seconds: float) -> Iterator[tuple[str, str]]:
        out, self.inbox = self.inbox, []
        yield from out

    # -- REST --------------------------------------------------------------------------
    def get_states(self) -> dict[str, str]:
        self.state_reads += 1
        if self.occupy_after is not None and self.state_reads > self.occupy_after:
            return {**self.states, "binary_sensor.den_presence": "on"}
        return dict(self.states)

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
    def make(link: object, topic: str, watch: list[str]) -> TimedChannel:
        ch = TimedChannel(link, topic, watch=watch)  # type: ignore[arg-type]
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
    channel = make(home, "z2m-1/den_pendant_1/set", group_topics(model, index, "den_pendant_1"))  # type: ignore[operator]
    assert "z2m-1/den/set" in home.subscribed
    channel.command((0.95, 0.04))
    home.inbox.append(("z2m-1/den/set", '{"brightness": 120}'))
    kinds = [o.kind for o in channel.observe(0.1)]
    assert kinds == ["echo", "foreign"]


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
