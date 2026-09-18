"""Gamut measurement: the calibration that gives a fixture its polygon.

The gamut is measured, never looked up: the probe commands colours far outside any real
emitter's gamut, with no transition, and reads back what the device says it now shows. The
convex hull of those answers is the polygon. A three-primary emitter measures as a triangle, a
multi-primary one as a larger hull, and a device that answers nothing but the transport's echo
of the command measures nothing at all and stays unmeasured rather than borrowing a table.

This module is the pure half of that procedure: WHICH colours to command, in what order, and
what the answers add up to, including HOW the device clips (every rule in ``CLIP_RULES`` is
fitted to the answers and the one that reproduces them is recorded, so a comparator predicts
where an unreachable command lands rather than assuming the nearest point). The other half,
publishing a colour and collecting the device's answer, is transport work and lives in an
adapter. The split is what makes the calibration brand-agnostic and testable: a simulated
device that clips to a hidden polygon must be recovered exactly, and the real one is recovered
through whatever channel it offers (a spontaneous attribute report, a read, or both).

A measurement also travels: two fixtures whose materials carry the same model label are the
same hardware by the author's declaration, so a measured polygon seeds the unmeasured ones as
``inherited``, exact from their first minute, and each fixture's own measurement later confirms
or, loudly, contradicts the seed (``seeds``, ``inherit``, ``confirm_seed``). On the reference
installation every unit of every label measured the identical polygon, so a seed has never
been wrong there; it is still a seed, because the measurement is cheap and the label is only
a label.

Things learned measuring the reference installation's fleet (every one of them shapes the plan
below): a device reports an attribute only when it changes, so consecutive probes must clip to
different places; a device may report at most once every ~10 s, or never, so the adapter reads
the value back rather than waiting for it; the transport echoes the commanded value before the
device answers, and that echo is recognised by value, never trusted; on a multi-endpoint device
the read lands under the transport's unsuffixed key while the endpoint key keeps the echo; and
the (x, 0.02) corner points are what reach a blue vertex, because the blue-green edge is nearly
vertical and (0.02, y) clips onto it instead.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from pascl.core.color import mired_to_kelvin
from pascl.core.gamut import (
    CLIP_RULES,
    XY,
    ClipRule,
    Polygon,
    clip,
    convex_hull,
    inside,
    polygon_problems,
    project,
)
from pascl.model import KELVIN_MAX, KELVIN_MIN, Gamut, HomeModel, with_fixture_gamut
from pascl.model.geometry import MAX_VERTICES, SAME_MODEL_FAIL, vertex_deviation

#: Far-outside valid chromaticities (x, y >= 0.02, x + y <= 0.99), ordered so consecutive
#: points clip to different regions (red, green, blue interleaved). x and y stay clear of 0
#: because a transport may derive an optimistic HS/RGB from xy before the device answers and
#: divide by y on the way.
FAR_POINTS: Final[tuple[XY, ...]] = (
    (0.95, 0.04),
    (0.04, 0.94),
    (0.02, 0.02),
    (0.75, 0.24),
    (0.15, 0.84),
    (0.22, 0.02),
    (0.90, 0.09),
    (0.35, 0.64),
    (0.02, 0.12),
    (0.80, 0.02),
    (0.02, 0.70),
    (0.10, 0.02),
    (0.60, 0.39),
    (0.55, 0.44),
    (0.02, 0.40),
    (0.50, 0.02),
    (0.30, 0.02),
    (0.08, 0.25),
)
#: How far beyond each hull vertex the vertex-refining probe goes.
VERTEX_PUSH: Final = 0.05
#: How far outside each edge midpoint the model-validation probe goes.
EDGE_PUSH: Final = 0.008
#: A reported value within this of the command on both axes is the transport's echo.
ECHO_TOL: Final = 5e-4
#: A device whose clip differs from the closest-point model by more than this per axis on an
#: edge probe clips by some other rule; the polygon is still its hull, but the mismatch is
#: surfaced rather than modelled away.
MODEL_ERROR_LIMIT: Final = 0.002
#: An edge whose probe answer lands further than this beyond the chord hides a vertex worth
#: another probe; below it the chord reproduces the device to a third of the comparators'
#: 0.003 tolerance and is accepted as the boundary.
REFINE_TOL: Final = 0.001
#: How many times the hull may grow by probing bulging edges.
MAX_REFINE_ROUNDS: Final = 3
#: How far beyond an implied corner the corner probe is placed.
CORNER_PUSH: Final = 0.02
#: A seed (a polygon inherited from a same-model fixture) that the fixture's own measurement
#: contradicts by more than this per vertex was not a stand-in but a wrong polygon: the
#: comparators' tolerance is of this order, so the seed would have moved their judgement.
SEED_TOL: Final = 0.003
#: Two measured units of one model label further apart than this are worth a reader's look
#: (the model refuses them only beyond ``SAME_MODEL_FAIL``).
SAME_MODEL_NOTE: Final = 0.005
#: An answer further inside the final hull than this is not the clip of an outside command
#: (every rule lands on the boundary): it is a probe the device reached, or a foreign colour
#: that arrived during the sample. Either way it says nothing about the clip rule.
INTERIOR_TOL: Final = 0.002
#: An answer whose gap from a rule's prediction exceeds both this and ``OUTLIER_FACTOR``
#: times the rule's median gap is one the rule cannot account for and is set aside.
OUTLIER_FLOOR: Final = 0.005
OUTLIER_FACTOR: Final = 10.0
#: The colour temperatures commanded to find a device's range, in mireds, coolest first:
#: past any real device's coolest (50 mired, 20000 K) and warmest (1000 mired, 1000 K), so
#: the device clips to its physical limits and answers them. Coolest first because a fixture
#: at rest is far more often at its warm end (the night floor) than at its cool one, and a
#: probe answered with the resting value is indistinguishable from one not applied.
CT_PROBES_MIRED: Final[tuple[int, int]] = (50, 1000)

Kind = Literal["far", "vertex", "edge"]


@dataclass(frozen=True)
class Step:
    """One colour to command with a zero transition, and why."""

    kind: Kind
    xy: XY


@dataclass(frozen=True)
class RuleFit:
    rule: ClipRule
    error: float
    """Worst per-axis gap between the rule's prediction and the device, over the answers
    consistent with the rule."""
    outliers: int
    """Answers the rule cannot account for at all (see ``OUTLIER_FLOOR``)."""


@dataclass(frozen=True)
class Verdict:
    polygon: Polygon | None
    """The measured gamut, or None when the device answered too little to form one."""
    model_error: float | None
    """Worst per-axis gap between the device's answers and ``clip_rule`` applied to the
    polygon, over every answered probe; None when no edge probe was answered (the far and
    vertex answers ARE the hull, so only an edge probe tests the rule and the polygon is
    unverified without one)."""
    device_reports: int
    unanswered: int
    notes: tuple[str, ...]
    clip_rule: ClipRule = "closest"
    """The rule that reproduces the device's answers best; see :func:`fit_clip_rule`."""
    fits: tuple[RuleFit, ...] = ()
    """Every rule against the same answers, best first; empty when unverified."""
    answers: tuple[tuple[Step, XY], ...] = ()
    """The evidence: each answered probe with the device's own value."""
    aborted: str | None = None
    """Why the measurement was cut short, when it was; the polygon is then withheld."""
    ct_range_k: tuple[int, int] | None = None
    """The colour temperatures the device can show, kelvin (floor, ceiling), from the two
    probes in ``CT_PROBES_MIRED``; None when not measured or declined (``ct_range_from``)."""
    ct_answers: tuple[tuple[int, int | None], ...] = ()
    """The evidence: each colour-temperature probe (mireds) with the device's answer."""


