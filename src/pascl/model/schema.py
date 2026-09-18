"""Home Model schema v0.

The one file that describes a home to the engine: where it is, what carries its commands, what
its fixtures are made of, which sensors watch which rooms, how each room's brightness follows the
sun, how its colour palettes bind to render groups, which plates control it, and how daylight
enters. The runtime reads it and the tuning UI writes it; a hand-authored model and a UI-authored
one are the same artifact.

Three properties are enforced here rather than hoped for:

* **Strict.** An unknown key anywhere is an error, so nothing a home declares can be silently
  dropped, which is what makes the lossless round-trip test meaningful.
* **Referential.** Every cross-reference (material, calibration, curve, transport, sensor, scene
  group, palette, lux pool, ambient scope) must resolve, and the set of errors is reported at
  once rather than one exception at a time.
* **Capability-typed.** The engine branches on ``Material.capabilities``; a device model name is
  carried only as a label. Adapter addresses are opaque strings the core never interprets.

What is not here is state: multipliers, factors, colour overrides, intent caches, holds, the
learned parameters. Those are runtime or learned state and live elsewhere by design.

Rooms may be grouped into **spaces**: a composite of rooms that behaves as one place for
vacancy (one shared timer that any member's presence cancels, whose expiry sweeps every
member), for the whole-space gestures (all on, all off, the house-wide dial) and for the wake
after sleep. A room in a space renders on its own presence for the occupied tier and on the
space's for staying lit: it keeps its lights on at the vacant tier while any other member is
occupied. A home with no such composite declares no spaces and every room stands alone; the
reference installation's open-plan "main space" is one instance, not a built-in concept.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal, cast, get_args

import yaml

from pascl.model.geometry import (
    CLIP_RULES,
    SAME_MODEL_FAIL,
    ClipRule,
    polygon_problems,
    vertex_deviation,
)

SCHEMA_VERSION = 1

CURVE_ANCHORS: tuple[str, ...] = (
    "midnight",
    "pre_dawn_mid",
    "civil_dawn",
    "am_mid",
    "noon",
    "pm_mid",
    "civil_dusk",
    "pre_midnight_mid",
)

TransportKind = Literal["z2m", "zha", "ha_bridged", "leap", "esphome", "homekit", "matter"]
Attribution = Literal["attributed", "inferred", "none"]
MaterialKind = Literal["bulb", "strip", "load"]
Capability = Literal["dim", "cct", "xy"]
Modality = Literal[
    "inovelli_mmwave",
    "fp2_zone",
    "s1_zone",
    "epp_zone",
    "pir",
    "tuya_24g",
    "thermal",
    "fsr",
    "door",
    "lux",
    "generic",
]
Role = Literal["accent", "highlight"]
GroupTier = Literal["room", "type", "named", "phantom"]
Fusion = Literal["or", "or_pir_latched"]
VacancyScope = Literal["room", "space"]
WakeRelight = Literal["all_dark", "occupied_dark"]
RoomPattern = Literal["A", "C"]
SleepBehaviour = Literal["suppress", "dim", "none"]
SurfaceKind = Literal["inovelli_vzm32", "hue_tap_dial", "lutron_pico", "generic"]
PresenceZone = Literal["room", "switch"]
LedTier = Literal["A", "B", "C", "D"]
PaletteKind = Literal["solar_cct", "solar_keyframes", "solar_cct_offset", "static"]

XY = tuple[float, float]

KELVIN_MIN = 1000
KELVIN_MAX = 20000
"""The widest colour-temperature range any transport declares (Zigbee's colorTemperature
attribute spans 1000-20000 K); a measured range must sit inside it."""

CREDIBLE_KELVIN = (1500, 10000)
"""The colour temperatures a white-tunable emitter can plausibly reach: no LED sold goes below
~1700 K or above ~9000 K. A range beyond this is a placeholder (a bulb declaring the whole
Zigbee attribute range as its physical limits), not a measurement."""

CtSource = Literal["probed", "observed", "declared", "inherited"]
CT_SOURCES: tuple[CtSource, ...] = get_args(CtSource)
CtTier = Literal["probed", "observed", "declared"]
CT_TRIED_TIERS: tuple[CtTier, ...] = get_args(CtTier)
"""The tiers a fixture's own evidence can decline (inheritance and the author's word are not
tried on the device); all three declined with proof is what makes an end unobservable."""
CtEnd = Literal["floor", "ceiling"]
CtVerdict = Literal["unobservable", "inconclusive"]
CT_VERDICTS: tuple[CtVerdict, ...] = get_args(CtVerdict)
"""How a tier declined an end: ``unobservable``, the evidence proves this tier can never
bound the end on this device (the attribute stores a command unclipped, the declaration is
the attribute's whole span, the report channel repeated a command past any emitter's
floor); ``inconclusive``, the tier did not get an answer it could judge (no reply, too few
pairs, an end the commands never reached), so trying again may still speak."""


@dataclass(frozen=True)
class CtDecline:
    """One tier's decline of one end of a fixture's colour-temperature range, with the
    reason: the proof that stays beside an unbounded end (DERIVED_PARAMETERS P2, P10)."""

    tier: CtTier
    end: CtEnd
    verdict: CtVerdict
    reason: str


"""Where one end of a fixture's colour-temperature range came from, in the order the chain
tries them (docs/gamut.md section 11): ``probed``, the device was commanded past the end and
its attribute clipped, answering the limit; ``observed``, the device reported the clip in use
(from commanded/reported pairs, ``pascl.estimator.ct_observed``); ``declared``, the device's
own credible declaration; ``inherited``, copied from a measured fixture of the same model
label. An end with no source is UNBOUNDED: no tier could speak, the value carried is the
attribute's span end (``KELVIN_MIN`` / ``KELVIN_MAX``), and :func:`fixture_cct_range` falls
through to the material's declaration (the author's word, the last tier) or to nothing."""


class ModelError(ValueError):
    """The model could not be loaded or does not validate. ``errors`` lists every problem."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


# ---------------------------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Location:
    latitude: float
    longitude: float
    elevation_m: float
    tz: str


@dataclass(frozen=True)
class Solar:
    solstice_blend_frac: float = 0.775
    tick_s: int = 45


@dataclass(frozen=True)
class Transport:
    kind: TransportKind
    attribution: Attribution
    endpoint: str | None = None
    base_topic: str | None = None
    groupcast: bool = True
    stagger_ms: int = 150


@dataclass(frozen=True)
class Calibration:
    """Colour anchor family: where warm, cool and night white sit for one fixture family."""

    warm_xy: XY
    cool_xy: XY
    night_xy: XY


@dataclass(frozen=True)
class Material:
    kind: MaterialKind
    capabilities: frozenset[Capability]
    cct_range_k: tuple[int, int] | None = None
    calibration: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class Sensor:
    modality: Modality
    transport: str
    device: str | None = None
    zone: str | None = None
    address: str | None = None


@dataclass(frozen=True)
class Gamut:
    """A fixture's measured colour gamut: the convex polygon of chromaticities it can reach,
    counter-clockwise in CIE xy, and the rule by which the device clips to it. Measured on
    the physical device (``pascl.estimator.gamut``), never taken from a model table, and bound
    to the transport's identity for that device so a replacement is measured afresh. The
    comparators judge the device against the intent clipped onto this polygon; a fixture
    without one resolves to the identity.

    A polygon may also be *inherited*: copied from a measured fixture whose material carries
    the same model label, so a new fixture is exact from its first minute. That is data with
    provenance (``inherited_from`` names the source), not brand knowledge: the engine never
    learns what the label means, only that the author called two fixtures the same hardware.
    An inherited polygon is a seed the runtime confirms by measuring the fixture itself."""

    vertices: tuple[XY, ...]
    bound_to: str | None = None
    """The transport's identity for the measured device (opaque to the core)."""
    measured: str | None = None
    """When it was measured, ISO 8601, opaque to the core."""
    model_error: float | None = None
    """Worst per-axis gap between the device's own answers and ``clip_rule`` applied to the
    polygon, over the probes that test the rule; None when unverified."""
    clip_rule: ClipRule = "closest"
    """How the device clips an unreachable command; see :data:`CLIP_RULES`."""
    inherited_from: str | None = None
    """The measured fixture this polygon was copied from; None when measured on this one."""
    firmware: str | None = None
    """The device's firmware at measurement, as the transport reported it. Information for a
    reader comparing units, never a staleness trigger: a gamut does not drift."""
    ct_range_k: tuple[int, int] | None = None
    """The colour temperatures the device can actually show, kelvin, (floor, ceiling), each end
    from the first tier of the chain that could speak for it (``ct_sources``). A transport's
    declared range is what the device advertises, and the reference installation found 22
    bulbs advertising 1000-20000 K over a physical ~2000-6500 K; a comparator judging the
    device against a declared range repaints such a bulb forever at night. None when no tier
    spoke for either end, in which case :func:`fixture_cct_range` falls back to the material's
    declaration; an end whose source is None is unbounded and carries the span end."""
    ct_sources: tuple[CtSource | None, CtSource | None] = (None, None)
    """The tier that produced each end of ``ct_range_k`` (floor, ceiling); None for an end no
    tier could speak for. (None, None) when there is no range."""
    ct_declined: tuple[CtDecline, ...] = ()
    """The tiers tried on this fixture that declined an end, each with its verdict and reason:
    the proof that stays beside an unbounded end. An end with no source that every tier in
    ``CT_TRIED_TIERS`` declined as ``unobservable`` is UNOBSERVABLE on this transport
    (:func:`unobservable`), the one state in which the model may ask the author for the
    range; an end with no source and a tier missing or ``inconclusive`` here is merely not
    known yet. The two must never read alike (DERIVED_PARAMETERS P2)."""


