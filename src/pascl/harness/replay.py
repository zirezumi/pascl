"""Replay: the pure render against a recorded trace.

A trace carries every input the reference installation's render read and every command it put
on the wire. Replay walks the records in time order, keeps the input vector current, and at each
wire command re-renders the fixtures that command covered from the same inputs, then compares
the frame with the command field by field under the contract's tolerances: on/off and colour
mode exactly, brightness within two device units, colour within a CIELAB distance of two.
A command a fixture would not have received (a phantom group, a bare transition) is counted as
skipped, never silently dropped, so the coverage of a replay is always stated.

The comparison is per command, not per tick: it judges the value the reference chose whenever it
chose to publish, and says nothing about whether the engine would have published then. Publish
timing is the reconciler's business and is compared separately.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from math import sqrt
from typing import Any, Final

from pascl.core.color import XY
from pascl.core.palette import NaturalWhite, default_warm_offset, kelvin_xy, natural_white
from pascl.core.render import (
    FixtureState,
    Frame,
    Phase,
    RoomState,
    SolarState,
    ambient_factor,
    render_fixture,
)
from pascl.harness.preview import site_of, solar_state
from pascl.harness.trace import Record
from pascl.model import HomeModel
from pascl.model.schema import Calibration, Palette

BRIGHTNESS_TOLERANCE: Final = 2
DELTA_E_TOLERANCE: Final = 2.0

# D65 reference white for the CIELAB distance.
_XN: Final = 0.95047
_ZN: Final = 1.08883


@dataclass(frozen=True)
class Verdict:
    field: str
    ok: bool
    expected: Any
    got: Any
    delta: float | None = None


@dataclass(frozen=True)
class Sample:
    t: datetime
    fixture: str
    output: str
    command: dict[str, Any]
    frame: Frame
    verdicts: tuple[Verdict, ...]
    context: dict[str, Any]

    @property
    def ok(self) -> bool:
        return all(v.ok for v in self.verdicts)


@dataclass
class ReplayResult:
    samples: list[Sample] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    fields: Counter[str] = field(default_factory=Counter)
    notes: Counter[str] = field(default_factory=Counter)
    """Things the replay had to supply itself, counted so a clean run states its basis."""

    @property
    def mismatches(self) -> list[Sample]:
        return [s for s in self.samples if not s.ok]

    def summary(self) -> str:
        total = len(self.samples)
        bad = len(self.mismatches)
        lines = [f"samples {total}, matched {total - bad}, mismatched {bad}"]
        for name in ("on", "brightness", "mode", "colour"):
            ok, miss = self.fields[f"{name}:ok"], self.fields[f"{name}:miss"]
            if ok or miss:
                lines.append(f"  {name:10s} ok {ok:5d}  miss {miss:5d}")
        if self.skipped:
            lines.append("skipped: " + ", ".join(f"{k}={v}" for k, v in self.skipped.items()))
        if self.notes:
            lines.append("notes: " + ", ".join(f"{k}={v}" for k, v in self.notes.items()))
        return "\n".join(lines)


def delta_e(a: XY, b: XY) -> float:
    """CIE76 distance between two chromaticities at unit luminance (so only a* and b* differ)."""
    la, lb = _lab(a), _lab(b)
    return sqrt((la[0] - lb[0]) ** 2 + (la[1] - lb[1]) ** 2)


def _lab(xy: XY) -> tuple[float, float]:
    x, y = xy
    yy = y if y > 1e-9 else 1e-9
    big_x, big_z = x / yy, (1.0 - x - y) / yy
    fx, fy, fz = _f(big_x / _XN), _f(1.0), _f(big_z / _ZN)
    return (500.0 * (fx - fy), 200.0 * (fy - fz))


def _f(t: float) -> float:
    eps = (6.0 / 29.0) ** 3
    if t > eps:
        return float(t ** (1.0 / 3.0))
    return t / (3.0 * (6.0 / 29.0) ** 2) + 4.0 / 29.0


class _State:
    """The input vector as of the record last applied, and when each path last changed."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.stamps: dict[str, datetime] = {}

    def apply(self, r: Record) -> None:
        if r.kind == "snapshot":
            self.values.update(r.data)
        elif r.kind in ("input", "latent", "sensor"):
            self.values[r.path] = r.value
            self.stamps[r.path] = r.t

    def get(self, path: str, default: Any = None) -> Any:
        v = self.values.get(path)
        return default if v is None else v


