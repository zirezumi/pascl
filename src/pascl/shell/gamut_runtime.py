"""The gamut calibration as the runtime runs it, unattended.

One tick: read the host (which fixtures are lit, which rooms occupied, which device sits
behind each fixture), let ``pick_target`` name the next dark fixture in an empty room, measure
it, record the result in the model, and let the measurement travel to its same-model
siblings as seeds. Nothing is visible: a dark fixture accepts colour silently and is put back
afterwards, and the measurement aborts the moment the fixture lights or the room fills. A
device that does not accept colour while dark is diagnosed on its first samples and set
aside with the reason (the forced mode switches it on to measure it), as is one that never
answers or never settles. The tick is a function so the add-on can call it on a timer, an
automation can call it once, and a test can call it against fakes; ``run`` is the loop the
command line offers.

The host is reached through the same binding the replay uses: the light entity behind a
fixture and the presence entity behind a room are whatever the binding says they are, so this
module knows no host name either.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pascl.clock import Clock, SystemClock
from pascl.estimator.gamut import (
    FixtureStatus,
    Seed,
    Verdict,
    confirm_seed,
    inherit,
    pick_target,
    statuses,
)
from pascl.harness.binding import Index
from pascl.model import HomeModel, dumps, validate, with_fixture_gamut
from pascl.shell.gamut_measure import (
    NEVER_SETTLES,
    NO_READ_ANSWER,
    NOT_APPLIED_LIT,
    NOT_APPLIED_WHILE_OFF,
    DeviceChannel,
    gamut_from,
    measure,
)
from pascl.shell.ha_light import HALightChannel
from pascl.shell.z2m import DeviceInfo, MqttLink, Z2MDeviceChannel, bridge_devices


class Host(MqttLink, Protocol):
    """An MQTT link that can also read the host's entity states and relay one entity's
    state changes as messages on a topic of its own."""

    def get_states(self) -> Mapping[str, str]: ...

    def watch_entity(self, entity_id: str) -> str: ...


@dataclass(frozen=True)
class HostView:
    lit: frozenset[str]
    """Fixtures whose light entity reads on."""
    occupied: frozenset[str]
    """Rooms whose presence reads on (a room in a space counts the space's presence too)."""


def host_view(model: HomeModel, index: Index, states: Mapping[str, str]) -> HostView:
    """What the host's entity states say about the fixtures and rooms the binding names."""
    lit: set[str] = set()
    occupied: set[str] = set()
    for entity, target in index.entities.items():
        parts = target.path.split(".")
        if states.get(entity) != "on":
            continue
        if len(parts) == 3 and parts[0] == "fixtures" and parts[2] == "light":
            lit.add(parts[1])
        elif len(parts) == 3 and parts[0] == "rooms" and parts[2] == "presence":
            occupied.add(parts[1])
        elif len(parts) == 3 and parts[0] == "spaces" and parts[2] == "presence":
            occupied.update(rid for rid, room in model.rooms.items() if room.space == parts[1])
    return HostView(frozenset(lit), frozenset(occupied))


def command_topics(model: HomeModel, index: Index) -> dict[str, str]:
    """fixture id -> its /set topic under the binding."""
    out: dict[str, str] = {}
    for topic, target in index.topics.items():
        parts = target.path.split(".")
        if len(parts) == 2 and parts[0] == "fixtures":
            out[parts[1]] = topic
    return out


def presence_entities(model: HomeModel, index: Index, room_id: str) -> list[str]:
    """The host entities whose turning on means the room is about to render: the room's
    presence and switch-zone composites, and the presence of the space it belongs to."""
    room = model.rooms.get(room_id)
    wanted = {f"rooms.{room_id}.presence", f"rooms.{room_id}.switch_zone"}
    if room is not None and room.space is not None:
        wanted.add(f"spaces.{room.space}.presence")
    return sorted(entity for entity, target in index.entities.items() if target.path in wanted)


def group_topics(model: HomeModel, index: Index, fixture_id: str) -> list[str]:
    """The /set topics of every group the fixture is a member of: commands there reach the
    device without touching its own topic, so a measurement must watch them as foreign."""
    out: list[str] = []
    for rid, room in model.rooms.items():
        if fixture_id not in room.fixtures:
            continue
        member_of = {gid for gid, g in room.groups.items() if fixture_id in g.members}
        for topic, target in index.topics.items():
            parts = target.path.split(".")
            if (
                len(parts) == 3
                and parts[0] == "groups"
                and parts[1] == rid
                and parts[2] in member_of
            ):
                out.append(topic)
    return sorted(out)


def device_infos(model: HomeModel, link: MqttLink, seconds: float = 3.0) -> dict[str, DeviceInfo]:
    """fixture id -> the transport's device record, for every fixture on a Zigbee2MQTT
    transport whose coordinator lists it. One retained read per coordinator."""
    lists: dict[str, dict[str, DeviceInfo]] = {}
    out: dict[str, DeviceInfo] = {}
    for room in model.rooms.values():
        for fid, fx in room.fixtures.items():
            tr = model.transports.get(fx.transport)
            if tr is None or tr.kind != "z2m" or not tr.base_topic:
                continue
            if tr.base_topic not in lists:
                lists[tr.base_topic] = bridge_devices(link, tr.base_topic, seconds)
            name = (fx.address or fid).split("/", 1)[0]
            info = lists[tr.base_topic].get(name)
            if info is not None:
                out[fid] = info
    return out


def device_ids(model: HomeModel, infos: Mapping[str, DeviceInfo]) -> dict[str, str]:
    """fixture id -> the identity a polygon is bound to: the IEEE address, plus the endpoint
    for a fixture that is one endpoint of a device."""
    out: dict[str, str] = {}
    for room in model.rooms.values():
        for fid, fx in room.fixtures.items():
            info = infos.get(fid)
            if info is None:
                continue
            _name, _sep, ep = (fx.address or fid).partition("/")
            out[fid] = info.ieee_address + (f"/{ep}" if ep else "")
    return out


@dataclass(frozen=True)
class Tick:
    """What one tick did."""

    picked: FixtureStatus | None
    verdict: Verdict | None = None
    seed_gap: float | None = None
    """How far the fixture's own measurement sat from the seed it held, if it held one."""
    seeds: tuple[Seed, ...] = ()
    """Seeds applied after the measurement travelled."""
    written: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)
    remaining: int = 0
    """Colour fixtures still not measured on their own device after this tick."""
    set_aside: bool = False
    """The picked fixture cannot be measured this way; the loop stops picking it."""