@dataclass(frozen=True)
class Fixture:
    material: str
    curve: str
    role: Role
    transport: str
    address: str | None = None
    scene_group: str | None = None
    palette_stride: int = 0
    gamut: Gamut | None = None


@dataclass(frozen=True)
class Group:
    members: tuple[str, ...]
    tier: GroupTier
    transport: str
    address: str | None = None


@dataclass(frozen=True)
class Presence:
    sources: tuple[str, ...]
    fusion: Fusion = "or"
    pir: str | None = None
    coalesce_ms: int | None = None


@dataclass(frozen=True)
class Vacancy:
    """How a room's lights go out after it empties: on its own timer (``room``, with its own
    ``seconds``) or on its space's shared timer (``space``, whose duration the space declares)."""

    scope: VacancyScope = "room"
    seconds: int | None = None


@dataclass(frozen=True)
class Space:
    """A composite of rooms that empties, wakes and takes whole-place gestures as one.

    Membership is declared by each room's ``space``; presence is the OR of the members' presence
    composites plus ``extra_presence``. ``vacancy_s`` is the shared timer any member's presence
    cancels and whose expiry sweeps every member that uses it. ``wake_relight`` says what a wake
    from sleep relights: every dark member (the open-plan default) or only occupied dark ones,
    which is also what a room outside any space does.
    """

    vacancy_s: int
    extra_presence: tuple[str, ...] = ()
    wake_relight: WakeRelight = "all_dark"


@dataclass(frozen=True)
class Curve:
    occupied: dict[str, int]
    vacant: dict[str, int]


@dataclass(frozen=True)
class SceneGroup:
    fixtures: tuple[str, ...]
    default: str
    parents: tuple[str, ...] = ()


@dataclass(frozen=True)
class ControlSurface:
    kind: SurfaceKind
    transport: str
    address: str | None = None
    presence_zone: PresenceZone = "room"
    phantom_group: str | None = None
    led_tier: LedTier = "D"
    smart_bulb_binding: bool = False


@dataclass(frozen=True)
class Room:
    vacancy: Vacancy
    presence: Presence
    fixtures: dict[str, Fixture]
    curves: dict[str, Curve]
    groups: dict[str, Group] = field(default_factory=dict)
    scene_groups: dict[str, SceneGroup] = field(default_factory=dict)
    control_surfaces: dict[str, ControlSurface] = field(default_factory=dict)
    switch_zone: Presence | None = None
    space: str | None = None
    """The composite this room belongs to, if any; see :class:`Space`."""
    pattern: RoomPattern = "A"
    palette_base: int = 0
    ambient_scope: str | None = None
    sleep: SleepBehaviour = "suppress"


@dataclass(frozen=True)
class Zone:
    room: str
    sources: tuple[str, ...]
    fixtures: tuple[str, ...]
    fusion: Fusion = "or"


@dataclass(frozen=True)
class Lux:
    pools: dict[str, tuple[str, ...]] = field(default_factory=dict)
    borrow: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AmbientScope:
    rooms: tuple[str, ...]
    lux_pool: str
    drive: bool = True
    fixtures: tuple[str, ...] | None = None
    slaved_to: str | None = None


@dataclass(frozen=True)
class AmbientNormalization:
    weather_data: bool = True
    cap: float = 1.20
    deadband: float = 0.15
    dose_k: float = 0.6
    tau_s: float = 3000.0
    elevation_gate_deg: float = 12.0


@dataclass(frozen=True)
class Palette:
    kind: PaletteKind
    bulbs: tuple[XY, ...] = ()
    strips: tuple[XY, ...] = ()
    offset_k: int | None = None


@dataclass(frozen=True)
class SceneTick:
    linger_s: int = 30
    transition_s: float = 30.0
    fast_transition_s: float = 0.2
    offset_wrap: int = 6400


@dataclass(frozen=True)
class Scenes:
    palettes: dict[str, Palette]
    parents: dict[str, tuple[str, ...]] = field(default_factory=dict)
    tick: SceneTick = field(default_factory=SceneTick)


@dataclass(frozen=True)
class HomeModel:
    version: int
    id: str
    location: Location
    transports: dict[str, Transport]
    materials: dict[str, Material]
    sensors: dict[str, Sensor]
    rooms: dict[str, Room]
    scenes: Scenes
    solar: Solar = field(default_factory=Solar)
    calibrations: dict[str, Calibration] = field(default_factory=dict)
    spaces: dict[str, Space] = field(default_factory=dict)
    zones: dict[str, Zone] = field(default_factory=dict)
    lux: Lux = field(default_factory=Lux)
    ambient_scopes: dict[str, AmbientScope] = field(default_factory=dict)
    ambient_normalization: AmbientNormalization = field(default_factory=AmbientNormalization)


# ---------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------


