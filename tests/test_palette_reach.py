"""The palette report: which authored colours the measured fixtures cannot show."""

from __future__ import annotations

from pathlib import Path

from pascl.core.gamut import Polygon, clip, toward_white
from pascl.core.palette_reach import (
    CREDIBLE_K,
    cct_credibility,
    cct_unreachable,
    unreachable,
)
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


def test_a_white_only_fixture_parks_the_night_arc_at_its_floor() -> None:
    """The demo pantry light has cct and no xy at 2200-6500 K: the solar arcs' 1475 K night
    white cannot be rendered as xy for it, so both white palettes report the 725 K gap; the
    colour bulbs, which fall back to xy below their floor, report nothing."""
    model = load(EXAMPLE.read_text(encoding="utf-8"))
    rows = cct_unreachable(model)
    assert [(r.palette, r.kelvin, r.reachable, r.gap, r.fixtures) for r in rows] == [
        ("natural_white", 1475.0, 2200, 725.0, ("pantry_light",)),
        ("warm", 1475.0, 2200, 725.0, ("pantry_light",)),
    ]
    assert all(r.ct_range_k == (2200, 6500) for r in rows)
    # the demo's ranges are all credible
    assert cct_credibility(model) == []


def test_a_placeholder_range_is_not_credible_until_measured() -> None:
    """The reference installation's finding: a transport advertising 1000-20000 K for bulbs
    whose physical range is 2000-6535 K. Declared, it is flagged and named as the render's
    floor; measured on one fixture, that fixture leaves the row and the rest stay."""
    import yaml

    from pascl.model import Gamut, from_dict, with_fixture_gamut

    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["home_model"]["materials"]["color_bulb"]["cct_range_k"] = [1000, 20000]
    model = from_dict(raw)
    rows = cct_credibility(model)
    assert len(rows) == 1 and rows[0].source == "author"
    assert rows[0].ct_range_k == (1000, 20000)
    assert set(rows[0].fixtures) == {"den_pendant_1", "den_pendant_2"}
    assert f"below {CREDIBLE_K[0]} K" in rows[0].note and "transport's placeholder" in rows[0].note
    assert rows[0].material == "color_bulb" and rows[0].question is None
    measured = with_fixture_gamut(
        model,
        "den_pendant_1",
        Gamut(TRIANGLE, ct_range_k=(2000, 6535), ct_sources=("probed", "probed")),
    )
    rows = cct_credibility(measured)
    assert len(rows) == 1 and rows[0].fixtures == ("den_pendant_2",)
    # a measurement outside the credible band is reported as such, on its own row
    odd = with_fixture_gamut(
        model,
        "den_pendant_2",
        Gamut(TRIANGLE, ct_range_k=(1200, 6535), ct_sources=("probed", "probed")),
    )
    rows = cct_credibility(odd)
    assert [(r.source, r.fixtures) for r in rows] == [
        ("author", ("den_pendant_1",)),
        ("probed", ("den_pendant_2",)),
    ]
    assert "wants a look" in rows[1].note


def test_an_end_no_tier_spoke_for_is_named_as_unknown() -> None:
    """The reference installation's 22: every tier declined, the material declares nothing,
    so the range has an observed floor and an unknown ceiling (or nothing at all), and the
    check says so per end rather than inventing a number."""
    import yaml

    from pascl.model import Gamut, from_dict, with_fixture_gamut

    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["home_model"]["materials"]["color_bulb"].pop("cct_range_k")
    model = from_dict(raw)
    rows = cct_credibility(model)
    assert len(rows) == 1 and rows[0].ct_range_k == (None, None)
    assert rows[0].sources == (None, None) and rows[0].source == "unknown"
    # nothing tried yet: not a question for the author, a measurement away
    assert "floor not known yet" in rows[0].note and "ceiling not known yet" in rows[0].note
    assert "probed,observed,declared still to speak" in rows[0].note
    assert "not a question for the author yet" in rows[0].note and rows[0].question is None
    half = with_fixture_gamut(
        model,
        "den_pendant_1",
        Gamut(TRIANGLE, ct_range_k=(1996, 20000), ct_sources=("observed", None)),
    )
    rows = cct_credibility(half)
    mine = [r for r in rows if r.fixtures == ("den_pendant_1",)]
    assert len(mine) == 1 and mine[0].ct_range_k == (1996, None)
    assert mine[0].sources == ("observed", None) and mine[0].source == "observed"
    assert "ceiling not known yet" in mine[0].note and "floor" not in mine[0].note.split(";")[0]
    # the arc report never parks anything at an unknown end
    assert cct_unreachable(half) == cct_unreachable(model)


def test_the_one_question_is_asked_only_after_every_tier_declined_with_proof() -> None:
    """The reference installation's 22: probes answered their own values, the declaration is
    the attribute's span, the channel repeated commands past any emitter's floor. Only then
    does the check ask the author, once per material, with the proof beside it; and the
    author's answer on the material closes it."""
    import yaml

    from pascl.model import CtDecline, Gamut, from_dict, with_fixture_gamut

    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["home_model"]["materials"]["color_bulb"].pop("cct_range_k")
    model = from_dict(raw)

    def declined(*tiers: str, verdict: str = "unobservable") -> Gamut:
        rows = tuple(
            CtDecline(t, end, verdict, f"{t} says no")  # type: ignore[arg-type]
            for t in tiers
            for end in ("floor", "ceiling")
        )
        return Gamut(TRIANGLE, ct_declined=rows)

    # two tiers with proof, one still to speak: not yet a question
    partial = with_fixture_gamut(model, "den_pendant_1", declined("probed", "declared"))
    rows = [r for r in cct_credibility(partial) if "den_pendant_1" in r.fixtures]
    assert len(rows) == 1 and rows[0].question is None
    assert "observed still to speak" in rows[0].note
    # every tier, but the observed one inconclusive: still not a question
    soft = with_fixture_gamut(
        model,
        "den_pendant_1",
        Gamut(
            TRIANGLE,
            ct_declined=(
                *declined("probed", "declared").ct_declined,
                *declined("observed", verdict="inconclusive").ct_declined,
            ),
        ),
    )
    rows = [r for r in cct_credibility(soft) if "den_pendant_1" in r.fixtures]
    assert rows[0].question is None and "observed still to speak" in rows[0].note
    # every tier with proof: the question, on the material, with the proof
    proven = with_fixture_gamut(model, "den_pendant_1", declined("probed", "declared", "observed"))
    rows = [r for r in cct_credibility(proven) if "den_pendant_1" in r.fixtures]
    assert len(rows) == 1 and rows[0].question is not None
    assert "floor unobservable" in rows[0].note and "ceiling unobservable" in rows[0].note
    q = rows[0].question
    assert "material 'color_bulb'" in q and "floor and ceiling" in q and "cct_range_k" in q
    assert "Proof: probed: probed says no" in q and "observed: observed says no" in q
    # the author answers on the material: the row (and the question) is gone, the value is
    # labelled author, and the unit's siblings share it
    raw["home_model"]["materials"]["color_bulb"]["cct_range_k"] = [2000, 6500]
    answered = with_fixture_gamut(
        from_dict(raw), "den_pendant_1", declined("probed", "declared", "observed")
    )
    assert [r for r in cct_credibility(answered) if "den_pendant_1" in r.fixtures] == []
    from pascl.model import fixture_cct_range, fixture_cct_sources

    assert fixture_cct_range(answered, "den_pendant_1") == (2000, 6500)
    assert fixture_cct_sources(answered, "den_pendant_1") == ("author", "author")
