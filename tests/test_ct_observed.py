"""The observed colour-temperature range: what it publishes, and what it declines.

DERIVED_PARAMETERS P8: the suite carries cases whose right answer is a decline, each kind by
name, and one fixture of real data (the reference installation's echoing channel, which is
itself a decline and must stay one)."""

from __future__ import annotations

import json
import random
from pathlib import Path

from pascl.estimator.ct_observed import (
    AGREE_TOL,
    MIN_SUPPORT,
    EndVerdict,
    apply,
    observe,
)
from pascl.estimator.gamut import ct_declines, inherit_ct
from pascl.model import CtDecline, Gamut, load, unobservable, untried, with_fixture_gamut

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "demo_home.yaml"
ECHO = ROOT / "tests" / "fixtures" / "ct_pairs_echo.json"
TRIANGLE = ((0.6915, 0.3083), (0.17, 0.7), (0.1532, 0.0475))


def _device(
    commands: list[int], floor: int = 500, ceiling: int = 153, noise: int = 0
) -> list[tuple[int, int]]:
    """A device whose channel carries the emitter's value: the command clamped to
    [ceiling, floor] mireds, with +-noise mireds of quantisation."""
    rng = random.Random(7)
    out = []
    for c in commands:
        r = min(max(c, ceiling), floor)
        if noise:
            r += rng.randint(-noise, noise)
        out.append((c, r))
    return out


def _arc() -> list[int]:
    """A night's worth of commands: the arc from 300 to 680 mired and back, a step per tick."""
    return [*range(300, 681, 4), *range(680, 299, -4)]


def test_a_reporting_channel_yields_both_ends_with_their_support() -> None:
    pairs = _device(_arc() + list(range(150, 100, -5)), noise=1)
    v = observe(pairs)
    assert v.floor.published and v.floor.kelvin == 2000 and v.floor.mired == 500
    assert v.ceiling.published and v.ceiling.kelvin == 6535 and v.ceiling.mired == 153
    # authority is the pairs that showed the clip, not the pairs seen (P7)
    assert v.floor.support == sum(1 for c, r in pairs if c - r > AGREE_TOL * r)
    assert v.floor.support >= MIN_SUPPORT and v.floor.contradictions == 0
    assert v.pairs == len(pairs) and v.commanded_mired == (105, 680)
    assert v.range_k == (2000, 6535)


def test_the_floor_alone_when_the_arc_never_reaches_the_ceiling() -> None:
    v = observe(_device(_arc()))
    assert v.floor.published and v.floor.kelvin == 2000
    assert not v.ceiling.published and v.ceiling.reason is not None
    assert "no clip observed at the ceiling" in v.ceiling.reason
    assert "coolest command, 300 mired" in v.ceiling.reason
    assert "channel repeats the command" in v.ceiling.reason
    # an end the commands never reached is inconclusive, never a proof (P2)
    assert not v.ceiling.proof and v.ceiling.verdict == "inconclusive"


def test_an_echoing_channel_is_proven_only_past_any_emitters_end() -> None:
    """Reports repeating commands inside the credible band could be a wide emitter; the same
    reports past it (warmer than 1500 K) can only be an echo."""
    inside = [(c, c) for c in range(300, 660, 4)]
    v = observe(inside)
    assert not v.floor.proof and "either the floor lies beyond it" in (v.floor.reason or "")
    past = [*inside, *((c, c) for c in range(660, 700, 4))]
    v = observe(past)
    assert v.floor.proof and v.floor.verdict == "unobservable"
    assert "lies past any emitter's floor" in (v.floor.reason or "")
    assert not v.ceiling.proof