class _Reader:
    """Walks a plain mapping into dataclasses, collecting every problem instead of stopping."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def fail(self, ctx: str, msg: str) -> None:
        self.errors.append(f"{ctx}: {msg}")

    def mapping(self, ctx: str, value: object, allowed: Iterable[str]) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            self.fail(ctx, f"expected a mapping, got {type(value).__name__}")
            return {}
        extra = sorted(set(value) - set(allowed))
        if extra:
            self.fail(ctx, f"unknown key(s) {extra}")
        return {k: v for k, v in value.items() if k not in extra}

    def req(self, ctx: str, d: dict[str, Any], key: str) -> Any:
        if key not in d:
            self.fail(ctx, f"missing required key '{key}'")
            return None
        return d[key]

    def literal(self, ctx: str, value: object, choices: tuple[Any, ...], default: str) -> Any:
        """A member of a Literal type. Returns Any on purpose: the caller assigns it to a field
        typed with that Literal, and the membership check here is what makes that sound."""
        if value is None:
            return default
        if not isinstance(value, str) or value not in choices:
            self.fail(ctx, f"expected one of {list(choices)}, got {value!r}")
            return default
        return value

    def number(self, ctx: str, value: object, default: float = 0.0) -> float:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int | float):
            self.fail(ctx, f"expected a number, got {value!r}")
            return default
        return float(value)

    def integer(self, ctx: str, value: object, default: int = 0) -> int:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            self.fail(ctx, f"expected an integer, got {value!r}")
            return default
        return value

    def boolean(self, ctx: str, value: object, default: bool) -> bool:
        if value is None:
            return default
        if not isinstance(value, bool):
            self.fail(ctx, f"expected true/false, got {value!r}")
            return default
        return value

    def string(self, ctx: str, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            self.fail(ctx, f"expected a string, got {value!r}")
            return None
        return value

    def strings(self, ctx: str, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            self.fail(ctx, f"expected a list of strings, got {value!r}")
            return ()
        return tuple(value)

    def xy(self, ctx: str, value: object) -> XY:
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not all(isinstance(v, int | float) and not isinstance(v, bool) for v in value)
        ):
            self.fail(ctx, f"expected [x, y], got {value!r}")
            return (0.0, 0.0)
        return (float(value[0]), float(value[1]))

    def xys(self, ctx: str, value: object) -> tuple[XY, ...]:
        if value is None:
            return ()
        if not isinstance(value, list):
            self.fail(ctx, f"expected a list of [x, y] pairs, got {value!r}")
            return ()
        return tuple(self.xy(f"{ctx}[{i}]", v) for i, v in enumerate(value))

    def named(self, ctx: str, value: object, build: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            self.fail(ctx, f"expected a mapping of ids, got {type(value).__name__}")
            return {}
        out: dict[str, Any] = {}
        for key, sub in value.items():
            if not isinstance(key, str) or not key:
                self.fail(ctx, f"ids must be non-empty strings, got {key!r}")
                continue
            out[key] = build(f"{ctx}.{key}", sub)
        return out


_LOCATION = ("latitude", "longitude", "elevation_m", "tz")
_SOLAR = ("solstice_blend_frac", "tick_s")
_TRANSPORT = ("kind", "attribution", "endpoint", "base_topic", "groupcast", "stagger_ms")
_CALIBRATION = ("warm_xy", "cool_xy", "night_xy")
_MATERIAL = ("kind", "capabilities", "cct_range_k", "calibration", "model")
_SENSOR = ("modality", "transport", "device", "zone", "address")
_FIXTURE = (
    "material",
    "curve",
    "role",
    "transport",
    "address",
    "scene_group",
    "palette_stride",
    "gamut",
)
_GAMUT = (
    "vertices",
    "bound_to",
    "measured",
    "model_error",
    "clip_rule",
    "inherited_from",
    "firmware",
    "ct_range_k",
    "ct_sources",
    "ct_declined",
)
_GROUP = ("members", "tier", "transport", "address")
_PRESENCE = ("sources", "fusion", "pir", "coalesce_ms")
_VACANCY = ("scope", "seconds")
_SPACE = ("vacancy_s", "extra_presence", "wake_relight")
_CURVE = ("occupied", "vacant")
_SCENE_GROUP = ("fixtures", "default", "parents")
_SURFACE = (
    "kind",
    "transport",
    "address",
    "presence_zone",
    "phantom_group",
    "led_tier",
    "smart_bulb_binding",
)
_ROOM = (
    "space",
    "vacancy",
    "presence",
    "fixtures",
    "curves",
    "groups",
    "scene_groups",
    "control_surfaces",
    "switch_zone",
    "pattern",
    "palette_base",
    "ambient_scope",
    "sleep",
)
_ZONE = ("room", "sources", "fixtures", "fusion")
_LUX = ("pools", "borrow")
_AMBIENT_SCOPE = ("rooms", "lux_pool", "drive", "fixtures", "slaved_to")
_AMBIENT = ("weather_data", "cap", "deadband", "dose_k", "tau_s", "elevation_gate_deg")
_PALETTE = ("kind", "bulbs", "strips", "offset_k")
_TICK = ("linger_s", "transition_s", "fast_transition_s", "offset_wrap")
_SCENES = ("palettes", "parents", "tick")
_HOME = (
    "version",
    "id",
    "location",
    "solar",
    "transports",
    "calibrations",
    "materials",
    "sensors",
    "spaces",
    "rooms",
    "zones",
    "lux",
    "ambient_scopes",
    "ambient_normalization",
    "scenes",
)


def _read_location(r: _Reader, ctx: str, v: object) -> Location:
    d = r.mapping(ctx, v, _LOCATION)
    return Location(
        latitude=r.number(f"{ctx}.latitude", r.req(ctx, d, "latitude")),
        longitude=r.number(f"{ctx}.longitude", r.req(ctx, d, "longitude")),
        elevation_m=r.number(f"{ctx}.elevation_m", d.get("elevation_m"), 0.0),
        tz=r.string(f"{ctx}.tz", r.req(ctx, d, "tz")) or "UTC",
    )


def _read_solar(r: _Reader, ctx: str, v: object) -> Solar:
    d = r.mapping(ctx, v, _SOLAR)
    return Solar(
        solstice_blend_frac=r.number(
            f"{ctx}.solstice_blend_frac", d.get("solstice_blend_frac"), 0.775
        ),
        tick_s=r.integer(f"{ctx}.tick_s", d.get("tick_s"), 45),
    )


def _read_transport(r: _Reader, ctx: str, v: object) -> Transport:
    d = r.mapping(ctx, v, _TRANSPORT)
    return Transport(
        kind=r.literal(f"{ctx}.kind", r.req(ctx, d, "kind"), get_args(TransportKind), "z2m"),
        attribution=r.literal(
            f"{ctx}.attribution",
            r.req(ctx, d, "attribution"),
            get_args(Attribution),
            "none",
        ),
        endpoint=r.string(f"{ctx}.endpoint", d.get("endpoint")),
        base_topic=r.string(f"{ctx}.base_topic", d.get("base_topic")),
        groupcast=r.boolean(f"{ctx}.groupcast", d.get("groupcast"), True),
        stagger_ms=r.integer(f"{ctx}.stagger_ms", d.get("stagger_ms"), 150),
    )


def _read_calibration(r: _Reader, ctx: str, v: object) -> Calibration:
    d = r.mapping(ctx, v, _CALIBRATION)
    return Calibration(
        warm_xy=r.xy(f"{ctx}.warm_xy", r.req(ctx, d, "warm_xy")),
        cool_xy=r.xy(f"{ctx}.cool_xy", r.req(ctx, d, "cool_xy")),
        night_xy=r.xy(f"{ctx}.night_xy", r.req(ctx, d, "night_xy")),
    )


def _read_material(r: _Reader, ctx: str, v: object) -> Material:
    d = r.mapping(ctx, v, _MATERIAL)
    caps = r.strings(f"{ctx}.capabilities", r.req(ctx, d, "capabilities"))
    bad = [c for c in caps if c not in get_args(Capability)]
    if bad:
        r.fail(f"{ctx}.capabilities", f"unknown capability {bad}")
    return Material(
        kind=r.literal(f"{ctx}.kind", r.req(ctx, d, "kind"), get_args(MaterialKind), "bulb"),
        capabilities=cast(
            "frozenset[Capability]", frozenset(c for c in caps if c in get_args(Capability))
        ),
        cct_range_k=_read_kelvin_range(r, f"{ctx}.cct_range_k", d.get("cct_range_k")),
        calibration=r.string(f"{ctx}.calibration", d.get("calibration")),
        model=r.string(f"{ctx}.model", d.get("model")),
    )


def _read_kelvin_range(r: _Reader, ctx: str, rng: object) -> tuple[int, int] | None:
    """``[min_k, max_k]`` as an int pair; None when absent."""
    if rng is None:
        return None
    if (
        isinstance(rng, list)
        and len(rng) == 2
        and all(isinstance(x, int) and not isinstance(x, bool) for x in rng)
    ):
        return (rng[0], rng[1])
    r.fail(ctx, f"expected [min_k, max_k], got {rng!r}")
    return None


def _read_ct_sources(r: _Reader, ctx: str, v: object) -> tuple[CtSource | None, CtSource | None]:
    """``[floor_source, ceiling_source]``, each a member of ``CT_SOURCES`` or null; (None, None)
    when absent."""
    if v is None:
        return (None, None)
    if isinstance(v, list) and len(v) == 2:
        out: list[CtSource | None] = []
        for i, s in enumerate(v):
            out.append(None if s is None else r.literal(f"{ctx}[{i}]", s, CT_SOURCES, "probed"))
        return (out[0], out[1])
    r.fail(ctx, f"expected [floor_source, ceiling_source], got {v!r}")
    return (None, None)


def _read_ct_declined(r: _Reader, ctx: str, v: object) -> tuple[CtDecline, ...]:
    """``[[tier, end, verdict, reason], ...]``; () when absent."""
    if v is None:
        return ()
    if not isinstance(v, list):
        r.fail(ctx, f"expected a list of [tier, end, verdict, reason] rows, got {v!r}")
        return ()
    out: list[CtDecline] = []
    for i, item in enumerate(v):
        if not (isinstance(item, list) and len(item) == 4 and isinstance(item[3], str) and item[3]):
            r.fail(f"{ctx}[{i}]", f"expected [tier, end, verdict, reason], got {item!r}")
            continue
        out.append(
            CtDecline(
                tier=r.literal(f"{ctx}[{i}][0]", item[0], CT_TRIED_TIERS, "probed"),
                end=r.literal(f"{ctx}[{i}][1]", item[1], ("floor", "ceiling"), "floor"),
                verdict=r.literal(f"{ctx}[{i}][2]", item[2], CT_VERDICTS, "inconclusive"),
                reason=item[3],
            )
        )
    return tuple(out)


def _read_sensor(r: _Reader, ctx: str, v: object) -> Sensor:
    d = r.mapping(ctx, v, _SENSOR)
    return Sensor(
        modality=r.literal(
            f"{ctx}.modality", r.req(ctx, d, "modality"), get_args(Modality), "generic"
        ),
        transport=r.string(f"{ctx}.transport", r.req(ctx, d, "transport")) or "",
        device=r.string(f"{ctx}.device", d.get("device")),
        zone=r.string(f"{ctx}.zone", d.get("zone")),
        address=r.string(f"{ctx}.address", d.get("address")),
    )


def _read_gamut(r: _Reader, ctx: str, v: object) -> Gamut:
    d = r.mapping(ctx, v, _GAMUT)
    err = d.get("model_error")
    model_error: float | None = None
    if err is not None:
        model_error = r.number(f"{ctx}.model_error", err)
    return Gamut(
        vertices=r.xys(f"{ctx}.vertices", r.req(ctx, d, "vertices")),
        bound_to=r.string(f"{ctx}.bound_to", d.get("bound_to")),
        measured=r.string(f"{ctx}.measured", d.get("measured")),
        model_error=model_error,
        clip_rule=r.literal(f"{ctx}.clip_rule", d.get("clip_rule"), CLIP_RULES, "closest"),
        inherited_from=r.string(f"{ctx}.inherited_from", d.get("inherited_from")),
        firmware=r.string(f"{ctx}.firmware", d.get("firmware")),
        ct_range_k=_read_kelvin_range(r, f"{ctx}.ct_range_k", d.get("ct_range_k")),
        ct_sources=_read_ct_sources(r, f"{ctx}.ct_sources", d.get("ct_sources")),
        ct_declined=_read_ct_declined(r, f"{ctx}.ct_declined", d.get("ct_declined")),
    )


def _read_fixture(r: _Reader, ctx: str, v: object) -> Fixture:
    d = r.mapping(ctx, v, _FIXTURE)
    gamut = d.get("gamut")
    return Fixture(
        material=r.string(f"{ctx}.material", r.req(ctx, d, "material")) or "",
        curve=r.string(f"{ctx}.curve", r.req(ctx, d, "curve")) or "",
        role=r.literal(f"{ctx}.role", r.req(ctx, d, "role"), get_args(Role), "accent"),
        transport=r.string(f"{ctx}.transport", r.req(ctx, d, "transport")) or "",
        address=r.string(f"{ctx}.address", d.get("address")),
        scene_group=r.string(f"{ctx}.scene_group", d.get("scene_group")),
        palette_stride=r.integer(f"{ctx}.palette_stride", d.get("palette_stride"), 0),
        gamut=None if gamut is None else _read_gamut(r, f"{ctx}.gamut", gamut),
    )


def _read_group(r: _Reader, ctx: str, v: object) -> Group:
    d = r.mapping(ctx, v, _GROUP)
    return Group(
        members=r.strings(f"{ctx}.members", r.req(ctx, d, "members")),
        tier=r.literal(f"{ctx}.tier", r.req(ctx, d, "tier"), get_args(GroupTier), "named"),
        transport=r.string(f"{ctx}.transport", r.req(ctx, d, "transport")) or "",
        address=r.string(f"{ctx}.address", d.get("address")),
    )


def _read_presence(r: _Reader, ctx: str, v: object) -> Presence:
    d = r.mapping(ctx, v, _PRESENCE)
    coalesce = d.get("coalesce_ms")
    return Presence(
        sources=r.strings(f"{ctx}.sources", r.req(ctx, d, "sources")),
        fusion=r.literal(f"{ctx}.fusion", d.get("fusion"), get_args(Fusion), "or"),
        pir=r.string(f"{ctx}.pir", d.get("pir")),
        coalesce_ms=None if coalesce is None else r.integer(f"{ctx}.coalesce_ms", coalesce, 0),
    )


def _read_vacancy(r: _Reader, ctx: str, v: object) -> Vacancy:
    d = r.mapping(ctx, v, _VACANCY)
    seconds = d.get("seconds")
    return Vacancy(
        scope=r.literal(f"{ctx}.scope", d.get("scope"), get_args(VacancyScope), "room"),
        seconds=None if seconds is None else r.integer(f"{ctx}.seconds", seconds, 0),
    )


def _read_space(r: _Reader, ctx: str, v: object) -> Space:
    d = r.mapping(ctx, v, _SPACE)
    return Space(
        vacancy_s=r.integer(f"{ctx}.vacancy_s", r.req(ctx, d, "vacancy_s"), 0),
        extra_presence=r.strings(f"{ctx}.extra_presence", d.get("extra_presence")),
        wake_relight=r.literal(
            f"{ctx}.wake_relight", d.get("wake_relight"), get_args(WakeRelight), "all_dark"
        ),
    )


def _read_curve(r: _Reader, ctx: str, v: object) -> Curve:
    d = r.mapping(ctx, v, _CURVE)

    def table(key: str) -> dict[str, int]:
        t = r.mapping(f"{ctx}.{key}", r.req(ctx, d, key), CURVE_ANCHORS)
        missing = [a for a in CURVE_ANCHORS if a not in t]
        if missing:
            r.fail(f"{ctx}.{key}", f"missing anchor(s) {missing}")
        out: dict[str, int] = {}
        for a in CURVE_ANCHORS:
            if a in t:
                val = r.integer(f"{ctx}.{key}.{a}", t[a], 0)
                if not 0 <= val <= 255:
                    r.fail(f"{ctx}.{key}.{a}", f"brightness {val} outside 0..255")
                out[a] = val
        return out

    return Curve(occupied=table("occupied"), vacant=table("vacant"))


def _read_scene_group(r: _Reader, ctx: str, v: object) -> SceneGroup:
    d = r.mapping(ctx, v, _SCENE_GROUP)
    return SceneGroup(
        fixtures=r.strings(f"{ctx}.fixtures", r.req(ctx, d, "fixtures")),
        default=r.string(f"{ctx}.default", r.req(ctx, d, "default")) or "",
        parents=r.strings(f"{ctx}.parents", d.get("parents")),
    )


def _read_surface(r: _Reader, ctx: str, v: object) -> ControlSurface:
    d = r.mapping(ctx, v, _SURFACE)
    return ControlSurface(
        kind=r.literal(f"{ctx}.kind", r.req(ctx, d, "kind"), get_args(SurfaceKind), "generic"),
        transport=r.string(f"{ctx}.transport", r.req(ctx, d, "transport")) or "",
        address=r.string(f"{ctx}.address", d.get("address")),
        presence_zone=r.literal(
            f"{ctx}.presence_zone", d.get("presence_zone"), get_args(PresenceZone), "room"
        ),
        phantom_group=r.string(f"{ctx}.phantom_group", d.get("phantom_group")),
        led_tier=r.literal(f"{ctx}.led_tier", d.get("led_tier"), get_args(LedTier), "D"),
        smart_bulb_binding=r.boolean(
            f"{ctx}.smart_bulb_binding", d.get("smart_bulb_binding"), False
        ),
    )


def _read_room(r: _Reader, ctx: str, v: object) -> Room:
    d = r.mapping(ctx, v, _ROOM)
    switch_zone = d.get("switch_zone")
    vacancy = d.get("vacancy")
    return Room(
        space=r.string(f"{ctx}.space", d.get("space")),
        vacancy=Vacancy() if vacancy is None else _read_vacancy(r, f"{ctx}.vacancy", vacancy),
        presence=_read_presence(r, f"{ctx}.presence", r.req(ctx, d, "presence")),
        fixtures=r.named(
            f"{ctx}.fixtures", r.req(ctx, d, "fixtures"), lambda c, s: _read_fixture(r, c, s)
        ),
        curves=r.named(f"{ctx}.curves", r.req(ctx, d, "curves"), lambda c, s: _read_curve(r, c, s)),
        groups=r.named(f"{ctx}.groups", d.get("groups"), lambda c, s: _read_group(r, c, s)),
        scene_groups=r.named(
            f"{ctx}.scene_groups", d.get("scene_groups"), lambda c, s: _read_scene_group(r, c, s)
        ),
        control_surfaces=r.named(
            f"{ctx}.control_surfaces",
            d.get("control_surfaces"),
            lambda c, s: _read_surface(r, c, s),
        ),
        switch_zone=None
        if switch_zone is None
        else _read_presence(r, f"{ctx}.switch_zone", switch_zone),
        pattern=r.literal(f"{ctx}.pattern", d.get("pattern"), get_args(RoomPattern), "A"),
        palette_base=r.integer(f"{ctx}.palette_base", d.get("palette_base"), 0),
        ambient_scope=r.string(f"{ctx}.ambient_scope", d.get("ambient_scope")),
        sleep=r.literal(f"{ctx}.sleep", d.get("sleep"), get_args(SleepBehaviour), "suppress"),
    )


def _read_zone(r: _Reader, ctx: str, v: object) -> Zone:
    d = r.mapping(ctx, v, _ZONE)
    return Zone(
        room=r.string(f"{ctx}.room", r.req(ctx, d, "room")) or "",
        sources=r.strings(f"{ctx}.sources", r.req(ctx, d, "sources")),
        fixtures=r.strings(f"{ctx}.fixtures", r.req(ctx, d, "fixtures")),
        fusion=r.literal(f"{ctx}.fusion", d.get("fusion"), get_args(Fusion), "or"),
    )


def _read_lux(r: _Reader, ctx: str, v: object) -> Lux:
    d = r.mapping(ctx, v, _LUX)
    pools = r.named(f"{ctx}.pools", d.get("pools"), lambda c, s: r.strings(c, s))
    borrow_raw = r.named(f"{ctx}.borrow", d.get("borrow"), lambda c, s: r.string(c, s) or "")
    return Lux(pools=pools, borrow=borrow_raw)


def _read_ambient_scope(r: _Reader, ctx: str, v: object) -> AmbientScope:
    d = r.mapping(ctx, v, _AMBIENT_SCOPE)
    fixtures = d.get("fixtures")
    return AmbientScope(
        rooms=r.strings(f"{ctx}.rooms", r.req(ctx, d, "rooms")),
        lux_pool=r.string(f"{ctx}.lux_pool", r.req(ctx, d, "lux_pool")) or "",
        drive=r.boolean(f"{ctx}.drive", d.get("drive"), True),
        fixtures=None if fixtures is None else r.strings(f"{ctx}.fixtures", fixtures),
        slaved_to=r.string(f"{ctx}.slaved_to", d.get("slaved_to")),
    )


def _read_ambient(r: _Reader, ctx: str, v: object) -> AmbientNormalization:
    d = r.mapping(ctx, v, _AMBIENT)
    return AmbientNormalization(
        weather_data=r.boolean(f"{ctx}.weather_data", d.get("weather_data"), True),
        cap=r.number(f"{ctx}.cap", d.get("cap"), 1.20),
        deadband=r.number(f"{ctx}.deadband", d.get("deadband"), 0.15),
        dose_k=r.number(f"{ctx}.dose_k", d.get("dose_k"), 0.6),
        tau_s=r.number(f"{ctx}.tau_s", d.get("tau_s"), 3000.0),
        elevation_gate_deg=r.number(f"{ctx}.elevation_gate_deg", d.get("elevation_gate_deg"), 12.0),
    )


def _read_palette(r: _Reader, ctx: str, v: object) -> Palette:
    d = r.mapping(ctx, v, _PALETTE)
    offset = d.get("offset_k")
    return Palette(
        kind=r.literal(f"{ctx}.kind", r.req(ctx, d, "kind"), get_args(PaletteKind), "static"),
        bulbs=r.xys(f"{ctx}.bulbs", d.get("bulbs")),
        strips=r.xys(f"{ctx}.strips", d.get("strips")),
        offset_k=None if offset is None else r.integer(f"{ctx}.offset_k", offset, 0),
    )


def _read_tick(r: _Reader, ctx: str, v: object) -> SceneTick:
    d = r.mapping(ctx, v, _TICK)
    return SceneTick(
        linger_s=r.integer(f"{ctx}.linger_s", d.get("linger_s"), 30),
        transition_s=r.number(f"{ctx}.transition_s", d.get("transition_s"), 30.0),
        fast_transition_s=r.number(f"{ctx}.fast_transition_s", d.get("fast_transition_s"), 0.2),
        offset_wrap=r.integer(f"{ctx}.offset_wrap", d.get("offset_wrap"), 6400),
    )


def _read_scenes(r: _Reader, ctx: str, v: object) -> Scenes:
    d = r.mapping(ctx, v, _SCENES)
    tick = d.get("tick")
    return Scenes(
        palettes=r.named(
            f"{ctx}.palettes", r.req(ctx, d, "palettes"), lambda c, s: _read_palette(r, c, s)
        ),
        parents=r.named(f"{ctx}.parents", d.get("parents"), lambda c, s: r.strings(c, s)),
        tick=SceneTick() if tick is None else _read_tick(r, f"{ctx}.tick", tick),
    )


def from_dict(data: object) -> HomeModel:
    """Build a model from a plain mapping (what YAML or JSON parses to). Raises ModelError with
    every structural problem found; referential problems are reported by :func:`validate`."""
    r = _Reader()
    ctx = "home_model"
    root = r.mapping(ctx, data, ("home_model",))
    d = r.mapping(ctx, r.req(ctx, root, "home_model"), _HOME)
    version = r.integer(f"{ctx}.version", r.req(ctx, d, "version"), -1)
    if version != SCHEMA_VERSION:
        r.fail(
            f"{ctx}.version", f"this engine reads schema version {SCHEMA_VERSION}, got {version}"
        )
    solar = d.get("solar")
    lux = d.get("lux")
    ambient = d.get("ambient_normalization")
    model = HomeModel(
        version=version,
        id=r.string(f"{ctx}.id", r.req(ctx, d, "id")) or "",
        location=_read_location(r, f"{ctx}.location", r.req(ctx, d, "location")),
        solar=Solar() if solar is None else _read_solar(r, f"{ctx}.solar", solar),
        transports=r.named(
            f"{ctx}.transports", r.req(ctx, d, "transports"), lambda c, s: _read_transport(r, c, s)
        ),
        calibrations=r.named(
            f"{ctx}.calibrations", d.get("calibrations"), lambda c, s: _read_calibration(r, c, s)
        ),
        materials=r.named(
            f"{ctx}.materials", r.req(ctx, d, "materials"), lambda c, s: _read_material(r, c, s)
        ),
        sensors=r.named(
            f"{ctx}.sensors", r.req(ctx, d, "sensors"), lambda c, s: _read_sensor(r, c, s)
        ),
        spaces=r.named(f"{ctx}.spaces", d.get("spaces"), lambda c, s: _read_space(r, c, s)),
        rooms=r.named(f"{ctx}.rooms", r.req(ctx, d, "rooms"), lambda c, s: _read_room(r, c, s)),
        zones=r.named(f"{ctx}.zones", d.get("zones"), lambda c, s: _read_zone(r, c, s)),
        lux=Lux() if lux is None else _read_lux(r, f"{ctx}.lux", lux),
        ambient_scopes=r.named(
            f"{ctx}.ambient_scopes",
            d.get("ambient_scopes"),
            lambda c, s: _read_ambient_scope(r, c, s),
        ),
        ambient_normalization=(
            AmbientNormalization()
            if ambient is None
            else _read_ambient(r, f"{ctx}.ambient_normalization", ambient)
        ),
        scenes=_read_scenes(r, f"{ctx}.scenes", r.req(ctx, d, "scenes")),
    )
    if r.errors:
        raise ModelError(r.errors)
    return model


def loads(text: str) -> HomeModel:
    return from_dict(yaml.safe_load(text))


# ---------------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------------


def validate(m: HomeModel) -> list[str]:
    """Every referential and semantic problem in the model, empty when it is sound."""
    errs: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            errs.append(msg)

    for tid, t in m.transports.items():
        if t.kind == "z2m":
            check(bool(t.base_topic), f"transports.{tid}: a z2m transport needs base_topic")
    for mid, mat in m.materials.items():
        check(
            mat.calibration is None or mat.calibration in m.calibrations,
            f"materials.{mid}: calibration '{mat.calibration}' is not declared",
        )
        check(
            "dim" in mat.capabilities or mat.kind == "load",
            f"materials.{mid}: a fixture material must be dimmable",
        )
        check(
            "cct" not in mat.capabilities or mat.cct_range_k is not None,
            f"materials.{mid}: a cct-capable material needs cct_range_k",
        )
        check(
            mat.cct_range_k is None or mat.cct_range_k[0] < mat.cct_range_k[1],
            f"materials.{mid}: cct_range_k must be [min_k, max_k] with min below max",
        )
    for sid, s in m.sensors.items():
        check(
            s.transport in m.transports, f"sensors.{sid}: transport '{s.transport}' is not declared"
        )

    scene_group_owner: dict[str, str] = {}
    lux_sensor_ids = {sid for sid, s in m.sensors.items() if s.modality == "lux"}
    for rid, room in m.rooms.items():
        ctx = f"rooms.{rid}"
        check(bool(room.fixtures), f"{ctx}: a room needs at least one fixture")
        for fid, fx in room.fixtures.items():
            fctx = f"{ctx}.fixtures.{fid}"
            check(fx.material in m.materials, f"{fctx}: material '{fx.material}' is not declared")
            check(
                fx.curve in room.curves, f"{fctx}: curve '{fx.curve}' is not in this room's curves"
            )
            check(
                fx.transport in m.transports, f"{fctx}: transport '{fx.transport}' is not declared"
            )
            check(fx.palette_stride >= 0, f"{fctx}: palette_stride must be non-negative")
            check(
                fx.scene_group is None or fx.scene_group in room.scene_groups,
                f"{fctx}: scene_group '{fx.scene_group}' is not in this room",
            )
            if fx.gamut is not None:
                fx_mat = m.materials.get(fx.material)
                check(
                    fx_mat is None or "xy" in fx_mat.capabilities,
                    f"{fctx}: a gamut on a material without the xy capability",
                )
                for problem in polygon_problems(fx.gamut.vertices):
                    check(False, f"{fctx}.gamut: {problem}")
                check(
                    fx.gamut.model_error is None or fx.gamut.model_error >= 0,
                    f"{fctx}.gamut: model_error must be non-negative",
                )
                check(
                    fx.gamut.clip_rule != "rgb_clamp" or len(fx.gamut.vertices) == 3,
                    f"{fctx}.gamut: rgb_clamp is a rule for a three-primary (triangle) gamut",
                )
                ct = fx.gamut.ct_range_k
                ct_src = fx.gamut.ct_sources
                check(
                    ct is None or KELVIN_MIN <= ct[0] < ct[1] <= KELVIN_MAX,
                    f"{fctx}.gamut: ct_range_k must be (floor, ceiling) inside "
                    f"{KELVIN_MIN}-{KELVIN_MAX} K",
                )
                check(
                    ct is None or fx_mat is None or "cct" in fx_mat.capabilities,
                    f"{fctx}.gamut: a ct_range_k on a material without the cct capability",
                )
                check(
                    ct is not None or ct_src == (None, None),
                    f"{fctx}.gamut: ct_sources without a ct_range_k",
                )
                check(
                    ct is None or ct_src[0] is not None or ct_src[1] is not None,
                    f"{fctx}.gamut: a ct_range_k needs a source for at least one end "
                    "(ct_sources); an end no tier spoke for is unbounded",
                )
                check(
                    ct is None or ct_src[0] is not None or ct[0] == KELVIN_MIN,
                    f"{fctx}.gamut: an unbounded floor (no source) carries {KELVIN_MIN} K",
                )
                check(
                    ct is None or ct_src[1] is not None or ct[1] == KELVIN_MAX,
                    f"{fctx}.gamut: an unbounded ceiling (no source) carries {KELVIN_MAX} K",
                )
                keys = [(d.tier, d.end) for d in fx.gamut.ct_declined]
                check(
                    len(keys) == len(set(keys)),
                    f"{fctx}.gamut: ct_declined names a (tier, end) twice",
                )
                for i, end in enumerate(("floor", "ceiling")):
                    end_src = ct_src[i]
                    check(
                        end_src is None or end_src == "inherited" or (end_src, end) not in keys,
                        f"{fctx}.gamut: the {end} came from the {end_src} tier, which ct_declined "
                        "also says declined it",
                    )
                seed_id = fx.gamut.inherited_from
                if seed_id is not None:
                    seed_fx = _find_fixture(m, seed_id)
                    seed_mat = None if seed_fx is None else m.materials.get(seed_fx.material)
                    check(seed_id != fid, f"{fctx}.gamut: inherited from itself")
                    check(
                        seed_fx is not None
                        and seed_fx.gamut is not None
                        and seed_fx.gamut.inherited_from is None,
                        f"{fctx}.gamut: inherited_from '{seed_id}' is not a measured fixture",
                    )
                    check(
                        seed_mat is None
                        or fx_mat is None
                        or (fx_mat.model is not None and fx_mat.model == seed_mat.model),
                        f"{fctx}.gamut: inherited_from '{seed_id}' does not carry the same "
                        f"material model label",
                    )
        for gid, g in room.groups.items():
            gctx = f"{ctx}.groups.{gid}"
            check(g.transport in m.transports, f"{gctx}: transport '{g.transport}' is not declared")
            check(bool(g.members), f"{gctx}: a group needs members")
            pool = room.control_surfaces if g.tier == "phantom" else room.fixtures
            for member in g.members:
                what = "control surface" if g.tier == "phantom" else "fixture"
                check(member in pool, f"{gctx}: member '{member}' is not a {what} of this room")
        for pctx, pres in (
            (f"{ctx}.presence", room.presence),
            (f"{ctx}.switch_zone", room.switch_zone),
        ):
            if pres is None:
                continue
            check(bool(pres.sources), f"{pctx}: needs at least one source")
            for src in pres.sources:
                check(src in m.sensors, f"{pctx}: source '{src}' is not a declared sensor")
                check(
                    src not in lux_sensor_ids,
                    f"{pctx}: '{src}' is a lux sensor, not a presence source",
                )
            if pres.fusion == "or_pir_latched":
                check(
                    pres.pir is not None and pres.pir in pres.sources,
                    f"{pctx}: or_pir_latched needs pir set to one of its sources",
                )
            check(
                pres.coalesce_ms is None or pres.coalesce_ms > 0,
                f"{pctx}: coalesce_ms must be positive when set",
            )
        check(
            room.space is None or room.space in m.spaces,
            f"{ctx}: space '{room.space}' is not declared",
        )
        if room.vacancy.scope == "space":
            check(room.space is not None, f"{ctx}.vacancy: scope 'space' but the room is in none")
            check(
                room.vacancy.seconds is None,
                f"{ctx}.vacancy: seconds belong to the space when scope is 'space'",
            )
        else:
            check(
                room.vacancy.seconds is not None and room.vacancy.seconds > 0,
                f"{ctx}.vacancy: a room-scoped vacancy needs positive seconds",
            )
        seen: dict[str, str] = {}
        for sgid, sg in room.scene_groups.items():
            sctx = f"{ctx}.scene_groups.{sgid}"
            check(
                sgid not in scene_group_owner,
                f"{sctx}: id '{sgid}' is also used in rooms.{scene_group_owner.get(sgid)}",
            )
            scene_group_owner[sgid] = rid
            check(
                sg.default in m.scenes.palettes,
                f"{sctx}: default palette '{sg.default}' is not declared",
            )
            for fid in sg.fixtures:
                check(fid in room.fixtures, f"{sctx}: fixture '{fid}' is not in this room")
                check(
                    fid not in seen,
                    f"{sctx}: fixture '{fid}' is already in scene group '{seen.get(fid)}'",
                )
                seen[fid] = sgid
            for parent in sg.parents:
                check(
                    parent in m.scenes.parents,
                    f"{sctx}: parent selector '{parent}' is not declared",
                )
        for fid, fx in room.fixtures.items():
            listed = fx.scene_group is None or (
                fx.scene_group in room.scene_groups
                and fid in room.scene_groups[fx.scene_group].fixtures
            )
            check(listed, f"{ctx}.fixtures.{fid}: scene_group '{fx.scene_group}' omits it")
        for cid, cs in room.control_surfaces.items():
            cctx = f"{ctx}.control_surfaces.{cid}"
            check(
                cs.transport in m.transports, f"{cctx}: transport '{cs.transport}' is not declared"
            )
            check(
                cs.presence_zone != "switch" or room.switch_zone is not None,
                f"{cctx}: presence_zone 'switch' but the room has no switch_zone",
            )
            if cs.phantom_group is not None:
                pg = room.groups.get(cs.phantom_group)
                check(
                    pg is not None and pg.tier == "phantom",
                    f"{cctx}: phantom_group '{cs.phantom_group}' is not a phantom group here",
                )
                check(
                    pg is None or cid in pg.members,
                    f"{cctx}: this surface is not a member of its phantom group",
                )
        check(
            room.ambient_scope is None or room.ambient_scope in m.ambient_scopes,
            f"{ctx}: ambient_scope '{room.ambient_scope}' is not declared",
        )
        check(
            room.space is None or room.pattern == "A",
            f"{ctx}: pattern C is a behaviour of rooms outside any space",
        )

    for sid, space in m.spaces.items():
        sctx = f"spaces.{sid}"
        check(space.vacancy_s > 0, f"{sctx}: vacancy_s must be positive")
        check(
            any(room.space == sid for room in m.rooms.values()),
            f"{sctx}: no room declares this space",
        )
        for src in space.extra_presence:
            check(src in m.sensors, f"{sctx}: extra_presence '{src}' is not a declared sensor")
            check(src not in lux_sensor_ids, f"{sctx}: '{src}' is a lux sensor, not presence")

    for zid, z in m.zones.items():
        zctx = f"zones.{zid}"
        zroom = m.rooms.get(z.room)
        check(zroom is not None, f"{zctx}: room '{z.room}' is not declared")
        for src in z.sources:
            check(src in m.sensors, f"{zctx}: source '{src}' is not a declared sensor")
        if zroom is not None:
            for fid in z.fixtures:
                check(fid in zroom.fixtures, f"{zctx}: fixture '{fid}' is not in room '{z.room}'")

    for pid, pool_sensors in m.lux.pools.items():
        check(bool(pool_sensors), f"lux.pools.{pid}: a pool needs sensors")
        for sid in pool_sensors:
            check(sid in lux_sensor_ids, f"lux.pools.{pid}: '{sid}' is not a declared lux sensor")
    for scope, source in m.lux.borrow.items():
        check(scope in m.ambient_scopes, f"lux.borrow.{scope}: not an ambient scope")
        check(
            source in m.ambient_scopes,
            f"lux.borrow.{scope}: borrows from '{source}', not an ambient scope",
        )
    for aid, sc in m.ambient_scopes.items():
        actx = f"ambient_scopes.{aid}"
        check(sc.lux_pool in m.lux.pools, f"{actx}: lux_pool '{sc.lux_pool}' is not declared")
        for rid in sc.rooms:
            check(rid in m.rooms, f"{actx}: room '{rid}' is not declared")
        check(
            sc.slaved_to is None or sc.slaved_to in m.ambient_scopes,
            f"{actx}: slaved_to '{sc.slaved_to}' is not an ambient scope",
        )
        if sc.fixtures is not None:
            owned = {fid for rid in sc.rooms if rid in m.rooms for fid in m.rooms[rid].fixtures}
            for fid in sc.fixtures:
                check(fid in owned, f"{actx}: fixture '{fid}' is not in the scope's rooms")
    an = m.ambient_normalization
    check(an.cap >= 1.0, "ambient_normalization.cap must be at least 1.0 (it only adds light)")
    check(0 <= an.deadband < an.cap, "ambient_normalization.deadband must sit inside the cap")

    for pid, p in m.scenes.palettes.items():
        pctx = f"scenes.palettes.{pid}"
        if p.kind == "static":
            check(
                bool(p.bulbs) and bool(p.strips),
                f"{pctx}: a static palette needs bulbs and strips entries",
            )
        if p.kind == "solar_cct_offset":
            check(
                p.offset_k is not None and p.offset_k > 0,
                f"{pctx}: solar_cct_offset needs a positive offset_k",
            )
        for arr, name in ((p.bulbs, "bulbs"), (p.strips, "strips")):
            for i, (x, y) in enumerate(arr):
                check(
                    0.0 <= x <= 1.0 and 0.0 <= y <= 1.0,
                    f"{pctx}.{name}[{i}]: xy outside the unit square",
                )
    for parent, children in m.scenes.parents.items():
        for child in children:
            check(
                child in scene_group_owner,
                f"scenes.parents.{parent}: child '{child}' is not a scene group",
            )
            if child in scene_group_owner:
                room = m.rooms[scene_group_owner[child]]
                check(
                    parent in room.scene_groups[child].parents,
                    f"scenes.parents.{parent}: '{child}' does not list this parent",
                )
    check(m.scenes.tick.linger_s > 0, "scenes.tick.linger_s must be positive")
    check(0 < m.solar.solstice_blend_frac <= 1.0, "solar.solstice_blend_frac must be in (0, 1]")
    # Two measured units of one model label must agree: a larger gap is a wrong measurement
    # or a wrong label, and either would put a wrong polygon under the comparators.
    first_of_label: dict[str, tuple[str, Gamut]] = {}
    for rid, room in m.rooms.items():
        for fid, fx in room.fixtures.items():
            fx_material = m.materials.get(fx.material)
            if fx.gamut is None or fx.gamut.inherited_from is not None:
                continue
            if fx_material is None or fx_material.model is None:
                continue
            first = first_of_label.setdefault(fx_material.model, (fid, fx.gamut))
            if first[0] == fid:
                continue
            gap = vertex_deviation(fx.gamut.vertices, first[1].vertices)
            check(
                gap <= SAME_MODEL_FAIL,
                f"rooms.{rid}.fixtures.{fid}.gamut: differs from {first[0]} by {gap:.4f} "
                f"although both carry the material model '{fx_material.model}' "
                f"(limit {SAME_MODEL_FAIL})",
            )
    return errs


def _find_fixture(m: HomeModel, fixture_id: str) -> Fixture | None:
    for room in m.rooms.values():
        fx = room.fixtures.get(fixture_id)
        if fx is not None:
            return fx
    return None


def load(text: str) -> HomeModel:
    """Parse and validate; raise ModelError on any problem."""
    model = loads(text)
    errs = validate(model)
    if errs:
        raise ModelError(errs)
    return model


# ---------------------------------------------------------------------------------------------
# Writing and derived facts
# ---------------------------------------------------------------------------------------------


def _xy_list(xys: Iterable[XY]) -> list[list[float]]:
    return [[x, y] for x, y in xys]


def to_dict(m: HomeModel) -> dict[str, Any]:
    """The plain-mapping form, complete with defaults, in a stable key order.

    ``from_dict(to_dict(m))`` equals ``m``; that is the lossless round trip the tests assert."""

    def presence(p: Presence) -> dict[str, Any]:
        return {
            "sources": list(p.sources),
            "fusion": p.fusion,
            "pir": p.pir,
            "coalesce_ms": p.coalesce_ms,
        }

    def room(r: Room) -> dict[str, Any]:
        return {
            "space": r.space,
            "pattern": r.pattern,
            "vacancy": {"scope": r.vacancy.scope, "seconds": r.vacancy.seconds},
            "presence": presence(r.presence),
            "switch_zone": None if r.switch_zone is None else presence(r.switch_zone),
            "fixtures": {
                fid: {
                    "material": f.material,
                    "curve": f.curve,
                    "role": f.role,
                    "transport": f.transport,
                    "address": f.address,
                    "scene_group": f.scene_group,
                    "palette_stride": f.palette_stride,
                    **(
                        {}
                        if f.gamut is None
                        else {
                            "gamut": {
                                "vertices": _xy_list(f.gamut.vertices),
                                "bound_to": f.gamut.bound_to,
                                "measured": f.gamut.measured,
                                "model_error": f.gamut.model_error,
                                "clip_rule": f.gamut.clip_rule,
                                "inherited_from": f.gamut.inherited_from,
                                "firmware": f.gamut.firmware,
                                "ct_range_k": (
                                    None if f.gamut.ct_range_k is None else list(f.gamut.ct_range_k)
                                ),
                                "ct_sources": (
                                    None
                                    if f.gamut.ct_sources == (None, None)
                                    else list(f.gamut.ct_sources)
                                ),
                                "ct_declined": (
                                    None
                                    if not f.gamut.ct_declined
                                    else [
                                        [d.tier, d.end, d.verdict, d.reason]
                                        for d in f.gamut.ct_declined
                                    ]
                                ),
                            }
                        }
                    ),
                }
                for fid, f in r.fixtures.items()
            },
            "groups": {
                gid: {
                    "members": list(g.members),
                    "tier": g.tier,
                    "transport": g.transport,
                    "address": g.address,
                }
                for gid, g in r.groups.items()
            },
            "curves": {
                cid: {"occupied": dict(c.occupied), "vacant": dict(c.vacant)}
                for cid, c in r.curves.items()
            },
            "scene_groups": {
                sid: {
                    "fixtures": list(s.fixtures),
                    "default": s.default,
                    "parents": list(s.parents),
                }
                for sid, s in r.scene_groups.items()
            },
            "palette_base": r.palette_base,
            "control_surfaces": {
                cid: {
                    "kind": c.kind,
                    "transport": c.transport,
                    "address": c.address,
                    "presence_zone": c.presence_zone,
                    "phantom_group": c.phantom_group,
                    "led_tier": c.led_tier,
                    "smart_bulb_binding": c.smart_bulb_binding,
                }
                for cid, c in r.control_surfaces.items()
            },
            "ambient_scope": r.ambient_scope,
            "sleep": r.sleep,
        }

    return {
        "home_model": {
            "version": m.version,
            "id": m.id,
            "location": {
                "latitude": m.location.latitude,
                "longitude": m.location.longitude,
                "elevation_m": m.location.elevation_m,
                "tz": m.location.tz,
            },
            "solar": {"solstice_blend_frac": m.solar.solstice_blend_frac, "tick_s": m.solar.tick_s},
            "transports": {
                tid: {
                    "kind": t.kind,
                    "attribution": t.attribution,
                    "endpoint": t.endpoint,
                    "base_topic": t.base_topic,
                    "groupcast": t.groupcast,
                    "stagger_ms": t.stagger_ms,
                }
                for tid, t in m.transports.items()
            },
            "calibrations": {
                cid: {
                    "warm_xy": list(c.warm_xy),
                    "cool_xy": list(c.cool_xy),
                    "night_xy": list(c.night_xy),
                }
                for cid, c in m.calibrations.items()
            },
            "materials": {
                mid: {
                    "kind": mat.kind,
                    "capabilities": sorted(mat.capabilities),
                    "cct_range_k": None if mat.cct_range_k is None else list(mat.cct_range_k),
                    "calibration": mat.calibration,
                    "model": mat.model,
                }
                for mid, mat in m.materials.items()
            },
            "sensors": {
                sid: {
                    "modality": s.modality,
                    "transport": s.transport,
                    "device": s.device,
                    "zone": s.zone,
                    "address": s.address,
                }
                for sid, s in m.sensors.items()
            },
            "spaces": {
                sid: {
                    "vacancy_s": s.vacancy_s,
                    "extra_presence": list(s.extra_presence),
                    "wake_relight": s.wake_relight,
                }
                for sid, s in m.spaces.items()
            },
            "rooms": {rid: room(r) for rid, r in m.rooms.items()},
            "zones": {
                zid: {
                    "room": z.room,
                    "sources": list(z.sources),
                    "fixtures": list(z.fixtures),
                    "fusion": z.fusion,
                }
                for zid, z in m.zones.items()
            },
            "lux": {
                "pools": {pid: list(p) for pid, p in m.lux.pools.items()},
                "borrow": dict(m.lux.borrow),
            },
            "ambient_scopes": {
                aid: {
                    "rooms": list(a.rooms),
                    "lux_pool": a.lux_pool,
                    "drive": a.drive,
                    "fixtures": None if a.fixtures is None else list(a.fixtures),
                    "slaved_to": a.slaved_to,
                }
                for aid, a in m.ambient_scopes.items()
            },
            "ambient_normalization": {
                "weather_data": m.ambient_normalization.weather_data,
                "cap": m.ambient_normalization.cap,
                "deadband": m.ambient_normalization.deadband,
                "dose_k": m.ambient_normalization.dose_k,
                "tau_s": m.ambient_normalization.tau_s,
                "elevation_gate_deg": m.ambient_normalization.elevation_gate_deg,
            },
            "scenes": {
                "palettes": {
                    pid: {
                        "kind": p.kind,
                        "bulbs": _xy_list(p.bulbs),
                        "strips": _xy_list(p.strips),
                        "offset_k": p.offset_k,
                    }
                    for pid, p in m.scenes.palettes.items()
                },
                "parents": {
                    parent: list(children) for parent, children in m.scenes.parents.items()
                },
                "tick": {
                    "linger_s": m.scenes.tick.linger_s,
                    "transition_s": m.scenes.tick.transition_s,
                    "fast_transition_s": m.scenes.tick.fast_transition_s,
                    "offset_wrap": m.scenes.tick.offset_wrap,
                },
            },
        }
    }


def dumps(m: HomeModel) -> str:
    return yaml.safe_dump(to_dict(m), sort_keys=False, allow_unicode=True)


def fixture_gamut(m: HomeModel, fixture_id: str) -> tuple[XY, ...]:
    """The polygon of a fixture (measured or inherited), or an empty tuple (the identity) when
    it has none or is unknown. Fixture ids are unique across rooms."""
    fx = _find_fixture(m, fixture_id)
    return () if fx is None or fx.gamut is None else fx.gamut.vertices


CctEndSource = Literal["probed", "observed", "declared", "inherited", "author"]
"""A resolved end's provenance: a ``CtSource`` from the fixture's gamut, or ``author`` when the
material's declaration supplied it."""