ChannelFactory = Callable[[Any, str, list[str], list[str]], DeviceChannel]


def channel_for(
    model: HomeModel,
    index: Index,
    host: Any,
    fixture_id: str,
    infos: Mapping[str, DeviceInfo],
    *,
    z2m_factory: ChannelFactory | None = None,
) -> tuple[DeviceChannel, str] | None:
    """The channel that reaches a fixture, chosen by its transport, and the key its airtime
    is shared under: a Zigbee2MQTT fixture gets the Z2M channel on its command topic (key:
    the coordinator's base topic); a fixture on any other transport that the host exposes as
    a light entity gets the entity channel (key: the transport id); None when neither the
    binding nor the host offers a way in. The room's presence entities are watched either
    way, so a measurement aborts ahead of the render a presence return triggers."""
    room_id = next((rid for rid, room in model.rooms.items() if fixture_id in room.fixtures), None)
    if room_id is None:
        return None
    fx = model.rooms[room_id].fixtures[fixture_id]
    transport = model.transports.get(fx.transport)
    occupancy = [host.watch_entity(e) for e in presence_entities(model, index, room_id)]
    if transport is not None and transport.kind == "z2m" and transport.base_topic:
        topic = command_topics(model, index).get(fixture_id)
        if topic is None:
            return None
        make = z2m_factory or (
            lambda link, t, w, o: Z2MDeviceChannel(link, t, watch=w, occupancy=o)
        )
        channel = make(host, topic, group_topics(model, index, fixture_id), occupancy)
        info = infos.get(fixture_id)
        if info is not None and isinstance(channel, Z2MDeviceChannel):
            channel.adopt(info)
        return channel, transport.base_topic
    entity = next(
        (e for e, t in index.entities.items() if t.path == f"fixtures.{fixture_id}.light"), None
    )
    if entity is None:
        return None
    light = HALightChannel(host, entity, occupancy=occupancy)
    light.discover()
    return light, fx.transport


def takes_ct(model: HomeModel, fixture_id: str) -> bool:
    """Whether the fixture's material takes a colour temperature, so its range is measured
    with its polygon; a material without ``cct`` is never sent one and carries no range."""
    for room in model.rooms.values():
        fx = room.fixtures.get(fixture_id)
        if fx is not None:
            mat = model.materials.get(fx.material)
            return mat is not None and "cct" in mat.capabilities
    return False


