"""The report-cadence estimator on synthetic fixtures shaped like the measured ones.

The vectors are the reference fleet's shapes in miniature: a bulb that reports a moving colour
every 10.04 s with a first report at once when it has been idle, a sconce family at 10.38 s,
a strip that reports nothing but the transport's echo, and the must-decline cases the design
names (too few gaps, no colour reports, gaps that do not cluster). The jitter and the replay
are checked against hand-built trips so a change to either arithmetic is caught here before
it reaches a derivation from real data.
"""

from __future__ import annotations

import pytest

from pascl.estimator.reporting import (
    CLUSTER_MIN,
    MIN_GAPS,
    MIN_TRIPS,
    Fade,
    ReportInterval,
    Trip,
    estimate,
    gaps_of,
    inherit,
    jitter,
    predict,
    replay,
    residuals,
    stands_down,
)


def fade(fixture: str, interval: float, transition: float = 30.0, first: float = 1.4) -> Fade:
    """A fixture reporting every ``interval`` from its first report at ``first`` until the
    settling report just after the transition's end."""
    times = [first]
    while times[-1] + interval <= transition + 0.6:
        times.append(round(times[-1] + interval, 3))
    return Fade(fixture, transition, tuple(times))


def test_gaps_are_between_reports_inside_the_fade_only() -> None:
    f = Fade("a", 30.0, (-12.0, 1.4, 11.44, 21.48, 30.6, 45.0))
    # the pre-command report and the one long after the fade are not cadence evidence
    assert gaps_of(f) == pytest.approx([10.04, 10.04, 9.12])


def test_a_regular_reporter_is_measured_at_its_median_gap() -> None:
    fades = [fade("strip", 10.04) for _ in range(12)]
    got = estimate(fades)["strip"]
    assert got.source == "measured"
    assert got.seconds == pytest.approx(10.04, abs=0.01)
    assert got.gaps >= MIN_GAPS
    assert got.on_cadence == 1.0


def test_too_few_gaps_is_declined_not_guessed() -> None:
    got = estimate([fade("bulb", 10.04)] * 3)["bulb"]
    assert got.source == "none"
    assert got.seconds is None
    assert got.note is not None and got.note.startswith("too few gaps")


def test_no_colour_reports_is_declined_by_name() -> None:
    got = estimate([Fade("blind", 30.0, ()) for _ in range(20)])["blind"]
    assert got.source == "none"
    assert got.note == "no colour reports"


def test_interleaved_cadences_do_not_cluster_and_are_declined() -> None:
    # 5 s, 10 s and 20 s gaps in equal measure: no single interval describes the device
    times = [0.5]
    for step in (5.0, 10.0, 20.0) * 12:
        times.append(times[-1] + step)
    f = Fade("mix", 500.0, tuple(times))
    got = estimate([f])["mix"]
    assert got.source == "none"
    assert got.on_cadence is not None and got.on_cadence < CLUSTER_MIN
    assert got.note is not None and got.note.startswith("gaps do not cluster")


def test_an_occasional_missed_slot_does_not_unseat_the_interval() -> None:
    # one gap in twenty is a doubled slot (19.3 s on the reference fleet, one fade in ~1,300)
    times = [1.4]
    for i in range(60):
        times.append(times[-1] + (20.08 if i % 20 == 19 else 10.04))
    got = estimate([Fade("bulb", 700.0, tuple(times))])["bulb"]
    assert got.source == "measured"
    assert got.seconds == pytest.approx(10.04, abs=0.01)
    assert got.p90_s == pytest.approx(10.04, abs=0.01)


def test_inherit_seeds_from_the_model_label_and_names_the_source() -> None:
    measured = estimate(
        [fade("sconce_2", 10.38) for _ in range(12)] + [fade("sconce_1", 10.38) for _ in range(12)]
    )
    labels = {"sconce_1": "LCA001", "sconce_2": "LCA001", "sconce_3": "LCA001", "other": "LST002"}
    got = inherit(measured, labels, ["sconce_1", "sconce_2", "sconce_3", "other"])
    assert got["sconce_1"].source == "measured"
    assert got["sconce_3"].source == "inherited"
    assert got["sconce_3"].seconds == pytest.approx(10.38, abs=0.01)
    assert got["sconce_3"].inherited_from == "sconce_1"
    assert got["other"].source == "none"
    assert got["other"].seconds is None