def ct_range_from(answers: Mapping[int, int | None]) -> tuple[int, int] | None:
    """The device's colour-temperature range, kelvin (floor, ceiling), from its answers
    (mireds) to the probes in ``CT_PROBES_MIRED``. Declined, None, when a probe went
    unanswered, when the device answered a probe with the probe's own value (the echo, or a
    device that claims 1000-20000 K, neither a physical limit), or when the pair is not an
    ordered range inside ``KELVIN_MIN``-``KELVIN_MAX``. Kelvin are truncated the way a host
    converts them, so the range compares exactly with what the host reports."""
    cool_probe, warm_probe = CT_PROBES_MIRED
    coolest, warmest = answers.get(cool_probe), answers.get(warm_probe)
    if coolest is None or warmest is None:
        return None
    if coolest == cool_probe or warmest == warm_probe:
        return None
    if coolest <= 0 or warmest <= 0:
        return None
    floor_k, ceiling_k = mired_to_kelvin(warmest), mired_to_kelvin(coolest)
    if not KELVIN_MIN <= floor_k < ceiling_k <= KELVIN_MAX:
        return None
    return (floor_k, ceiling_k)


def is_echo(command: XY, reported: XY, tol: float = ECHO_TOL) -> bool:
    """A value the transport handed back that merely repeats the command is not the device."""
    return abs(reported[0] - command[0]) < tol and abs(reported[1] - command[1]) < tol