def fixture_cct_range(m: HomeModel, fixture_id: str) -> tuple[int | None, int | None]:
    """The colour temperatures a fixture can show, kelvin (floor, ceiling), each end resolved
    on its own: the gamut's end when a tier spoke for it (``Gamut.ct_sources``), else the
    material's declaration (the author's word), else None, meaning nothing is known about
    that end. What the render floors a colour temperature at (a None floor leaves the render
    its default) and what a comparator clips the intent to before judging the device (a None
    end clips nothing). (None, None) for a fixture without ``cct``, or unknown."""
    (floor_k, _), (ceiling_k, _) = _cct_ends(m, fixture_id)
    return (floor_k, ceiling_k)


def unobservable(gamut: Gamut | None) -> tuple[bool, bool]:
    """Per end (floor, ceiling): whether nothing on this transport can bound it: the end has
    no source and every tier in ``CT_TRIED_TIERS`` declined it as ``unobservable``. An end
    with a source is bounded; an unsourced end with a tier untried or inconclusive is not
    known yet either way. The only state in which asking the author for the range is
    justified."""
    if gamut is None:
        return (False, False)

    def proven(i: int, end: str) -> bool:
        if gamut.ct_sources[i] is not None:
            return False
        shown = {d.tier for d in gamut.ct_declined if d.end == end and d.verdict == "unobservable"}
        return all(t in shown for t in CT_TRIED_TIERS)

    return (proven(0, "floor"), proven(1, "ceiling"))