def test_inherit_refuses_a_label_whose_units_disagree() -> None:
    measured = estimate(
        [fade("u1", 10.0) for _ in range(12)]
        + [fade("u2", 12.0, transition=60.0) for _ in range(12)]
    )
    got = inherit(measured, {"u1": "X", "u2": "X", "u3": "X"}, ["u1", "u2", "u3"])
    assert got["u3"].source == "none"
    assert got["u3"].note is not None and "disagree" in got["u3"].note


def test_inherit_keeps_a_declined_fixtures_reason_when_nothing_seeds_it() -> None:
    measured = estimate([fade("lonely", 10.0)])
    got = inherit(measured, {"lonely": "Z"}, ["lonely"])
    assert got["lonely"].source == "none"
    assert got["lonely"].note is not None and got["lonely"].note.startswith("too few gaps")


def test_prediction_is_fade_end_or_next_report_whichever_is_later() -> None:
    t = Trip("a", fade_end_s=30.0, tripped_s=31.0, last_report_s=21.5, settled_s=31.6)
    assert predict(t, 10.04) == pytest.approx(31.54)
    assert predict(t, None) == 30.0
    early = Trip("a", 30.0, 31.0, 15.0, 30.4)
    assert predict(early, 10.04) == 30.0


def trips() -> list[Trip]:
    """Twenty-five race-loss trips (the sensor tripped at 31 s, the settling report came
    later): the report lands 0.0-0.3 s beyond the phase prediction, one of them 3.9 s beyond
    (a host stall), and one before it."""
    out = []
    for i in range(25):
        last = 22.0 if i == 7 else 21.2 + (i % 7) * 0.2
        beyond = 3.9 if i == 13 else (-0.5 if i == 7 else 0.3 * (i % 4) / 3)
        out.append(Trip("bulb", 30.0, 31.0, last, max(30.0, last + 10.0) + beyond))
    assert all(t.settled_s > t.tripped_s for t in out)
    return out


def intervals() -> dict[str, ReportInterval]:
    return {"bulb": ReportInterval("bulb", 10.0, "measured", gaps=100)}


def test_residuals_never_go_negative() -> None:
    res = residuals(trips(), intervals())
    assert min(res) == 0.0
    assert max(res) == pytest.approx(3.9)


def test_jitter_is_the_tail_of_the_residual_and_declines_on_too_few_trips() -> None:
    j = jitter(trips(), intervals())
    assert j.trips == 25
    assert j.seconds == pytest.approx(3.9)
    assert j.residual_max_s == pytest.approx(3.9)
    few = jitter(trips()[: MIN_TRIPS - 1], intervals())
    assert few.seconds is None
    assert few.note is not None and few.note.startswith("too few trips")


def test_replay_counts_false_arms_and_the_wait_paid() -> None:
    ts = trips()
    at_zero = replay(ts, intervals(), 0.0)
    # without a margin every trip whose report landed past the prediction still arms early
    assert at_zero.false_arms == sum(1 for r in residuals(ts, intervals()) if r > 0)
    at_j = replay(ts, intervals(), 3.9)
    assert at_j.false_arms == 0
    assert 0 < at_j.wait_p50_s < 5.0
    assert at_j.arm_max_s == pytest.approx(max(predict(t, 10.0) for t in ts) + 3.9)
    unknown = replay(ts, {}, None)
    # an unknown interval and jitter arm at the fade end, which these trips have already
    # passed: no wait, and exactly today's false arms (every report that lands after the trip)
    assert unknown.wait_max_s == 0.0
    assert unknown.false_arms == sum(1 for t in ts if t.settled_s > t.tripped_s)


def test_stand_down_regime_is_named_from_the_helpers() -> None:
    assert not stands_down(30.0, 60.0, 10.0, 3.9)
    assert stands_down(46.0, 60.0, 10.0, 3.9)
    assert stands_down(59.0, 60.0, None, None)
    assert not stands_down(30.0, 60.0, None, None)