def tick(
    model: HomeModel,
    index: Index,
    host: Host,
    *,
    write: Path | None,
    clock: Clock | None = None,
    channel_factory: ChannelFactory | None = None,
    exclude: Collection[str] = (),
    force: bool = False,
) -> tuple[HomeModel, Tick]:
    """One pass: pick, measure, record, propagate. Returns the (possibly updated) model and
    what happened. The measurement watches the room's presence entities through the host,
    so it aborts and restores the fixture ahead of the render a presence return triggers. A
    measurement is written only if the model still validates with it (a polygon that
    contradicts its same-model siblings beyond the model's limit is refused loudly rather
    than stored), and never while write is None. force is the visible mode: lit
    fixtures and occupied rooms included."""
    clk = clock or SystemClock()
    states = host.get_states()
    view = host_view(model, index, states)
    infos = device_infos(model, host)
    ids = device_ids(model, infos)
    remaining = sum(1 for s in statuses(model, ids) if s.status != "measured")
    pick = pick_target(
        model,
        ids,
        () if force else view.lit,
        () if force else view.occupied,
        exclude,
    )
    if pick is None:
        return model, Tick(None, notes=("nothing to measure now",), remaining=remaining)
    found = channel_for(model, index, host, pick.fixture, infos, z2m_factory=channel_factory)
    if found is None:
        note = f"{pick.fixture}: neither a command topic nor a light entity reaches it"
        return model, Tick(pick, notes=(note,), remaining=remaining, set_aside=True)
    channel, _key = found
    if channel.needs_lit and not force:
        note = f"{pick.fixture}: its transport cannot measure a dark fixture; forced mode only"
        return model, Tick(pick, notes=(note,), remaining=remaining, set_aside=True)

    def room_filled() -> str | None:
        if force:
            return None
        now = host_view(model, index, host.get_states())
        if pick.room in now.occupied:
            return f"room {pick.room} became occupied"
        return None

    verdict = measure(
        channel,
        allow_lit=force,
        clock=clk,
        abort_when=room_filled,
        ct=takes_ct(model, pick.fixture),
    )
    if verdict is None:
        note = f"{pick.fixture} was lit by the time it was tried"
        return model, Tick(pick, notes=(note,), remaining=remaining)
    notes = list(verdict.notes)
    gamut = gamut_from(verdict, channel, clk)
    if gamut is None:
        # a diagnosis about the device itself is final for this run: colour it will not
        # apply while off (the forced mode switches it on), or not at all, or too slowly
        aside = verdict.aborted is not None and verdict.aborted.startswith(
            (NOT_APPLIED_WHILE_OFF, NOT_APPLIED_LIT, NEVER_SETTLES)
        )
        return model, Tick(pick, verdict, notes=tuple(notes), remaining=remaining, set_aside=aside)
    held = model.rooms[pick.room].fixtures[pick.fixture].gamut
    seed_gap: float | None = None
    if held is not None and held.inherited_from is not None:
        agreed, seed_gap = confirm_seed(held, gamut.vertices)
        notes.append(
            f"seed from {held.inherited_from} {'confirmed' if agreed else 'CONTRADICTED'} "
            f"(gap {seed_gap:.5f})"
        )
    updated = with_fixture_gamut(model, pick.fixture, gamut)
    problems = validate(updated)
    if problems:
        notes.extend(f"refused: {p}" for p in problems)
        return model, Tick(pick, verdict, seed_gap, notes=tuple(notes), remaining=remaining)
    updated, applied = inherit(updated, ids)
    written = False
    if write is not None:
        write.write_text(dumps(updated), encoding="utf-8")
        written = True
    remaining = sum(1 for s in statuses(updated, ids) if s.status != "measured")
    return updated, Tick(pick, verdict, seed_gap, tuple(applied), written, tuple(notes), remaining)


def run(
    model: HomeModel,
    index: Index,
    host: Host,
    *,
    write: Path | None,
    interval_s: float,
    report: Callable[[Tick], None],
    once: bool = False,
    force: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> HomeModel:
    """The loop: a tick, then interval_s of rest, until every colour fixture is measured
    on its own device, or just one tick with once (what a scheduler or an automation
    calls). A tick that finds nothing dark and empty is not the end: it is rest. A fixture a
    tick set aside (no way in, a transport that needs the forced mode, or a device the
    measurement diagnosed) is not picked again in this run; one that answered no read-back
    is given a second tick, since a busy transport looks the same, and set aside after."""
    set_aside: set[str] = set()
    silent: dict[str, int] = {}
    while True:
        model, result = tick(model, index, host, write=write, exclude=set_aside, force=force)
        report(result)
        if result.picked is not None:
            name = result.picked.fixture
            if result.set_aside:
                set_aside.add(name)
            reason = result.verdict.aborted if result.verdict is not None else None
            if reason is not None and reason.startswith(NO_READ_ANSWER):
                silent[name] = silent.get(name, 0) + 1
                if silent[name] >= 2:
                    set_aside.add(name)
        if once or (result.picked is None and result.remaining <= len(set_aside)):
            return model
        sleep(interval_s)