def untried(gamut: Gamut | None) -> tuple[tuple[CtTier, ...], tuple[CtTier, ...]]:
    """Per end: the tiers that have not yet spoken with proof (never tried, or inconclusive),
    for a reader deciding what to try next. Empty for a sourced end."""
    if gamut is None:
        return (CT_TRIED_TIERS, CT_TRIED_TIERS)
    out: list[tuple[CtTier, ...]] = []
    for i, end in enumerate(("floor", "ceiling")):
        if gamut.ct_sources[i] is not None:
            out.append(())
            continue
        shown = {d.tier for d in gamut.ct_declined if d.end == end and d.verdict == "unobservable"}
        out.append(tuple(t for t in CT_TRIED_TIERS if t not in shown))
    return (out[0], out[1])


def fixture_cct_sources(
    m: HomeModel, fixture_id: str
) -> tuple[CctEndSource | None, CctEndSource | None]:
    """Where each end of :func:`fixture_cct_range` came from: the gamut's tier, ``author``
    for the material's declaration, None for an end nothing speaks for."""
    (_, floor_src), (_, ceiling_src) = _cct_ends(m, fixture_id)
    return (floor_src, ceiling_src)


def _cct_ends(
    m: HomeModel, fixture_id: str
) -> tuple[tuple[int | None, CctEndSource | None], tuple[int | None, CctEndSource | None]]:
    fx = _find_fixture(m, fixture_id)
    if fx is None:
        return ((None, None), (None, None))
    mat = m.materials.get(fx.material)
    declared = None if mat is None else mat.cct_range_k
    out: list[tuple[int | None, CctEndSource | None]] = []
    for i in (0, 1):
        if fx.gamut is not None and fx.gamut.ct_range_k is not None and fx.gamut.ct_sources[i]:
            out.append((fx.gamut.ct_range_k[i], fx.gamut.ct_sources[i]))
        elif declared is not None:
            out.append((declared[i], "author"))
        else:
            out.append((None, None))
    return (out[0], out[1])