def test_the_real_data_fixture_declines_at_both_ends() -> None:
    """The reference installation: every report repeats the command (the attribute is stored
    unclipped and the native push carries it), so the warmest command of the night arc was
    reported as sent and nothing can be inferred. This must stay a decline."""
    record = json.loads(ECHO.read_text(encoding="utf-8"))
    pairs = [(int(c), int(r)) for c, r in record["pairs"]]
    assert len(pairs) > 500 and max(c for c, _ in pairs) >= 650
    v = observe(pairs)
    assert not v.floor.published and not v.ceiling.published
    # a handful of reports differ from their command (a stale push landing after the next
    # command), scattered, and the hundreds commanded past them contradict every one; the
    # commands ran past any emitter's floor (1000 mired) and ceiling (50 mired) and were
    # reported as sent, which PROVES the channel repeats the command
    assert v.floor.support < MIN_SUPPORT and v.floor.contradictions > 100 * max(v.floor.support, 1)
    assert v.floor.proof and v.floor.verdict == "unobservable"
    assert v.floor.reason is not None and "the channel repeats the command" in v.floor.reason
    assert v.ceiling.proof and v.ceiling.verdict == "unobservable"
    assert v.range_k == (None, None)


def test_too_few_clipped_pairs_decline() -> None:
    pairs = _device([*range(300, 500, 10), 520, 560, 600])
    v = observe(pairs)
    assert not v.floor.published and v.floor.support == 3 and v.floor.mired == 500
    assert (
        v.floor.reason is not None
        and f"among 3 commanded past it; {MIN_SUPPORT} are needed" in v.floor.reason
    )


def test_disagreeing_clipped_reports_decline() -> None:
    # a channel that sometimes carries the emitter and sometimes something else
    pairs = _device(_arc())
    wobble = [(c, r if i % 3 else r - 40) for i, (c, r) in enumerate(pairs)]
    v = observe(wobble)
    assert not v.floor.published and v.floor.reason is not None
    assert "clipped reports disagree" in v.floor.reason and v.floor.disagreeing > 0


def test_contradicting_reports_decline() -> None:
    # every fourth report past the floor repeats the command (an echo that got through)
    pairs = _device(_arc())
    leaky = [(c, c if (c > 520 and i % 4 == 0) else r) for i, (c, r) in enumerate(pairs)]
    v = observe(leaky)
    assert not v.floor.published and v.floor.reason is not None
    assert "warmer than the candidate floor" in v.floor.reason and v.floor.contradictions > 0
    # one stray echo is tolerated, and counted
    one = [(c, c if i == 60 else r) for i, (c, r) in enumerate(pairs)]
    v = observe(one)
    assert v.floor.published and v.floor.contradictions == 1


def test_a_value_no_emitter_could_have_declines() -> None:
    pairs = _device(list(range(300, 900, 5)), floor=800)  # 1250 K
    v = observe(pairs)
    assert not v.floor.published and v.floor.reason is not None
    assert "not one an emitter could have" in v.floor.reason and v.floor.mired == 800


def test_no_pairs_is_its_own_decline() -> None:
    v = observe([])
    assert v.pairs == 0 and v.commanded_mired is None
    assert v.floor.reason == "no pairs" and v.ceiling.reason == "no pairs"
    assert isinstance(v.floor, EndVerdict)