def fit_clip_rule(answers: Iterable[tuple[XY, XY]], poly: Sequence[XY]) -> tuple[RuleFit, ...]:
    """Every clip rule against the device's answers, best first: for each rule the worst
    per-axis gap between what it predicts for a command and what the device answered. Ties
    go to ``closest``; ``rgb_clamp`` is fitted only to a triangle, the only shape it is
    defined on. The polygon is the same under every rule (the answers are on its boundary
    whatever the rule); what differs is WHERE on the boundary an unreachable command lands,
    which is what a comparator has to predict.

    The fit is robust to a few bad answers: a gap more than ``OUTLIER_FACTOR`` times the
    rule's median gap (and above ``OUTLIER_FLOOR``) is an answer the rule cannot account
    for, a stale report or a colour from elsewhere that the sampling let through, and it is
    counted rather than allowed to pick the rule. A device that really follows another rule
    disagrees with the wrong one on most answers, so its median is large and nothing is
    trimmed away from that disagreement."""
    pairs = list(answers)
    out: list[RuleFit] = []
    for rule in CLIP_RULES:
        if rule == "rgb_clamp" and len(poly) != 3:
            continue
        gaps = sorted(
            max(abs(pr[0] - got[0]), abs(pr[1] - got[1]))
            for cmd, got in pairs
            for pr in (clip(cmd, poly, rule),)
        )
        if not gaps:
            out.append(RuleFit(rule, 0.0, 0))
            continue
        median = gaps[len(gaps) // 2]
        limit = max(OUTLIER_FLOOR, OUTLIER_FACTOR * median)
        kept = [g for g in gaps if g <= limit]
        out.append(RuleFit(rule, max(kept) if kept else 0.0, len(gaps) - len(kept)))
    out.sort(key=lambda f: (f.error, f.outliers, CLIP_RULES.index(f.rule)))
    return tuple(out)


def _clamp_xy(x: float, y: float) -> XY:
    return (round(max(0.0, min(0.98, x)), 4), round(max(0.0, min(0.98, y)), 4))


def _interior(p: XY, poly: Sequence[XY]) -> bool:
    """Inside the polygon and further than ``INTERIOR_TOL`` from every edge."""
    if not inside(p, poly):
        return False
    n = len(poly)
    best = math.inf
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        vx, vy = b[0] - a[0], b[1] - a[1]
        den = vx * vx + vy * vy
        u = ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / den if den > 0 else 0.0
        u = max(0.0, min(1.0, u))
        best = min(best, math.dist(p, (a[0] + u * vx, a[1] + u * vy)))
    return best > INTERIOR_TOL


def outward_steps(
    poly: Sequence[XY], vertex_push: float = VERTEX_PUSH, edge_push: float = EDGE_PUSH
) -> tuple[Step, ...]:
    """Probes just beyond each vertex (along the ray from the centroid, to refine it) and just
    outside each edge midpoint (along the outward normal, to validate the clip model)."""
    n = len(poly)
    cx = sum(p[0] for p in poly) / n
    cy = sum(p[1] for p in poly) / n
    out: list[Step] = []
    for vx, vy in poly:
        dx, dy = vx - cx, vy - cy
        d = math.hypot(dx, dy) or 1.0
        out.append(Step("vertex", _clamp_xy(vx + vertex_push * dx / d, vy + vertex_push * dy / d)))
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        nx, ny = b[1] - a[1], -(b[0] - a[0])  # counter-clockwise polygon: (dy, -dx) is outward
        d = math.hypot(nx, ny) or 1.0
        out.append(Step("edge", _clamp_xy(mx + edge_push * nx / d, my + edge_push * ny / d)))
    return tuple(out)


class Probe:
    """The measurement as a pure state machine.

    The adapter loops: ``step = probe.next()``; command ``step.xy`` with a zero transition;
    collect the device's answer (its own report of what it now shows, or None when nothing but
    the echo came back); ``probe.answer(step, reported)``. When ``next()`` returns None,
    ``verdict()`` holds the polygon.

    Passes: the far points form a first hull; one probe beyond each of its vertices refines
    them; one just outside each edge midpoint checks that the device clips to the closest
    point of that edge. An edge the device does NOT clip to (its answer lands beyond the chord)
    is a corner the far points missed, a fourth or fifth primary: a probe pushed well out along
    that edge's normal finds the true vertex, the hull grows, and its new edges are checked in
    turn, until every edge is confirmed or the vertex ceiling is reached.
    """

    def __init__(self, far_points: Iterable[XY] = FAR_POINTS) -> None:
        self._far = tuple(far_points)
        self.reset()

    def reset(self) -> None:
        """Start over with the same far points: nothing answered, nothing noted. The adapter
        uses it when the device's circumstances change part-way (switched on to be measured
        lit, say) and what came before is not evidence about what follows."""
        self._queue: list[Step] = [Step("far", xy) for xy in self._far]
        self._phase: Literal["far", "vertices", "edges", "done"] = "far"
        self._answers: list[tuple[Step, XY]] = []
        self._edge_answers: dict[tuple[XY, XY], XY] = {}
        self._edge_unanswered: set[tuple[XY, XY]] = set()
        self._rounds = 0
        self._unanswered = 0
        self._notes: list[str] = []
        self._last: XY | None = None

    # -- geometry of the current estimate ----------------------------------------------------

    def _hull(self) -> Polygon:
        return convex_hull(xy for s, xy in self._answers if s.kind in ("far", "vertex"))

    @staticmethod
    def _edges(hull: Polygon) -> list[tuple[XY, XY]]:
        return [(hull[i], hull[(i + 1) % len(hull)]) for i in range(len(hull))]

    @staticmethod
    def _edge_probe(a: XY, b: XY, push: float) -> XY:
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        nx, ny = b[1] - a[1], -(b[0] - a[0])  # counter-clockwise polygon: (dy, -dx) is outward
        d = math.hypot(nx, ny) or 1.0
        return _clamp_xy(mx + push * nx / d, my + push * ny / d)

    def _corner_probe(self, hull: Polygon, i: int) -> XY | None:
        """Where a hidden vertex behind hull edge i must be, if the two neighbouring edges are
        true edges of the device's gamut: the point where their lines meet, pushed a little
        further out so the probe lands in that vertex's own wedge and the device answers with
        the vertex itself. None when the neighbours do not meet outside the hull."""
        n = len(hull)
        a, b = hull[i], hull[(i + 1) % n]
        p, q = hull[i - 1], hull[(i + 2) % n]
        d1 = (a[0] - p[0], a[1] - p[1])
        d2 = (b[0] - q[0], b[1] - q[1])
        den = d1[0] * d2[1] - d1[1] * d2[0]
        if abs(den) < 1e-12:
            return None
        t = ((q[0] - p[0]) * d2[1] - (q[1] - p[1]) * d2[0]) / den
        x = (p[0] + t * d1[0], p[1] + t * d1[1])
        if inside(x, hull) or not (
            0.0 <= x[0] <= 1.0 and 0.0 <= x[1] <= 1.0 and x[0] + x[1] <= 1.0
        ):
            return None
        # Along the bisector of the two edges' outward normals: the only direction from the
        # vertex that is guaranteed to lie in its own closest-point wedge, whatever its angle.
        # Counter-clockwise polygon: (dy, -dx) of an edge's direction points outward.
        e2 = (q[0] - b[0], q[1] - b[1])  # edge b -> q in hull order
        n1 = (d1[1], -d1[0])
        n2 = (e2[1], -e2[0])
        l1 = math.hypot(*n1) or 1.0
        l2 = math.hypot(*n2) or 1.0
        bx, by = n1[0] / l1 + n2[0] / l2, n1[1] / l1 + n2[1] / l2
        d = math.hypot(bx, by) or 1.0
        return _clamp_xy(x[0] + CORNER_PUSH * bx / d, x[1] + CORNER_PUSH * by / d)

    def _edge_error(self, edge: tuple[XY, XY], hull: Polygon) -> float | None:
        """How far the device's answer to this edge's probe sits from the closest point of the
        hull, or None when the edge has not been probed since it appeared, or when the answer
        lies inside the hull (no clip of an outside command does: a colour from elsewhere
        reached the device, and the edge stays unverified rather than looking bulged)."""
        ans = self._edge_answers.get(edge)
        if ans is None or _interior(ans, hull):
            return None
        pr = project(self._edge_probe(*edge, EDGE_PUSH), hull)
        return max(abs(pr[0] - ans[0]), abs(pr[1] - ans[1]))

    # -- the protocol ------------------------------------------------------------------------

    def next(self) -> Step | None:
        while not self._queue and self._phase != "done":
            self._plan()
        return self._queue[0] if self._queue else None

    def _plan(self) -> None:
        hull = self._hull()
        if self._phase == "far":
            if len(hull) < 3:
                self._notes.append(f"hull degenerate after the far pass ({len(hull)} vertices)")
                self._phase = "done"
                return
            n = len(hull)
            cx = sum(v[0] for v in hull) / n
            cy = sum(v[1] for v in hull) / n
            for vx, vy in hull:
                dx, dy = vx - cx, vy - cy
                d = math.hypot(dx, dy) or 1.0
                self._queue.append(
                    Step("vertex", _clamp_xy(vx + VERTEX_PUSH * dx / d, vy + VERTEX_PUSH * dy / d))
                )
            self._phase = "edges"
            return
        if self._phase == "vertices":
            self._phase = "edges"
            return
        if self._phase == "edges":
            if len(hull) < 3:
                self._phase = "done"
                return
            unprobed = [
                e
                for e in self._edges(hull)
                if e not in self._edge_answers and e not in self._edge_unanswered
            ]
            if unprobed:
                self._queue.extend(
                    Step("edge", self._edge_probe(a, b, EDGE_PUSH)) for a, b in unprobed
                )
                return
            edges = self._edges(hull)
            bulging = [
                i
                for i, e in enumerate(edges)
                if (err := self._edge_error(e, hull)) is not None and err > REFINE_TOL
            ]
            if (
                bulging
                and self._rounds < MAX_REFINE_ROUNDS
                and len(hull) + len(bulging) <= MAX_VERTICES
            ):
                self._rounds += 1
                for i in bulging:
                    # aim at the corner the neighbouring edges imply; failing that, straight out
                    target = self._corner_probe(hull, i) or self._edge_probe(*edges[i], VERTEX_PUSH)
                    self._queue.append(Step("vertex", target))
                self._phase = "vertices"
                return
            if bulging:
                self._notes.append(
                    f"{len(bulging)} edge(s) still bulge after {self._rounds} refinement round(s)"
                )
            self._phase = "done"

    def answer(self, step: Step, reported: XY | None, *, trusted: bool = False) -> None:
        """Record the device's answer to the pending step. Two untrusted values are not
        evidence: one equal to the command is the transport's echo, and one equal to the
        previous answer is the device's late report of the colour it showed before (probes
        are ordered so that consecutive ones clip to different places). ``trusted`` lifts
        both: the adapter read the value back from the device, so equality with the command
        means the device REACHED it (a probe that fell inside the true gamut, as one outside
        a cut corner does), which is evidence about the polygon, not noise."""
        if not self._queue or self._queue[0] != step:
            raise ValueError(f"answer for {step} but the pending step is {self.next()}")
        self._queue.pop(0)
        why = self._not_evidence(step, reported, trusted)
        if why is not None or reported is None:
            self._unanswered += 1
            self._notes.append(f"{why} for {step.kind} probe {list(step.xy)}")
            if step.kind == "edge":
                # never ask the same edge again: an edge with no answer is unverified
                self._edge_unanswered.update(
                    e
                    for e in self._edges(self._hull())
                    if self._edge_probe(*e, EDGE_PUSH) == step.xy
                )
            return
        xy = (float(reported[0]), float(reported[1]))
        self._answers.append((step, xy))
        self._last = xy
        if step.kind == "edge":
            for e in self._edges(self._hull()):
                if self._edge_probe(*e, EDGE_PUSH) == step.xy:
                    self._edge_answers[e] = xy

    def _not_evidence(self, step: Step, reported: XY | None, trusted: bool) -> str | None:
        if reported is None:
            return "no device answer"
        if not trusted and is_echo(step.xy, reported):
            return "only the echo"
        if not trusted and self._last is not None and is_echo(self._last, reported):
            return "only the previous answer again (stale report)"
        return None

    def verdict(self) -> Verdict:
        hull = self._hull()
        notes = list(self._notes)
        answers = tuple(self._answers)
        if len(hull) < 3:
            return Verdict(
                None, None, len(answers), self._unanswered, tuple(notes), answers=answers
            )
        problems = polygon_problems(hull)
        if problems:
            notes.extend(f"hull rejected: {p}" for p in problems)
            return Verdict(
                None, None, len(answers), self._unanswered, tuple(notes), answers=answers
            )
        unverified = sum(1 for e in self._edges(hull) if self._edge_error(e, hull) is None)
        if not any(s.kind == "edge" for s, _ in answers):
            notes.append("no edge probe was answered; the clip model is unverified")
            return Verdict(
                hull, None, len(answers), self._unanswered, tuple(notes), answers=answers
            )
        if unverified:
            notes.append(f"{unverified} edge(s) unverified")
        on_boundary = [(s, xy) for s, xy in answers if not _interior(xy, hull)]
        foreign = sum(1 for s, xy in answers if _interior(xy, hull) and not is_echo(s.xy, xy))
        if foreign:
            notes.append(
                f"{foreign} answer(s) inside the hull ignored: a colour the measurement did "
                f"not command reached the device during those samples"
            )
        fits = fit_clip_rule(((s.xy, xy) for s, xy in on_boundary), hull)
        best = fits[0]
        if best.rule != "closest":
            others = ", ".join(f"{f.rule} {f.error:.4f}" for f in fits[1:])
            notes.append(
                f"device clips by the {best.rule} rule (residual {best.error:.4f}; {others})"
            )
        if best.outliers:
            notes.append(
                f"{best.outliers} answer(s) no rule accounts for ignored: a stale report or a "
                f"colour from elsewhere during those samples"
            )
        if best.error > MODEL_ERROR_LIMIT:
            notes.append(
                f"device clips {best.error:.4f} from every rule modelled; comparators are "
                f"approximate"
            )
        return Verdict(
            hull,
            best.error,
            len(answers),
            self._unanswered,
            tuple(notes),
            clip_rule=best.rule,
            fits=fits,
            answers=answers,
        )


# ---------------------------------------------------------------------------------------------
# Which fixture to measure next
# ---------------------------------------------------------------------------------------------

Status = Literal["measured", "inherited", "unmeasured", "rebound"]


@dataclass(frozen=True)
class FixtureStatus:
    room: str
    fixture: str
    status: Status
    label: str | None = None
    """The material's model label, the key a seed travels along."""


def statuses(model: HomeModel, device_ids: Mapping[str, str] | None = None) -> list[FixtureStatus]:
    """Every colour-capable fixture with its measurement state. ``device_ids`` maps fixture id
    to the transport's current identity for its device; a polygon bound to a different
    identity is ``rebound`` (the device was replaced) and wants measuring again. An
    ``inherited`` polygon is a seed copied from a same-model fixture, exact until the
    fixture's own measurement confirms or replaces it."""
    out: list[FixtureStatus] = []
    ids = device_ids or {}
    for rid, room in model.rooms.items():
        for fid, fx in room.fixtures.items():
            mat = model.materials.get(fx.material)
            if mat is None or "xy" not in mat.capabilities:
                continue
            g = fx.gamut
            status: Status
            if g is None:
                status = "unmeasured"
            elif g.bound_to is not None and fid in ids and ids[fid] != g.bound_to:
                status = "rebound"
            elif g.inherited_from is not None:
                status = "inherited"
            else:
                status = "measured"
            out.append(FixtureStatus(rid, fid, status, mat.model))
    return out


def pick_target(
    model: HomeModel,
    device_ids: Mapping[str, str] | None,
    lit: Collection[str],
    occupied_rooms: Collection[str],
    exclude: Collection[str] = (),
) -> FixtureStatus | None:
    """The fixture to measure now, or None: unmeasured before rebound before inherited (a
    seed is already exact, its confirmation can wait), then by room and name, skipping any
    fixture that is lit, whose room is occupied, or that the caller has set aside (one its
    transport cannot measure invisibly). Measuring a dark fixture in an empty room is
    invisible and self-restoring, which is what lets the runtime do this on its own, the way
    the reference installation's daylight calibrator takes its empty-and-dark windows."""
    order = {"unmeasured": 0, "rebound": 1, "inherited": 2}
    candidates = [
        s
        for s in statuses(model, device_ids)
        if s.status != "measured"
        and s.fixture not in lit
        and s.fixture not in exclude
        and s.room not in occupied_rooms
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda s: (order[s.status], s.room, s.fixture))


# ---------------------------------------------------------------------------------------------
# Seeds: a measured polygon travels to same-model fixtures with its provenance
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Seed:
    room: str
    fixture: str
    source: str
    """The measured fixture whose polygon is copied."""
    label: str
    """The material model label both carry."""
    refresh: bool = False
    """True when the fixture already held a copy that no longer matches its source."""


def _sources(model: HomeModel) -> dict[str, tuple[str, Gamut]]:
    """label -> (fixture, gamut) of the first measured (not inherited) fixture per label."""
    out: dict[str, tuple[str, Gamut]] = {}
    for room in model.rooms.values():
        for fid, fx in room.fixtures.items():
            mat = model.materials.get(fx.material)
            g = fx.gamut
            if mat is None or mat.model is None or g is None or g.inherited_from is not None:
                continue
            out.setdefault(mat.model, (fid, g))
    return out


def seeds(model: HomeModel, device_ids: Mapping[str, str] | None = None) -> list[Seed]:
    """What ``inherit`` would do: every unmeasured or rebound colour fixture whose material
    carries the model label of a measured fixture, and every inherited copy whose source has
    since changed. Deterministic: the source is the first measured fixture of the label in
    model order."""
    sources = _sources(model)
    out: list[Seed] = []
    for s in statuses(model, device_ids):
        if s.label is None or s.label not in sources:
            continue
        src_id, src = sources[s.label]
        if s.status in ("unmeasured", "rebound"):
            out.append(Seed(s.room, s.fixture, src_id, s.label))
        elif s.status == "inherited":
            g = model.rooms[s.room].fixtures[s.fixture].gamut
            assert g is not None
            if (
                g.inherited_from != src_id
                or g.vertices != src.vertices
                or g.clip_rule != src.clip_rule
            ):
                out.append(Seed(s.room, s.fixture, src_id, s.label, refresh=True))
    return out


def inherit(
    model: HomeModel, device_ids: Mapping[str, str] | None = None
) -> tuple[HomeModel, list[Seed]]:
    """The model with every seed applied: the source's polygon, rule and error, dated by the
    source's measurement, bound to the fixture's current device identity when known, and
    ``inherited_from`` naming the source. Nothing measured is touched, and a fixture whose
    label no measured fixture carries stays as it is (the identity, or its stale copy)."""
    ids = device_ids or {}
    sources = _sources(model)
    applied = seeds(model, device_ids)
    out = model
    for seed in applied:
        _src_id, src = sources[seed.label]
        out = with_fixture_gamut(
            out,
            seed.fixture,
            Gamut(
                vertices=src.vertices,
                bound_to=ids.get(seed.fixture),
                measured=src.measured,
                model_error=src.model_error,
                clip_rule=src.clip_rule,
                inherited_from=seed.source,
                firmware=None,
                ct_range_k=src.ct_range_k,
            ),
        )
    return out, applied


def confirm_seed(seed: Gamut, measured: Polygon) -> tuple[bool, float]:
    """Whether a fixture's own measurement agrees with the seed it held: the largest vertex
    gap, and True when it is within ``SEED_TOL``. A contradiction is loud by design: it means
    two fixtures the author called the same hardware are not."""
    gap = vertex_deviation(seed.vertices, measured)
    return gap <= SEED_TOL, gap


def consistency(model: HomeModel) -> list[str]:
    """Notes a reader should see: measured units of one label further apart than
    ``SAME_MODEL_NOTE`` (the model refuses only beyond ``SAME_MODEL_FAIL``), and inherited
    copies that no longer match their source."""
    notes: list[str] = []
    first: dict[str, tuple[str, Gamut]] = {}
    for room in model.rooms.values():
        for fid, fx in room.fixtures.items():
            mat = model.materials.get(fx.material)
            g = fx.gamut
            if mat is None or mat.model is None or g is None:
                continue
            if g.inherited_from is not None:
                continue
            ref = first.setdefault(mat.model, (fid, g))
            if ref[0] == fid:
                continue
            gap = vertex_deviation(g.vertices, ref[1].vertices)
            if gap > SAME_MODEL_NOTE:
                what = "beyond the model's limit" if gap > SAME_MODEL_FAIL else "worth a look"
                notes.append(
                    f"{fid} and {ref[0]} both carry '{mat.model}' but differ by {gap:.4f} ({what})"
                )
    for seed in seeds(model):
        if seed.refresh:
            notes.append(
                f"{seed.fixture} holds a copy of {seed.source} that no longer matches it; "
                f"run inherit to refresh"
            )
    return notes