def fixture_clip(m: HomeModel, fixture_id: str) -> tuple[tuple[XY, ...], ClipRule]:
    """The polygon AND the clip rule a comparator needs for a fixture; ``((), "closest")``,
    the identity, when it has no gamut."""
    fx = _find_fixture(m, fixture_id)
    if fx is None or fx.gamut is None:
        return (), "closest"
    return fx.gamut.vertices, fx.gamut.clip_rule


def fixture_model_label(m: HomeModel, fixture_id: str) -> str | None:
    """The model label of a fixture's material, the author's declaration of what hardware it
    is; None when the fixture is unknown or its material carries no label."""
    fx = _find_fixture(m, fixture_id)
    if fx is None:
        return None
    mat = m.materials.get(fx.material)
    return None if mat is None else mat.model


def with_fixture_gamut(m: HomeModel, fixture_id: str, gamut: Gamut | None) -> HomeModel:
    """A copy of the model with one fixture's gamut set (or cleared). The measurement writes
    the model the way the tuning UI does; nothing in the model is mutated in place."""
    for rid, room in m.rooms.items():
        if fixture_id in room.fixtures:
            fixtures = dict(room.fixtures)
            fixtures[fixture_id] = replace(fixtures[fixture_id], gamut=gamut)
            rooms = dict(m.rooms)
            rooms[rid] = replace(room, fixtures=fixtures)
            return replace(m, rooms=rooms)
    raise KeyError(fixture_id)


def space_rooms(m: HomeModel, space_id: str) -> tuple[str, ...]:
    """The rooms that declare membership of a space, in model order."""
    return tuple(rid for rid, room in m.rooms.items() if room.space == space_id)


def consumer_envelopes(m: HomeModel) -> dict[str, int | None]:
    """The derived per-fusion release-hold ceilings: a presence composite whose consumer coalesces
    trigger bursts is bounded by that window; one with no such consumer is bounded only by the
    engine's maximum hold (``None`` here). Never authored; always computed from the model."""
    out: dict[str, int | None] = {}
    for rid, room in m.rooms.items():
        out[rid] = room.presence.coalesce_ms
        if room.switch_zone is not None:
            out[f"{rid}_switch"] = room.switch_zone.coalesce_ms
    for zid, z in m.zones.items():
        out[zid] = m.rooms[z.room].presence.coalesce_ms if z.room in m.rooms else None
    return out