def _fixture_rooms(model: HomeModel) -> dict[str, str]:
    return {fid: rid for rid, room in model.rooms.items() for fid in room.fixtures}


def _covered(model: HomeModel, path: str, rooms: dict[str, str]) -> tuple[list[str], str | None]:
    """The fixtures a wire command on ``path`` reaches, or the reason none does."""
    parts = path.split(".")
    if parts[0] == "fixtures" and len(parts) == 2:
        return ([parts[1]], None) if parts[1] in rooms else ([], "unknown_fixture")
    if parts[0] == "groups" and len(parts) == 3:
        room = model.rooms.get(parts[1])
        group = room.groups.get(parts[2]) if room else None
        if room is None or group is None:
            return [], "unknown_group"
        members = [m for m in group.members if m in room.fixtures]
        return (members, None) if members else ([], f"no_fixture_member:{group.tier}")
    return [], "unknown_output_path"


def _solar(state: _State) -> SolarState:
    progress = state.get("solar.progress", -1.0)
    pct = {
        k[len("solar.pct.") :]: float(v)
        for k, v in state.values.items()
        if k.startswith("solar.pct.") and v is not None
    }
    return SolarState(
        progress=float(progress),
        factor=float(state.get("solar.factor", 0.0)),
        pct=pct,
        ready=bool(state.get("solar.ready", True)),
    )


def _phase(model: HomeModel, state: _State, room_id: str) -> Phase:
    presence_path = f"rooms.{room_id}.presence"
    if state.get(presence_path, False):
        return "occupied"
    room = model.rooms[room_id]
    timer_path = (
        f"spaces.{room.space}.vacancy_timer"
        if room.vacancy.scope == "space"
        else f"rooms.{room_id}.vacancy_timer"
    )
    timer_state, _, why = str(state.get(timer_path, "")).partition(":")
    if timer_state == "active":
        return "fading"
    # The reference starts its vacancy timer a beat after presence clears, and its clearing run
    # renders inside that beat: a presence edge newer than the timer's last change means the
    # room is still lit and fading, not swept.
    t_presence, t_timer = state.stamps.get(presence_path), state.stamps.get(timer_path)
    if t_presence is not None and (t_timer is None or t_presence > t_timer):
        return "fading"
    # An idle timer that was cancelled rather than finished never swept the room: a shared
    # space's timer is cancelled whenever any other member is occupied, and the room stays lit
    # on its vacant tier until the space as a whole empties.
    if why == "cancelled":
        return "fading"
    return "vacant"


def _room_state(model: HomeModel, state: _State, room_id: str) -> RoomState:
    room = model.rooms[room_id]
    ambient = 1.0
    if room.ambient_scope:
        scope = model.ambient_scopes.get(room.ambient_scope)
        drive = bool(
            state.get(f"ambient.scope.{room.ambient_scope}.drive", scope.drive if scope else False)
        )
        coupling = state.get(f"ambient.scope.{room.ambient_scope}.coupling")
        ambient = ambient_factor(
            None if coupling is None else float(coupling),
            float(state.get("ambient.accumulator", 1.0)),
            drive,
        )
    return RoomState(
        phase=_phase(model, state, room_id),
        multiplier=float(state.get(f"rooms.{room_id}.multiplier", 1.0)),
        ambient=ambient,
        sleeping=bool(state.get("sleep", False)),
    )


def _pair(state: _State, x_path: str, y_path: str) -> XY | None:
    x, y = state.get(x_path), state.get(y_path)
    if x is None or y is None:
        return None
    return (float(x), float(y))


