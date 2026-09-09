"""Binding a recorded installation onto the Home Model.

A recording is keyed by whatever the host called things: entity ids, MQTT topics, attribute
names. The engine never learns those names; instead a binding file declares, once per
installation, the naming patterns that turn a model id into the host's ids, and this module
expands those patterns over a model into a reverse index from observed id to engine path.

Patterns use placeholders: ``{fixture}``, ``{room}``, ``{group}``, ``{scene_group}``,
``{scope}``, ``{sensor}``, ``{anchor}``, ``{palette}``, ``{address}``, ``{base_topic}``.
An observed id may name an attribute with ``#``, as in ``sensor.fusion_{scope}#coupling``.
Irregular names are pinned per path under ``overrides``. Palette display names map to palette
ids under ``palette_names``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml

from pascl.core.solar import PERCENT_KEYS
from pascl.model import HomeModel

Transform = Literal["bool", "float", "int", "str", "timer", "palette", "json", "light", "command"]
TargetKind = Literal["input", "latent", "sensor", "output"]


@dataclass(frozen=True)
class Target:
    path: str
    kind: TargetKind
    transform: Transform
    attribute: str | None = None


# (binding section, pattern key) -> (engine path template, record kind, transform)
_FIXTURE: dict[str, tuple[str, TargetKind, Transform]] = {
    "light": ("fixtures.{fixture}.light", "input", "light"),
    "command_topic": ("fixtures.{fixture}", "output", "command"),
    "brightness_factor": ("fixtures.{fixture}.factor", "input", "float"),
    "color_factor": ("fixtures.{fixture}.color_factor", "input", "float"),
    "color_override_x": ("fixtures.{fixture}.override_x", "input", "float"),
    "color_override_y": ("fixtures.{fixture}.override_y", "input", "float"),
    "color_override_kelvin": ("fixtures.{fixture}.override_kelvin", "input", "float"),
    "color_override_is_ct": ("fixtures.{fixture}.override_is_ct", "input", "bool"),
    "held_off": ("fixtures.{fixture}.held_off", "input", "bool"),
    "scene_x": ("fixtures.{fixture}.scene_x", "input", "float"),
    "scene_y": ("fixtures.{fixture}.scene_y", "input", "float"),
    "intent_brightness": ("fixtures.{fixture}.intent.brightness", "latent", "float"),
    "intent_x": ("fixtures.{fixture}.intent.x", "latent", "float"),
    "intent_y": ("fixtures.{fixture}.intent.y", "latent", "float"),
    "intent_ct_mode": ("fixtures.{fixture}.intent.ct_mode", "latent", "bool"),
    "intent_kelvin": ("fixtures.{fixture}.intent.kelvin", "latent", "float"),
    "baseline_target": ("fixtures.{fixture}.baseline_target", "latent", "float"),
    "last_published": ("fixtures.{fixture}.last_published", "latent", "float"),
}
_GROUP: dict[str, tuple[str, TargetKind, Transform]] = {
    "command_topic": ("groups.{room}.{group}", "output", "command"),
}
_ROOM: dict[str, tuple[str, TargetKind, Transform]] = {
    "presence": ("rooms.{room}.presence", "input", "bool"),
    "switch_zone": ("rooms.{room}.switch_zone", "input", "bool"),
    "multiplier": ("rooms.{room}.multiplier", "input", "float"),
    "vacancy_timer": ("rooms.{room}.vacancy_timer", "input", "timer"),
    "hold_active": ("rooms.{room}.hold_active", "input", "bool"),
    "on_override": ("rooms.{room}.on_override", "input", "bool"),
    "hold_ms": ("rooms.{room}.hold_ms", "latent", "float"),
}
_SCENE_GROUP: dict[str, tuple[str, TargetKind, Transform]] = {
    "selector": ("scene_groups.{scene_group}", "input", "palette"),
}
_SCOPE: dict[str, tuple[str, TargetKind, Transform]] = {
    "drive": ("ambient.scope.{scope}.drive", "input", "bool"),
    "coupling": ("ambient.scope.{scope}.coupling", "input", "float"),
}
_GLOBAL: dict[str, tuple[str, TargetKind, Transform]] = {
    "solar_progress": ("solar.progress", "input", "float"),
    "solar_factor": ("solar.factor", "input", "float"),
    "solar_ready": ("solar.ready", "input", "bool"),
    "solar_anchor_pct": ("solar.pct.{anchor}", "input", "float"),
    "sleep": ("sleep", "input", "bool"),
    "scene_offset": ("scene.offset", "input", "int"),
    "palette_json": ("scene.palette.{palette}", "input", "json"),
    "scene_transition_s": ("scene.transition_s", "input", "float"),
    "scene_linger_s": ("scene.linger_s", "input", "float"),
    "ambient_accumulator": ("ambient.accumulator", "input", "float"),
    "ambient_weather": ("ambient.weather", "input", "bool"),
    "main_space_vacancy_timer": ("vacancy.main_space", "input", "timer"),
}
_SECTIONS = ("fixture", "group", "room", "scene_group", "ambient_scope", "sensor", "global")


@dataclass(frozen=True)
class Binding:
    patterns: dict[str, dict[str, str]]
    palette_names: dict[str, str] = field(default_factory=dict)
    """Host display name -> palette id."""
    overrides: dict[str, str] = field(default_factory=dict)
    """Engine path -> observed id, for the irregular names a convention cannot produce."""


def load_binding(text: str) -> Binding:
    raw = yaml.safe_load(text)
    if not isinstance(raw, Mapping) or "binding" not in raw:
        raise ValueError("a binding file has a single top-level 'binding' key")
    b = raw["binding"]
    if not isinstance(b, Mapping):
        raise ValueError("binding must be a mapping")
    unknown = sorted(set(b) - {*_SECTIONS, "version", "palette_names", "overrides"})
    if unknown:
        raise ValueError(f"binding: unknown key(s) {unknown}")
    patterns: dict[str, dict[str, str]] = {}
    for section in _SECTIONS:
        sec = b.get(section) or {}
        if not isinstance(sec, Mapping):
            raise ValueError(f"binding.{section} must be a mapping")
        patterns[section] = {str(k): str(v) for k, v in sec.items()}
    names = {str(k): str(v) for k, v in (b.get("palette_names") or {}).items()}
    overrides = {str(k): str(v) for k, v in (b.get("overrides") or {}).items()}
    return Binding(patterns=patterns, palette_names=names, overrides=overrides)


@dataclass
class Index:
    """Observed id -> Target for host entities; topic -> Target for wire commands."""

    entities: dict[str, Target] = field(default_factory=dict)
    topics: dict[str, Target] = field(default_factory=dict)
    palette_ids: dict[str, str] = field(default_factory=dict)

    def add(self, observed: str, target: Target) -> None:
        if target.transform == "command":
            self.topics[observed] = target
            return
        if "#" in observed:
            entity, attr = observed.split("#", 1)
            self.entities[entity] = Target(target.path, target.kind, target.transform, attr)
        else:
            self.entities[observed] = target


def _fill(template: str, **values: str) -> str:
    out = template
    for k, v in values.items():
        out = out.replace("{" + k + "}", v)
    return out


def expand(binding: Binding, model: HomeModel) -> Index:
    """The reverse index for this model under this binding."""
    idx = Index(palette_ids=dict(binding.palette_names))
    pats = binding.patterns
    rev_overrides = {path: observed for path, observed in binding.overrides.items()}

    def emit(
        section: str, key: str, path: str, kind: TargetKind, transform: Transform, **vals: str
    ) -> None:
        pattern = pats.get(section, {}).get(key)
        observed = rev_overrides.get(path) or (_fill(pattern, **vals) if pattern else None)
        if observed:
            idx.add(observed, Target(path, kind, transform))

    for rid, room in model.rooms.items():
        for key, (tpl, kind, tr) in _ROOM.items():
            emit("room", key, _fill(tpl, room=rid), kind, tr, room=rid)
        for fid, fx in room.fixtures.items():
            base = model.transports[fx.transport].base_topic or ""
            for key, (tpl, kind, tr) in _FIXTURE.items():
                emit(
                    "fixture",
                    key,
                    _fill(tpl, fixture=fid),
                    kind,
                    tr,
                    fixture=fid,
                    room=rid,
                    address=fx.address or fid,
                    base_topic=base,
                )
        for gid, g in room.groups.items():
            base = model.transports[g.transport].base_topic or ""
            for key, (tpl, kind, tr) in _GROUP.items():
                emit(
                    "group",
                    key,
                    _fill(tpl, room=rid, group=gid),
                    kind,
                    tr,
                    room=rid,
                    group=gid,
                    address=g.address or gid,
                    base_topic=base,
                )
        for sgid in room.scene_groups:
            for key, (tpl, kind, tr) in _SCENE_GROUP.items():
                emit(
                    "scene_group",
                    key,
                    _fill(tpl, scene_group=sgid),
                    kind,
                    tr,
                    scene_group=sgid,
                    room=rid,
                )
    for sid in model.ambient_scopes:
        for key, (tpl, kind, tr) in _SCOPE.items():
            emit("ambient_scope", key, _fill(tpl, scope=sid), kind, tr, scope=sid)
    for sid, sensor in model.sensors.items():
        if sensor.address:
            transform: Transform = "float" if sensor.modality == "lux" else "bool"
            idx.add(sensor.address, Target(f"sensors.{sid}", "sensor", transform))
    for key, (tpl, kind, tr) in _GLOBAL.items():
        if key == "solar_anchor_pct":
            for anchor in PERCENT_KEYS:
                emit("global", key, _fill(tpl, anchor=anchor), kind, tr, anchor=anchor)
        elif key == "palette_json":
            for pid in model.scenes.palettes:
                emit("global", key, _fill(tpl, palette=pid), kind, tr, palette=pid)
        else:
            emit("global", key, tpl, kind, tr)
    return idx


def transform_value(
    transform: Transform,
    state: Any,
    attrs: Mapping[str, Any] | None,
    palette_ids: Mapping[str, str],
) -> Any:
    """A host state into the engine's value for that path. Unknown/unavailable become None."""
    if state in (None, "unknown", "unavailable", "none", ""):
        return None
    if transform == "bool":
        if isinstance(state, bool):
            return state
        return str(state).lower() in ("on", "true", "occupied", "detected", "home", "open", "1")
    if transform == "float":
        try:
            return float(state)
        except (TypeError, ValueError):
            return None
    if transform == "int":
        try:
            return int(float(state))
        except (TypeError, ValueError):
            return None
    if transform == "timer":
        return str(state)
    if transform == "palette":
        name = str(state)
        return palette_ids.get(name, name.lower().replace(" ", "_"))
    if transform == "json":
        try:
            return json.loads(str(state))
        except ValueError:
            return None
    if transform == "light":
        a = attrs or {}
        return {
            "on": str(state).lower() == "on",
            "brightness": a.get("brightness"),
            "xy": a.get("xy_color"),
            "ct_mired": a.get("color_temp"),
            "color_mode": a.get("color_mode"),
        }
    return str(state)
