"""The palette report: which authored colours the measured fixtures cannot show."""

from __future__ import annotations

from pathlib import Path

from pascl.core.gamut import Polygon, clip, toward_white
from pascl.core.palette_reach import unreachable
from pascl.model import Gamut, load, with_fixture_gamut

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "demo_home.yaml"
TRIANGLE: Polygon = ((0.153185, 0.047547), (0.691493, 0.308293), (0.169986, 0.699992))
#: A gamut so small that the demo's dusk palette and the warm end of the solar arc fall out.
SMALL: Polygon = ((0.25, 0.25), (0.45, 0.30), (0.30, 0.45))


def test_nothing_to_report_without_a_gamut_and_only_the_entries_outside_with_one() -> None:
    model = load(EXAMPLE.read_text(encoding="utf-8"))
    assert unreachable(model) == []
    # the demo's dusk palette (the reference installation's) has entries a Hue-class
    # triangle cannot show; exactly those are reported, with the clip that renders instead
    m2 = with_fixture_gamut(model, "den_pendant_1", Gamut(TRIANGLE))
    m2 = with_fixture_gamut(m2, "den_strip", Gamut(TRIANGLE))
    rows = unreachable(m2)
    dusk = model.scenes.palettes["dusk"]
    expected = []
    for family, entries, fid in (
        ("bulbs", dusk.bulbs, "den_pendant_1"),
        ("strips", dusk.strips, "den_strip"),
    ):
        for i, entry in enumerate(entries):
            shown = clip(entry, TRIANGLE)
            if max(abs(shown[0] - entry[0]), abs(shown[1] - entry[1])) > 0.001:
                expected.append((family, i, fid, shown))
    assert expected  # the fixture is not vacuous
    assert [
        (r.family, r.index, r.fixtures[0], r.reachable) for r in rows if r.palette == "dusk"
    ] == expected
    assert all(r.palette == "dusk" for r in rows)  # the solar arcs stay inside the triangle


def test_static_entries_and_arcs_are_reported_per_gamut_group() -> None:
    model = load(EXAMPLE.read_text(encoding="utf-8"))
    m2 = with_fixture_gamut(model, "den_pendant_1", Gamut(SMALL))
    m2 = with_fixture_gamut(m2, "den_pendant_2", Gamut(SMALL))
    m2 = with_fixture_gamut(m2, "den_strip", Gamut(TRIANGLE))
    rows = unreachable(m2)
    static = [r for r in rows if r.palette == "dusk" and r.family == "bulbs"]
    # the two pendants share a polygon: one row per entry, naming both; the strip is its own
    # family and polygon and is reported on its own
    assert [r.index for r in static] == [0, 1, 2]
    assert all(r.fixtures == ("den_pendant_1", "den_pendant_2") for r in static)
    for r in static:
        assert r.reachable == clip(r.intent, SMALL)
        assert r.gap > 0.001
    strip = [r for r in rows if r.palette == "dusk" and r.family == "strips"]
    assert strip and all(r.fixtures == ("den_strip",) for r in strip)
    arcs = [r for r in rows if r.family == "arc"]
    # the natural white and the warm palette both leave the small gamut at the warm end
    assert {r.palette for r in arcs} == {"natural_white", "warm"}
    for r in arcs:
        assert r.kelvin is not None and r.kelvin < 2000
        assert r.fixtures == ("den_pendant_1", "den_pendant_2")
        assert r.reachable == clip(r.intent, SMALL)
    # sorted by palette, family, index, then the worst gap first
    assert [r.palette for r in rows] == sorted(r.palette for r in rows)


def test_the_fitted_rule_decides_what_is_shown() -> None:
    model = load(EXAMPLE.read_text(encoding="utf-8"))
    m2 = with_fixture_gamut(model, "den_pendant_1", Gamut(SMALL, clip_rule="toward_white"))
    rows = [r for r in unreachable(m2) if r.palette == "dusk"]
    assert rows and all(r.reachable == toward_white(r.intent, SMALL) for r in rows)
