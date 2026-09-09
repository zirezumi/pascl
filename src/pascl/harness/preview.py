"""The preview evaluator.

The same pure render the engine runs, pointed at an instant and a set of assumptions instead of
a live home: give it a model, a time, which rooms are occupied and what their layers hold, and
it returns every fixture's frame. Scrub the time and the season and the whole day's behaviour
is visible offline, which is what the tuning UI will show and what the property suite drives.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from pascl.core import solar
from pascl.core.palette import NaturalWhite, natural_white
from pascl.core.render import FixtureState, Frame, Phase, RoomState, SolarState, render_room
from pascl.core.solar import Site
from pascl.model import HomeModel


@dataclass(frozen=True)
class PreviewInputs:
    phases: Mapping[str, Phase] = field(default_factory=dict)
    """Room -> phase; rooms not listed are occupied."""
    multipliers: Mapping[str, float] = field(default_factory=dict)
    ambient: Mapping[str, float] = field(default_factory=dict)
    """Room -> ambient multiplier already composed (1.0 when absent)."""
    fixtures: Mapping[str, FixtureState] = field(default_factory=dict)
    scene_bindings: Mapping[str, str] = field(default_factory=dict)
    scene_offset: int = 0
    sleeping: bool = False
    warm_offset_k: float = 0.0


DEFAULT_INPUTS: Final = PreviewInputs()


@dataclass(frozen=True)
class PreviewResult:
    at: datetime
    solar: SolarState
    white: NaturalWhite
    frames: dict[str, Frame]


def site_of(model: HomeModel) -> Site:
    loc = model.location
    return Site(loc.latitude, loc.longitude, loc.elevation_m, ZoneInfo(loc.tz))


def solar_state(model: HomeModel, at: datetime) -> SolarState:
    """The solar inputs the render reads at ``at``, rounded the way the reference publishes them."""
    site = site_of(model)
    params = solar.SolarParams(solstice_blend_frac=model.solar.solstice_blend_frac)
    day = solar.solar_date(site, at)
    today = solar.compute_day(site, day, params)
    ref = solar.reference_day(site, today, params)
    norm = solar.normalize(today, ref, solar.is_dst(at.astimezone(site.tz)), params)
    progress = solar.day_progress(norm, at)
    pct = {k: round(v, 6) for k, v in solar.anchor_percents(norm).items() if v is not None}
    return SolarState(
        progress=-1.0 if progress is None else round(progress, 3),
        factor=round(solar.daylight_factor(norm, at), 3),
        pct=pct,
        ready=solar.ready(today),
    )


def preview(
    model: HomeModel, at: datetime, inputs: PreviewInputs = DEFAULT_INPUTS
) -> PreviewResult:
    sol = solar_state(model, at)
    white = natural_white(sol.progress, sol.factor, sol.pct)
    frames: dict[str, Frame] = {}
    for rid in model.rooms:
        state = RoomState(
            phase=inputs.phases.get(rid, "occupied"),
            multiplier=inputs.multipliers.get(rid, 1.0),
            ambient=inputs.ambient.get(rid, 1.0),
            sleeping=inputs.sleeping,
        )
        frames.update(
            render_room(
                model,
                rid,
                sol,
                state,
                inputs.fixtures,
                white,
                inputs.scene_bindings,
                inputs.scene_offset,
                inputs.warm_offset_k,
            )
        )
    return PreviewResult(at=at, solar=sol, white=white, frames=frames)


def scrub(
    model: HomeModel,
    start: datetime,
    end: datetime,
    step: timedelta,
    inputs: PreviewInputs = DEFAULT_INPUTS,
) -> Iterator[PreviewResult]:
    at = start
    while at <= end:
        yield preview(model, at, inputs)
        at += step