def _fixture_state(
    state: _State, fid: str, recorded_scene_colour: bool, last_ct_mode: bool
) -> FixtureState:
    p = f"fixtures.{fid}"
    cached = state.get(f"{p}.intent.ct_mode")
    kelvin = state.get(f"{p}.override_kelvin")
    return FixtureState(
        factor=float(state.get(f"{p}.factor", 1.0)),
        held_off=bool(state.get(f"{p}.held_off", False)),
        color_factor=float(state.get(f"{p}.color_factor", 0.0)),
        override_xy=_pair(state, f"{p}.override_x", f"{p}.override_y"),
        override_kelvin=None if kelvin is None else float(kelvin),
        override_is_ct=bool(state.get(f"{p}.override_is_ct", False)),
        ct_mode_cached=last_ct_mode if cached is None else bool(cached),
        scene_xy=_pair(state, f"{p}.scene_x", f"{p}.scene_y") if recorded_scene_colour else None,
    )


def _palette(model: HomeModel, state: _State, room_id: str, fid: str) -> Palette | None:
    fx = model.rooms[room_id].fixtures[fid]
    if fx.scene_group is None:
        return None
    group = model.rooms[room_id].scene_groups[fx.scene_group]
    pid = state.get(f"scene_groups.{fx.scene_group}", group.default)
    return model.scenes.palettes.get(str(pid))


def _calibration(model: HomeModel, room_id: str, fid: str) -> Calibration | None:
    material = model.materials[model.rooms[room_id].fixtures[fid].material]
    return model.calibrations.get(material.calibration) if material.calibration else None


def _judge(frame: Frame, command: dict[str, Any], calibration: Calibration | None) -> list[Verdict]:
    out: list[Verdict] = []
    if "on" in command:
        want = bool(command["on"])
        out.append(Verdict("on", frame.on == want, want, frame.on))
    if "brightness" in command and (frame.on or frame.virtual or command.get("on") is not False):
        want_b = int(command["brightness"])
        got_b = frame.brightness
        ok = got_b is not None and abs(got_b - want_b) <= BRIGHTNESS_TOLERANCE
        out.append(
            Verdict(
                "brightness", ok, want_b, got_b, None if got_b is None else float(got_b - want_b)
            )
        )
    engine_mode = "ct" if frame.ct_mired is not None else ("xy" if frame.xy is not None else "none")
    if "xy" in command:
        want_xy: XY = (float(command["xy"][0]), float(command["xy"][1]))
        out.append(Verdict("mode", engine_mode == "xy", "xy", engine_mode))
        got_xy = frame.xy
        if got_xy is None and frame.ct_mired is not None and calibration is not None:
            got_xy = kelvin_xy(1e6 / frame.ct_mired, calibration)
        de = None if got_xy is None else delta_e(got_xy, want_xy)
        out.append(
            Verdict("colour", de is not None and de <= DELTA_E_TOLERANCE, want_xy, got_xy, de)
        )
    elif "ct_mired" in command:
        want_ct = int(command["ct_mired"])
        out.append(Verdict("mode", engine_mode == "ct", "ct", engine_mode))
        if calibration is not None:
            want_xy2 = kelvin_xy(1e6 / want_ct, calibration)
            got_xy2 = (
                frame.xy if frame.ct_mired is None else kelvin_xy(1e6 / frame.ct_mired, calibration)
            )
            de = None if got_xy2 is None else delta_e(got_xy2, want_xy2)
        else:
            de = None if frame.ct_mired is None else float(abs(frame.ct_mired - want_ct))
        out.append(
            Verdict(
                "colour",
                de is not None and de <= DELTA_E_TOLERANCE,
                want_ct,
                frame.ct_mired if frame.ct_mired is not None else frame.xy,
                de,
            )
        )
    return out


