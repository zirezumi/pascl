"""The render: one fixture's frame from the model and the inputs.

``render_fixture`` is the pure function at the centre of the engine: given the solar state, the
room's presence phase and layers, the fixture's own layers and the scene's colour, it returns
what the fixture should show. Brightness is the eased curve for the room's tier (occupied or
vacant), scaled by ambient normalisation, the room multiplier and the fixture factor. Colour is
the scene's goal, pulled toward a held override by its decaying colour factor in OKLab, and then
published as a colour temperature when it sits on the white arc and the fixture can render one,
else as chromaticity. Sleep suppresses a room's light while the frame keeps rendering underneath.

Nothing here reads a clock, a model file or a device; every input is a value.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from pascl.core import curves
from pascl.core.color import XY, blend_xy, kelvin_to_mired
from pascl.core.palette import NaturalWhite, natural_xy, scene_goal, warm_kelvin
from pascl.model.schema import Calibration, Fixture, HomeModel, Material, Palette, Room

Phase = Literal["occupied", "fading", "vacant"]
Mode = Literal["ct", "xy", "off"]

COLOR_FACTOR_DEADZONE: Final = 0.02
CT_TOLERANCE_ENTER: Final = 0.003
CT_TOLERANCE_HOLD: Final = 0.006
DEFAULT_MIN_KELVIN: Final = 2000
SLEEP_DIM_FRACTION: Final = 0.5


@dataclass(frozen=True)
class SolarState:
    progress: float
    factor: float
    pct: Mapping[str, float]
    ready: bool = True


@dataclass(frozen=True)
class RoomState:
    phase: Phase = "occupied"
    multiplier: float = 1.0
    ambient: float = 1.0
    sleeping: bool = False


@dataclass(frozen=True)
class FixtureState:
    factor: float = 1.0
    held_off: bool = False
    color_factor: float = 0.0
    override_xy: XY | None = None
    override_kelvin: float | None = None
    override_is_ct: bool = False
    ct_mode_cached: bool = False
    scene_xy: XY | None = None


@dataclass(frozen=True)
class Frame:
    fixture: str
    on: bool
    brightness: int | None
    xy: XY | None
    ct_mired: int | None
    mode: Mode
    solar_baseline: int
    goal: int
    baseline_target: float
    virtual: bool
    """True when sleep suppresses the light and this is the frame it would otherwise show."""


def ambient_factor(coupling: float | None, accumulator: float, drive: bool) -> float:
    """The room's ambient multiplier: 1 plus its coupling times the accumulator's excess over 1,
    rounded to six decimals; exactly 1 when the drive is off or nothing is known."""
    if not drive or coupling is None:
        return 1.0
    c = min(max(coupling, 0.0), 1.0)
    return round(1.0 + c * (accumulator - 1.0), 6)


def _near(a: XY | None, b: XY | None, tol: float) -> bool:
    if a is None or b is None:
        return False
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


def render_fixture(
    model: HomeModel,
    room_id: str,
    fixture_id: str,
    solar: SolarState,
    room_state: RoomState,
    fx_state: FixtureState,
    white: NaturalWhite,
    palette: Palette | None,
    scene_offset: int = 0,
    warm_offset_k: float = 0.0,
) -> Frame:
    room: Room = model.rooms[room_id]
    fx: Fixture = room.fixtures[fixture_id]
    material: Material = model.materials[fx.material]
    calibration: Calibration | None = (
        model.calibrations.get(material.calibration) if material.calibration else None
    )
    is_strip = material.kind == "strip"
    is_bulb = not is_strip

    # --- brightness ---------------------------------------------------------------------
    anchors = curves.AnchorTable.from_day(
        solar.pct.get("civil_dawn", 0.20),
        solar.pct.get("noon", 0.50),
        solar.pct.get("civil_dusk", 0.80),
    )
    table = (
        room.curves[fx.curve].occupied
        if room_state.phase == "occupied"
        else room.curves[fx.curve].vacant
    )
    bri = curves.compose(
        table, solar.progress, anchors, room_state.ambient, room_state.multiplier, fx_state.factor
    )

    # --- colour --------------------------------------------------------------------------
    goal_xy: XY | None
    natural_anchor: XY | None
    if calibration is not None:
        natural_anchor = natural_xy(white, calibration)
        warm_k = warm_kelvin(white, warm_offset_k)
        if fx_state.scene_xy is not None:
            goal_xy = fx_state.scene_xy
        elif palette is not None:
            goal_xy = scene_goal(
                palette,
                is_strip,
                white,
                calibration,
                scene_offset,
                room.palette_base,
                fx.palette_stride,
            )
        else:
            goal_xy = natural_anchor
    else:
        natural_anchor = None
        warm_k = warm_kelvin(white, warm_offset_k)
        goal_xy = fx_state.scene_xy

    baseline_xy = goal_xy
    cf = fx_state.color_factor
    has_anchor = (
        cf > COLOR_FACTOR_DEADZONE
        and fx_state.override_xy is not None
        and fx_state.override_xy[0] >= 0
        and fx_state.override_xy[1] >= 0
    )
    if has_anchor and goal_xy is not None and fx_state.override_xy is not None:
        goal_xy = blend_xy(goal_xy, fx_state.override_xy, cf)

    # CT decision: the bulb anchors are the comparison, so a strip never takes it.
    tol = CT_TOLERANCE_HOLD if fx_state.ct_mode_cached else CT_TOLERANCE_ENTER
    bulb_calib = _bulb_calibration(model, material, calibration)
    natural_bulb = natural_xy(white, bulb_calib) if bulb_calib else None
    warm_bulb = _warm_anchor(white, bulb_calib, warm_offset_k) if bulb_calib else None
    can_ct = "cct" in material.capabilities and goal_xy is not None
    matches_natural = can_ct and natural_bulb is not None and _near(goal_xy, natural_bulb, tol)
    matches_warm = can_ct and warm_bulb is not None and _near(goal_xy, warm_bulb, tol)
    min_k = material.cct_range_k[0] if material.cct_range_k else DEFAULT_MIN_KELVIN
    ceiling_mired = kelvin_to_mired(min_k)
    goal_ct: int | None = None
    use_ct = False
    if matches_natural:
        goal_ct = kelvin_to_mired(white.kelvin_rounded)
        use_ct = goal_ct <= ceiling_mired
    elif matches_warm:
        goal_ct = kelvin_to_mired(round(warm_k))
        use_ct = goal_ct <= ceiling_mired

    # A colour-temperature flavoured override on a bulb.
    if (
        fx_state.override_is_ct
        and is_bulb
        and cf > COLOR_FACTOR_DEADZONE
        and fx_state.override_kelvin
        and "cct" in material.capabilities
        and baseline_xy is not None
    ):
        baseline_ct = (
            natural_bulb is not None and _near(baseline_xy, natural_bulb, CT_TOLERANCE_ENTER)
        ) or (warm_bulb is not None and _near(baseline_xy, warm_bulb, CT_TOLERANCE_ENTER))
        override_mired = kelvin_to_mired(fx_state.override_kelvin)
        if baseline_ct:
            base_k = (
                white.kelvin_rounded
                if (
                    natural_bulb is not None
                    and _near(baseline_xy, natural_bulb, CT_TOLERANCE_ENTER)
                )
                else round(warm_k)
            )
            baseline_mired = kelvin_to_mired(base_k)
            goal_ct = int(baseline_mired + cf * (override_mired - baseline_mired))
            use_ct = True
        elif cf >= 1.0:
            goal_ct = override_mired
            use_ct = True

    # --- on/off and sleep ----------------------------------------------------------------
    lit = room_state.phase != "vacant" and not fx_state.held_off and not bri.off
    brightness = bri.goal
    virtual = False
    if lit and room_state.sleeping:
        if room.sleep == "suppress":
            virtual = True
            lit = False
        elif room.sleep == "dim":
            brightness = curves.clamp_level(brightness * SLEEP_DIM_FRACTION)
    mode: Mode = "off" if not lit else ("ct" if use_ct else "xy")
    return Frame(
        fixture=fixture_id,
        on=lit,
        brightness=brightness if lit or virtual else None,
        xy=None if use_ct else goal_xy,
        ct_mired=goal_ct if use_ct else None,
        mode=mode,
        solar_baseline=bri.solar_baseline,
        goal=bri.goal,
        baseline_target=bri.baseline_target,
        virtual=virtual,
    )


def _warm_anchor(
    white: NaturalWhite, calibration: Calibration | None, warm_offset_k: float
) -> XY | None:
    if calibration is None:
        return None
    from pascl.core.palette import kelvin_xy

    return kelvin_xy(warm_kelvin(white, warm_offset_k), calibration)


def _bulb_calibration(
    model: HomeModel, material: Material, own: Calibration | None
) -> Calibration | None:
    """The bulb family's anchors, which the reference compares every fixture against."""
    if material.kind == "bulb" and own is not None:
        return own
    for mat in model.materials.values():
        if mat.kind == "bulb" and mat.calibration and mat.calibration in model.calibrations:
            return model.calibrations[mat.calibration]
    return own


def render_room(
    model: HomeModel,
    room_id: str,
    solar: SolarState,
    room_state: RoomState,
    fixture_states: Mapping[str, FixtureState],
    white: NaturalWhite,
    scene_bindings: Mapping[str, str],
    scene_offset: int = 0,
    warm_offset_k: float = 0.0,
) -> dict[str, Frame]:
    """Every fixture of a room. ``scene_bindings`` maps a scene group to the palette it shows;
    a group with no binding shows its default."""
    room = model.rooms[room_id]
    frames: dict[str, Frame] = {}
    for fid, fx in room.fixtures.items():
        palette: Palette | None = None
        if fx.scene_group is not None:
            group = room.scene_groups[fx.scene_group]
            pid = scene_bindings.get(fx.scene_group, group.default)
            palette = model.scenes.palettes.get(pid)
        frames[fid] = render_fixture(
            model,
            room_id,
            fid,
            solar,
            room_state,
            fixture_states.get(fid, FixtureState()),
            white,
            palette,
            scene_offset,
            warm_offset_k,
        )
    return frames