def test_apply_fills_weaker_ends_and_never_a_probed_one() -> None:
    v = observe(_device(_arc()))  # floor 2000 K, ceiling declined (inconclusive)
    # nothing known: the floor lands, the ceiling stays unbounded with its decline recorded
    g, notes = apply(Gamut(TRIANGLE), v)
    assert g.ct_range_k == (2000, 20000) and g.ct_sources == ("observed", None)
    assert any(n.startswith("floor: observed 2000 K") for n in notes)
    assert any(n.startswith("ceiling: inconclusive: no clip observed") for n in notes)
    assert [(d.tier, d.end, d.verdict) for d in g.ct_declined] == [
        ("observed", "ceiling", "inconclusive")
    ]
    # a declared range: the floor is replaced, the declared ceiling kept
    declared = Gamut(TRIANGLE, ct_range_k=(1800, 6535), ct_sources=("declared", "declared"))
    g, _ = apply(declared, v)
    assert g.ct_range_k == (2000, 6535) and g.ct_sources == ("observed", "declared")
    # a probed floor stands, with a note when the observation disagrees
    probed = Gamut(TRIANGLE, ct_range_k=(2200, 6535), ct_sources=("probed", "probed"))
    g, notes = apply(probed, v)
    assert g.ct_range_k == probed.ct_range_k and g.ct_sources == probed.ct_sources
    assert any("probed value 2200 K stands" in n for n in notes)
    # a declined verdict changes no end, and records the declines (replacing earlier observed
    # ones, keeping the other tiers')
    earlier = Gamut(
        TRIANGLE,
        ct_declined=(
            CtDecline("probed", "floor", "unobservable", "own value"),
            CtDecline("observed", "floor", "inconclusive", "old"),
        ),
    )
    g, notes = apply(earlier, observe([]))
    assert g.ct_range_k is None and notes == [
        "floor: inconclusive: no pairs",
        "ceiling: inconclusive: no pairs",
    ]
    assert [(d.tier, d.end, d.reason) for d in g.ct_declined] == [
        ("probed", "floor", "own value"),
        ("observed", "floor", "no pairs"),
        ("observed", "ceiling", "no pairs"),
    ]


def test_the_real_data_fixture_leaves_a_proof_of_unobservability() -> None:
    """The product state the reference installation is in: probes answered their own values,
    the declaration is the attribute's span, the channel repeated commands past any emitter's
    ends. All three tiers declined with proof, so the end is UNOBSERVABLE, the one state in
    which the model asks the author; a fixture with a tier untried is merely unknown."""
    record = json.loads(ECHO.read_text(encoding="utf-8"))
    pairs = [(int(c), int(r)) for c, r in record["pairs"]]
    measured = Gamut(TRIANGLE, ct_declined=ct_declines({50: 50, 1000: 1000}, (50, 1000)))
    assert unobservable(measured) == (False, False)
    assert untried(measured) == (("observed",), ("observed",))
    g, _ = apply(measured, observe(pairs))
    assert g.ct_range_k is None and unobservable(g) == (True, True)
    assert untried(g) == ((), ())
    assert {(d.tier, d.verdict) for d in g.ct_declined} == {
        ("probed", "unobservable"),
        ("declared", "unobservable"),
        ("observed", "unobservable"),
    }
    # the arc alone (inside the band) leaves the observed tier inconclusive: not a proof
    inside = Gamut(TRIANGLE, ct_declined=measured.ct_declined)
    g2, _ = apply(inside, observe([(c, c) for c in range(300, 640, 4)]))
    assert unobservable(g2) == (False, False) and untried(g2) == (("observed",), ("observed",))


def test_inherit_ct_fills_unbounded_ends_from_a_sibling() -> None:
    model = load(EXAMPLE.read_text(encoding="utf-8"))
    observed = with_fixture_gamut(
        model,
        "den_pendant_1",
        Gamut(TRIANGLE, ct_range_k=(1996, 20000), ct_sources=("observed", None)),
    )
    bare = with_fixture_gamut(observed, "den_pendant_2", Gamut(TRIANGLE))
    seeded, applied = inherit_ct(bare)
    assert [(s.fixture, s.source, s.end, s.kelvin) for s in applied] == [
        ("den_pendant_2", "den_pendant_1", "floor", 1996)
    ]
    g = seeded.rooms["den"].fixtures["den_pendant_2"].gamut
    assert g is not None and g.ct_range_k == (1996, 20000)
    assert g.ct_sources == ("inherited", None) and g.inherited_from is None
    # idempotent, and a source of its own is never overwritten
    again, applied = inherit_ct(seeded)
    assert applied == [] and again == seeded
    own = with_fixture_gamut(
        seeded,
        "den_pendant_2",
        Gamut(TRIANGLE, ct_range_k=(2100, 6535), ct_sources=("probed", "probed")),
    )
    same, applied = inherit_ct(own)
    assert applied == [] and same == own