def replay(
    model: HomeModel,
    records: Iterable[Record],
    *,
    rooms: Iterable[str] | None = None,
    recorded_scene_colour: bool = True,
) -> ReplayResult:
    """Render at every wire command in ``records`` and compare.

    ``recorded_scene_colour`` uses the per-fixture scene colour the reference's scene engine
    wrote (so the render is judged on its own); ``False`` makes the engine derive the colour
    from the palette recipe instead, which judges the palette port as well.
    """
    wanted = set(rooms) if rooms is not None else None
    fixture_rooms = _fixture_rooms(model)
    state = _State()
    result = ReplayResult()
    last_ct_mode: dict[str, bool] = {}
    warm_default = default_warm_offset(model)
    derived_pct: dict[date, dict[str, float]] = {}
    tz = site_of(model).tz

    for r in records:
        if r.kind != "output":
            state.apply(r)
            continue
        fixtures, why = _covered(model, r.path, fixture_rooms)
        if why is not None:
            result.skipped[why] += 1
            continue
        command = {k: v for k, v in r.data.items() if k in ("on", "brightness", "xy", "ct_mired")}
        if not command:
            result.skipped["no_comparable_field"] += 1
            continue
        solar = _solar(state)
        if not solar.pct:
            # A recording that never saw the anchor sensors change carries none; the solar
            # clock is pure, so derive the day's anchors from the model instead of defaulting.
            day = r.t.astimezone(tz).date()
            if day not in derived_pct:
                derived_pct[day] = dict(solar_state(model, r.t).pct)
            solar = SolarState(solar.progress, solar.factor, derived_pct[day], solar.ready)
            result.notes["anchors_derived_from_model"] += 1
        white: NaturalWhite = natural_white(solar.progress, solar.factor, solar.pct)
        scene_offset = int(state.get("scene.offset", 0))
        warm_offset = float(state.get("scene.warm_offset_k", warm_default))
        for fid in fixtures:
            rid = fixture_rooms[fid]
            if wanted is not None and rid not in wanted:
                result.skipped["other_room"] += 1
                continue
            room_state = _room_state(model, state, rid)
            light = state.get(f"fixtures.{fid}.light")
            if light is None and room_state.phase == "fading":
                # The reference's recovery frame: a fixture whose host state is unknown (a
                # restart, a coordinator drop) in a room that is not occupied is driven OFF to
                # re-establish a known state. That is the reconciler's rule, not the render's,
                # so the replay applies it here and counts it.
                room_state = RoomState(
                    "vacant", room_state.multiplier, room_state.ambient, room_state.sleeping
                )
                result.notes["recovery_off_assumed"] += 1
            fx_state = _fixture_state(
                state, fid, recorded_scene_colour, last_ct_mode.get(fid, False)
            )
            frame = render_fixture(
                model,
                rid,
                fid,
                solar,
                room_state,
                fx_state,
                white,
                _palette(model, state, rid, fid),
                scene_offset,
                warm_offset,
            )
            last_ct_mode[fid] = frame.ct_mired is not None
            verdicts = _judge(frame, command, _calibration(model, rid, fid))
            for v in verdicts:
                result.fields[f"{v.field}:{'ok' if v.ok else 'miss'}"] += 1
            light = state.get(f"fixtures.{fid}.light") or {}
            context = {
                "phase": room_state.phase,
                "presence": state.get(f"rooms.{rid}.presence"),
                "light_on": light.get("on") if isinstance(light, dict) else None,
                "p": solar.progress,
                "factor": solar.factor,
                "white_k": white.kelvin_rounded,
                "multiplier": room_state.multiplier,
                "ambient": room_state.ambient,
                "fixture_factor": fx_state.factor,
                "held_off": fx_state.held_off,
                "color_factor": fx_state.color_factor,
                "scene_xy": fx_state.scene_xy,
                "ct_cached": fx_state.ct_mode_cached,
                "sleep": room_state.sleeping,
            }
            result.samples.append(
                Sample(r.t, fid, r.path, command, frame, tuple(verdicts), context)
            )
    return result


def describe(sample: Sample) -> str:
    """One line per failing field, with the inputs the frame was rendered from."""
    bad = [v for v in sample.verdicts if not v.ok]
    when = sample.t.isoformat(timespec="seconds")
    fields = "; ".join(
        f"{v.field}: expected {v.expected} got {v.got}"
        + (f" (Δ {v.delta:.3f})" if v.delta is not None else "")
        for v in bad
    )
    ctx = " ".join(f"{k}={v}" for k, v in sample.context.items())
    return f"{when} {sample.fixture} via {sample.output}: {fields} | {ctx}"
