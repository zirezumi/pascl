"""The gamut calibration as the runtime runs it, unattended.

One tick: read the host (which fixtures are lit, which rooms occupied, which device sits
behind each fixture), let ``pick_target`` name the next dark fixture in an empty room, measure
it, record the result in the model, and let the measurement travel to its same-model
siblings as seeds. Nothing is visible: a dark fixture accepts colour silently and is put back
afterwards, and the measurement aborts the moment the fixture lights or the room fills. The
tick is a function so the add-on can call it on a timer, an automation can call it once, and
a test can call it against fakes; ``run`` is the loop the command line offers.

The host is reached through the same binding the replay uses: the light entity behind a
fixture and the presence entity behind a room are whatever the binding says they are, so this
module knows no host name either.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

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
from pascl.shell.gamut_measure import gamut_from, measure
from pascl.shell.z2m import DeviceInfo, MqttLink, Z2MDeviceChannel, bridge_devices


class Host(MqttLink, Protocol):
    """An MQTT link that can also read the host's entity states."""

    def get_states(self) -> Mapping[str, str]: ...


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


def tick(
    model: HomeModel,
    index: Index,
    host: Host,
    *,
    write: Path | None,
    clock: Clock | None = None,
    channel_factory: Callable[[MqttLink, str, list[str]], Z2MDeviceChannel] | None = None,
) -> tuple[HomeModel, Tick]:
    """One pass: pick, measure, record, propagate. Returns the (possibly updated) model and
    what happened. A measurement is written only if the model still validates with it (a
    polygon that contradicts its same-model siblings beyond the model's limit is refused
    loudly rather than stored), and never while ``write`` is None."""
    clk = clock or SystemClock()
    states = host.get_states()
    view = host_view(model, index, states)
    infos = device_infos(model, host)
    ids = device_ids(model, infos)
    remaining = sum(1 for s in statuses(model, ids) if s.status != "measured")
    pick = pick_target(model, ids, view.lit, view.occupied)
    if pick is None:
        return model, Tick(None, notes=("nothing to measure now",), remaining=remaining)
    topics = command_topics(model, index)
    topic = topics.get(pick.fixture)
    if topic is None:
        note = f"{pick.fixture} has no command topic in the binding"
        return model, Tick(pick, notes=(note,), remaining=remaining)
    make = channel_factory or (lambda link, t, w: Z2MDeviceChannel(link, t, watch=w))
    channel = make(host, topic, group_topics(model, index, pick.fixture))
    info = infos.get(pick.fixture)
    if info is not None:
        channel.adopt(info)

    def room_filled() -> str | None:
        now = host_view(model, index, host.get_states())
        if pick.room in now.occupied:
            return f"room {pick.room} became occupied"
        return None

    verdict = measure(channel, clock=clk, abort_when=room_filled)
    if verdict is None:
        note = f"{pick.fixture} was lit by the time it was tried"
        return model, Tick(pick, notes=(note,), remaining=remaining)
    notes = list(verdict.notes)
    gamut = gamut_from(verdict, channel, clk)
    if gamut is None:
        return model, Tick(pick, verdict, notes=tuple(notes), remaining=remaining)
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
    sleep: Callable[[float], None] = time.sleep,
) -> HomeModel:
    """The loop: a tick, then ``interval_s`` of rest, until every colour fixture is measured
    on its own device, or just one tick with ``once`` (what a scheduler or an automation
    calls). A tick that finds nothing dark and empty is not the end: it is rest."""
    while True:
        model, result = tick(model, index, host, write=write)
        report(result)
        if once or (result.picked is None and result.remaining == 0):
            return model
        sleep(interval_s)
