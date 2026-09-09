"""The solar clock against a recorded day of the reference installation.

The golden file is private (it carries the home's coordinates) and lives outside this
repository. Point ``PASCL_PRIVATE_GOLDEN`` at the directory holding ``solar_*.json`` files to
run these; without it they are skipped, which is what CI does.

Each file is a snapshot of the reference engine's published anchors at one instant plus that
day's progress and daylight-factor timeline. The port must land every anchor within the
crossing tolerance and every published fraction within its rounding.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pascl.core import solar
from pascl.core.solar import Site

GOLDEN_DIR = os.environ.get("PASCL_PRIVATE_GOLDEN")
FILES = sorted(Path(GOLDEN_DIR).glob("solar_*.json")) if GOLDEN_DIR else []

pytestmark = pytest.mark.skipif(
    not FILES, reason="PASCL_PRIVATE_GOLDEN not set or holds no solar_*.json"
)


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _site(g: dict[str, object]) -> Site:
    s = g["site"]
    assert isinstance(s, dict)
    return Site(
        latitude=float(s["latitude"]),
        longitude=float(s["longitude"]),
        elevation_m=float(s["elevation"]),
        tz=ZoneInfo(str(s["tz"])),
    )


def _state(g: dict[str, object], entity: str) -> str:
    states = g["states"]
    assert isinstance(states, dict)
    return str(states[entity]["state"])


@pytest.mark.parametrize("path", FILES, ids=[p.stem for p in FILES])
def test_anchors_percents_and_timeline_match_the_reference(path: Path) -> None:
    g = _load(path)
    site = _site(g)
    captured = datetime.fromisoformat(str(g["captured_at"]))
    day = solar.solar_date(site, captured)
    today = solar.compute_day(site, day)
    ref = solar.reference_day(site, today)
    norm = solar.normalize(today, ref, dst_in_force=solar.is_dst(captured.astimezone(site.tz)))

    tol = 2.0
    for k in solar.ANCHOR_KEYS:
        want_real = datetime.fromisoformat(_state(g, f"sensor.sol_{k}"))
        want_norm = datetime.fromisoformat(_state(g, f"sensor.sol_{k}_normalized"))
        assert abs((today[k] - want_real).total_seconds()) <= tol, f"{k} real"
        assert abs((norm[k] - want_norm).total_seconds()) <= tol, f"{k} normalised"

    assert abs(today.day_length.total_seconds() - float(_state(g, "sensor.sol_day_length"))) <= tol
    assert (
        abs(norm.day_length.total_seconds() - float(_state(g, "sensor.sol_day_length_normalized")))
        <= tol
    )

    pct = solar.anchor_percents(norm)
    for k in solar.PERCENT_KEYS:
        want = float(_state(g, f"sensor.sol_{k}_normalized_pct"))
        got = pct[k]
        assert got is not None
        assert abs(got - want) <= 1.5e-6 + tol / 86400, k

    timeline = g["timeline"]
    assert isinstance(timeline, list)
    checked = 0
    for row in timeline:
        assert isinstance(row, dict)
        at = datetime.fromisoformat(str(row["t"]))
        want = float(row["state"])
        if row["id"] == "sensor.sol_normalized_p":
            got = solar.day_progress(norm, at)
            assert got is not None
            assert abs(round(got, 3) - want) <= 0.001 + 1e-9, row
            checked += 1
        elif row["id"] == "sensor.sol_factor":
            assert abs(round(solar.daylight_factor(norm, at), 3) - want) <= 0.002 + 1e-9, row
            checked += 1
    assert checked > 0

    pos = solar.position(site, captured)
    assert abs(pos.elevation - float(_state(g, "sensor.sol_elevation"))) <= 0.6
    assert abs(pos.azimuth - float(_state(g, "sensor.sol_azimuth"))) <= 1.0
    assert abs(pos.declination - float(_state(g, "sensor.sol_declination"))) <= 0.01
    assert abs(pos.equation_of_time - float(_state(g, "sensor.sol_equation_of_time"))) <= 0.05
