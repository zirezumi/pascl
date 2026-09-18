"""Command-line entry point.

Deliberately thin: it parses arguments and hands off. ``pascl preview`` renders a model at an
instant (or scrubs a window) and prints the frames; ``pascl assemble`` turns a recorded export
into a golden trace under a binding; ``pascl binding-ids`` prints the host ids a binding
expands to, for an exporter to fetch and a host list to be checked against; ``pascl gamut``
is the colour-gamut calibration (its state, its seeds, a measurement, the unattended loop)
and ``pascl palette check`` reports the palette colours the measured fixtures cannot show.
The engine itself has nothing to run yet.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pascl.version import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pascl",
        description="PASCL, a presence-adaptive solar-circadian lighting compositor.",
    )
    parser.add_argument("--version", action="version", version=f"pascl {__version__}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("preview", help="render a Home Model at an instant, or scrub a window")
    p.add_argument("--model", required=True, type=Path)
    p.add_argument("--at", required=True, help="ISO instant; a naive value is taken as UTC")
    p.add_argument(
        "--room", action="append", default=None, help="limit to these rooms (repeatable)"
    )
    p.add_argument("--phase", default="occupied", choices=["occupied", "fading", "vacant"])
    p.add_argument("--scene", action="append", default=[], metavar="GROUP=PALETTE")
    p.add_argument("--offset", type=int, default=0, help="the global scene offset")
    p.add_argument("--sleeping", action="store_true")
    p.add_argument(
        "--scrub", type=int, default=0, metavar="MINUTES", help="step through 24 h from --at"
    )

    a = sub.add_parser("assemble", help="assemble a golden trace from a recorded export")
    a.add_argument("--model", required=True, type=Path)
    a.add_argument("--binding", required=True, type=Path)
    a.add_argument(
        "--raw", required=True, type=Path, help="newline-JSON export (.jsonl or .jsonl.gz)"
    )
    a.add_argument("--out", required=True, type=Path)
    a.add_argument("--report", action="store_true", help="print what the binding could not place")

    r = sub.add_parser("replay", help="re-render at every recorded wire command and compare")
    r.add_argument("--model", required=True, type=Path)
    r.add_argument("--binding", type=Path, help="needed with --raw")
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument("--raw", type=Path, help="a recorded export, assembled on the fly")
    src.add_argument("--trace", type=Path, help="an assembled golden trace")
    r.add_argument("--room", action="append", default=None, help="limit to these rooms")
    r.add_argument(
        "--palette",
        action="store_true",
        help="derive scene colour from the palette recipe, not the recorded per-fixture value",
    )
    r.add_argument("--limit", type=int, default=40, help="mismatch lines to print")

    b = sub.add_parser(
        "binding-ids",
        help="list every host id a binding expands to, and check them against the host's own list",
    )
    b.add_argument("--model", required=True, type=Path)
    b.add_argument("--binding", required=True, type=Path)
    b.add_argument(
        "--live",
        type=Path,
        help="the host's entity ids, one per line; bound ids missing from it fail the command",
    )
    b.add_argument("--no-topics", action="store_true", help="omit the command topics")

    g = sub.add_parser(
        "gamut",
        help="colour gamut calibration: which fixtures are measured, and the probe plan",
    )
    gs = g.add_subparsers(dest="gamut_command")
    st = gs.add_parser(
        "status", help="every colour fixture: measured, inherited, unmeasured or rebound"
    )
    st.add_argument("--model", required=True, type=Path)
    st.add_argument(
        "--device-ids",
        type=Path,
        help="'fixture<TAB>device identity' lines from the host (pascl gamut ids); a bound "
        "polygon whose device changed reads as rebound",
    )
    pl = gs.add_parser("plan", help="print the far-point probe plan a measurement starts with")
    pk = gs.add_parser("pick", help="the fixture the runtime would measure next, if any")
    pk.add_argument("--model", required=True, type=Path)
    pk.add_argument("--device-ids", type=Path)
    pk.add_argument("--lit", type=Path, help="lit fixture ids, one per line")
    pk.add_argument("--occupied", type=Path, help="occupied room ids, one per line")
    del pl
    inh = gs.add_parser(
        "inherit",
        help="seed unmeasured fixtures from a measured one of the same material model label",
    )
    inh.add_argument("--model", required=True, type=Path)
    inh.add_argument("--device-ids", type=Path)
    inh.add_argument("--write", type=Path, help="write the seeded model to this path")
    ids = gs.add_parser(
        "ids",
        help="the transport's identity, model and firmware of every fixture's device, as "
        "'fixture<TAB>identity<TAB>model<TAB>firmware' lines for --device-ids",
    )
    ids.add_argument("--model", required=True, type=Path)
    ids.add_argument("--binding", required=True, type=Path)
    ids.add_argument("--ha-url", help="Home Assistant base URL (default: env HA_URL)")
    me = gs.add_parser(
        "measure",
        help="measure one fixture's gamut over Zigbee2MQTT through Home Assistant",
    )
    me.add_argument("--model", required=True, type=Path)
    me.add_argument("--binding", required=True, type=Path, help="names the fixture's /set topic")
    me.add_argument(
        "--fixture", action="append", required=True, help="a fixture to measure (repeatable)"
    )
    me.add_argument(
        "--parallel",
        type=int,
        default=2,
        metavar="N",
        help="the most fixtures in flight per coordinator or transport; the number actually in "
        "flight is found from the samples (grown on prompt answers, halved on a retry)",
    )
    me.add_argument("--ha-url", help="Home Assistant base URL (default: env HA_URL)")
    me.add_argument(
        "--identity",
        help="the device identity to bind the polygon to (default: the IEEE address the "
        "coordinator reports, or the topic when it reports none)",
    )
    me.add_argument(
        "--allow-lit",
        "--force",
        dest="allow_lit",
        action="store_true",
        help="forced mode: measure whatever the fixture's state and whoever is in the room, "
        "visibly (otherwise a lit fixture is skipped and a measurement aborts, restoring the "
        "fixture at once, when the room fills or the fixture is turned on)",
    )
    me.add_argument(
        "--ct-only",
        dest="ct_only",
        action="store_true",
        help="measure the colour-temperature range alone (two probes, a few seconds) and "
        "record it on the fixture's existing gamut; the polygon must already be measured",
    )
    me.add_argument(
        "--write", type=Path, help="write the model with the measurement recorded to this path"
    )
    au = gs.add_parser(
        "auto",
        help="the runtime loop: measure the next dark fixture in an empty room, record it, "
        "seed its siblings, rest, repeat",
    )
    au.add_argument("--model", required=True, type=Path)
    au.add_argument("--binding", required=True, type=Path)
    au.add_argument("--ha-url", help="Home Assistant base URL (default: env HA_URL)")
    au.add_argument("--write", type=Path, help="where to write the model after each measurement")
    au.add_argument("--interval", type=float, default=300.0, help="seconds between ticks")
    au.add_argument("--once", action="store_true", help="one tick, then exit")
    au.add_argument(
        "--force",
        action="store_true",
        help="forced mode: lit fixtures and occupied rooms too, visibly",
    )

    ob = gs.add_parser(
        "observe-ct",
        help="judge a fixture's colour-temperature range from (commanded, reported) pairs a "
        "transport gathered, and record what is published on its gamut",
    )
    ob.add_argument("--model", required=True, type=Path)
    ob.add_argument("--fixture", required=True, help="the fixture the pairs belong to")
    ob.add_argument(
        "--pairs",
        required=True,
        type=Path,
        help="JSON: a list of [commanded, reported] mired pairs, or an object with a 'pairs' key",
    )
    ob.add_argument("--write", type=Path, help="write the model with the range recorded")

    pa = sub.add_parser("palette", help="palette checks against the measured gamuts")
    ps = pa.add_subparsers(dest="palette_command")
    pc = ps.add_parser(
        "check", help="list every palette colour some fixture cannot show, and what it shows"
    )
    pc.add_argument("--model", required=True, type=Path)
    return parser


def _parse_at(text: str) -> datetime:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _cmd_preview(args: argparse.Namespace) -> int:
    from pascl.harness.preview import PreviewInputs, preview, site_of
    from pascl.model import load

    model = load(args.model.read_text(encoding="utf-8"))
    bindings = dict(s.split("=", 1) for s in args.scene)
    rooms = args.room or list(model.rooms)
    inputs = PreviewInputs(
        phases={rid: args.phase for rid in model.rooms},
        scene_bindings=bindings,
        scene_offset=args.offset,
        sleeping=args.sleeping,
    )
    start = _parse_at(args.at)
    tz = site_of(model).tz
    steps = (
        [start]
        if not args.scrub
        else [start + timedelta(minutes=m) for m in range(0, 24 * 60, args.scrub)]
    )
    for at in steps:
        r = preview(model, at, inputs)
        local = at.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        print(
            f"{local}  p={r.solar.progress:.3f} factor={r.solar.factor:.3f} "
            f"white={r.white.kelvin_rounded}K"
        )
        for rid in rooms:
            for fid in model.rooms[rid].fixtures:
                f = r.frames[fid]
                colour = f"ct={f.ct_mired}" if f.mode == "ct" else (f"xy={f.xy}" if f.xy else "")
                state = "on " if f.on else ("vrt" if f.virtual else "off")
                bri = "-" if f.brightness is None else str(f.brightness)
                print(f"  {fid:28s} {state} bri={bri:>4} {colour}")
    return 0


def _cmd_assemble(args: argparse.Namespace) -> int:
    import gzip

    from pascl.harness.assemble import assemble
    from pascl.harness.binding import load_binding
    from pascl.harness.trace import write_trace
    from pascl.model import load

    model = load(args.model.read_text(encoding="utf-8"))
    binding = load_binding(args.binding.read_text(encoding="utf-8"))
    with (
        gzip.open(args.raw, "rt", encoding="utf-8")
        if args.raw.suffix == ".gz"
        else args.raw.open(encoding="utf-8")
    ) as f:
        result = assemble(model, binding, f)
    n = write_trace(args.out, result.records)
    print(f"wrote {n} records to {args.out}: {dict(result.kinds)}", file=sys.stderr)
    if args.report:
        for eid, count in result.unmapped_entities.most_common(40):
            print(f"unmapped entity {count:6d}  {eid}", file=sys.stderr)
        for topic, count in result.unmapped_topics.most_common(20):
            print(f"unmapped topic  {count:6d}  {topic}", file=sys.stderr)
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    import gzip

    from pascl.harness.assemble import assemble
    from pascl.harness.binding import load_binding
    from pascl.harness.replay import describe, replay
    from pascl.harness.trace import Record, read_trace
    from pascl.model import load

    model = load(args.model.read_text(encoding="utf-8"))
    records: list[Record]
    if args.raw is not None:
        if args.binding is None:
            print("--raw needs --binding", file=sys.stderr)
            return 2
        binding = load_binding(args.binding.read_text(encoding="utf-8"))
        with (
            gzip.open(args.raw, "rt", encoding="utf-8")
            if args.raw.suffix == ".gz"
            else args.raw.open(encoding="utf-8")
        ) as f:
            assembled = assemble(model, binding, f)
        records = assembled.records
        print(f"assembled {dict(assembled.kinds)}", file=sys.stderr)
    else:
        records = list(read_trace(args.trace))
    result = replay(model, records, rooms=args.room, recorded_scene_colour=not args.palette)
    print(result.summary())
    for s in result.mismatches[: args.limit]:
        print(describe(s))
    return 0 if not result.mismatches else 1


def _cmd_binding_ids(args: argparse.Namespace) -> int:
    """Print the ids a recording must carry for this binding to place the full input vector.

    An exporter that filters a store by its own list of names silently drops whatever that
    list does not name, and the assembler cannot report it: an entity that never reached the
    export is not unplaced, it is absent. Feeding the exporter this list closes that gap from
    the binding's side; ``--live`` closes it from the host's, by naming the bound ids the host
    does not have at all.
    """
    from pascl.harness.binding import expand, load_binding, unbound_live
    from pascl.model import load

    model = load(args.model.read_text(encoding="utf-8"))
    binding = load_binding(args.binding.read_text(encoding="utf-8"))
    index = expand(binding, model)
    for eid in sorted(index.entities):
        print(eid)
    if not args.no_topics:
        for topic in sorted(index.topics):
            print(topic)
    if args.live is None:
        return 0
    live = [ln.strip() for ln in args.live.read_text(encoding="utf-8").splitlines() if ln.strip()]
    missing = unbound_live(index, live)
    for eid in missing:
        print(f"not live: {eid} -> {index.entities[eid].path}", file=sys.stderr)
    print(
        f"{len(index.entities)} bound entity ids, {len(missing)} not on the host",
        file=sys.stderr,
    )
    return 1 if missing else 0


def _read_lines(path: Path | None) -> list[str]:
    if path is None:
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _read_device_ids(path: Path | None) -> dict[str, str]:
    """'fixture<TAB>identity[<TAB>...]' lines (what ``pascl gamut ids`` prints)."""
    out: dict[str, str] = {}
    for ln in _read_lines(path):
        cols = ln.split("\t")
        if len(cols) >= 2 and cols[0] and cols[1]:
            out[cols[0]] = cols[1]
    return out


def _ha_link(args: argparse.Namespace) -> Any:
    """A Home Assistant link from ``--ha-url``/``HA_URL`` and ``HA_TOKEN`` (the token is read
    from the environment so it never appears on a command line), or None with a message."""
    import os

    from pascl.shell.ha_mqtt import HAMqttLink

    url = args.ha_url or os.environ.get("HA_URL")
    token = os.environ.get("HA_TOKEN")
    if not url or not token:
        print("need --ha-url (or HA_URL) and HA_TOKEN in the environment", file=sys.stderr)
        return None
    return HAMqttLink(url, token)


def _cmd_gamut(args: argparse.Namespace) -> int:
    """The gamut calibration from the outside: what is measured, what the runtime would
    measure next, the plan a measurement follows, and the seeds a measurement gives its
    same-model siblings. The measurement itself needs a transport and lives with the
    adapters; these are the pure views of it."""
    from pascl.estimator.gamut import (
        FAR_POINTS,
        consistency,
        inherit,
        inherit_ct,
        pick_target,
        statuses,
    )
    from pascl.model import dumps, load

    if args.gamut_command == "plan":
        for i, (x, y) in enumerate(FAR_POINTS, 1):
            print(f"{i:2d}  far  {x:.2f} {y:.2f}")
        print(
            f"{len(FAR_POINTS)} far points, then one probe beyond each hull vertex, one outside "
            "each edge, and a corner probe for any edge the device does not clip to"
        )
        return 0
    model = load(args.model.read_text(encoding="utf-8"))
    ids = _read_device_ids(getattr(args, "device_ids", None))
    if args.gamut_command == "status":
        rows = statuses(model, ids)
        for r in rows:
            print(f"{r.status:10s} {r.room:20s} {r.fixture:32s} {r.label or ''}")
        kinds = ("measured", "inherited", "unmeasured", "rebound")
        counts = {k: sum(1 for r in rows if r.status == k) for k in kinds}
        print(", ".join(f"{v} {k}" for k, v in counts.items()), file=sys.stderr)
        for note in consistency(model):
            print(f"note: {note}", file=sys.stderr)
        return 0 if counts["unmeasured"] == 0 and counts["rebound"] == 0 else 1
    if args.gamut_command == "pick":
        pick = pick_target(model, ids, set(_read_lines(args.lit)), set(_read_lines(args.occupied)))
        if pick is None:
            print("nothing to measure now", file=sys.stderr)
            return 1
        print(f"{pick.status} {pick.room} {pick.fixture}")
        return 0
    if args.gamut_command == "inherit":
        seeded, applied = inherit(model, ids)
        for s in applied:
            verb = "refresh" if s.refresh else "seed"
            print(f"{verb:8s} {s.room:20s} {s.fixture:32s} from {s.source}  [{s.label}]")
        seeded, ct_applied = inherit_ct(seeded)
        for c in ct_applied:
            print(f"{'ct ' + c.end:8s} {c.fixture:32s} {c.kelvin} K from {c.source}  [{c.label}]")
        print(
            f"{len(applied)} seed(s), {len(ct_applied)} colour temperature end(s)", file=sys.stderr
        )
        if args.write is not None and (applied or ct_applied):
            args.write.write_text(dumps(seeded), encoding="utf-8")
            print(f"written to {args.write}", file=sys.stderr)
        return 0
    if args.gamut_command == "observe-ct":
        return _cmd_gamut_observe_ct(args, model)
    if args.gamut_command == "ids":
        return _cmd_gamut_ids(args, model)
    if args.gamut_command == "measure":
        return _cmd_gamut_measure(args, model)
    if args.gamut_command == "auto":
        return _cmd_gamut_auto(args, model)
    print(
        "usage: pascl gamut {status,plan,pick,inherit,ids,measure,auto,observe-ct} ...",
        file=sys.stderr,
    )
    return 2


def _cmd_gamut_observe_ct(args: argparse.Namespace, model: Any) -> int:
    """The observed tier of the colour-temperature range (docs/gamut.md section 11): the
    pairs a transport gathered, judged per end, and each published end recorded on the
    fixture's gamut where the end it holds came from a weaker tier."""
    import json

    from pascl.estimator.ct_observed import apply, observe
    from pascl.model import dumps, validate, with_fixture_gamut

    raw = json.loads(args.pairs.read_text(encoding="utf-8"))
    rows = raw["pairs"] if isinstance(raw, dict) else raw
    pairs = [(int(c), int(r)) for c, r in rows]
    verdict = observe(pairs)
    span = verdict.commanded_mired
    print(
        f"{args.fixture}: {verdict.pairs} pair(s)"
        + (f", commanded {span[0]}-{span[1]} mired" if span else "")
    )
    for ev in (verdict.floor, verdict.ceiling):
        if ev.published:
            print(
                f"  {ev.end}: {ev.kelvin} K ({ev.mired} mired) from {ev.support} pair(s), "
                f"{ev.disagreeing} disagreeing, {ev.contradictions} contradicting"
            )
        else:
            print(f"  {ev.end}: declined ({ev.verdict}): {ev.reason}")
    held = _held_gamut(model, args.fixture)
    if held is None:
        print(f"{args.fixture}: no polygon on record; measure it first", file=sys.stderr)
        return 1
    updated, notes = apply(held, verdict)
    for note in notes:
        print(f"{args.fixture} note: {note}", file=sys.stderr)
    if updated == held:
        print(f"{args.fixture}: nothing recorded", file=sys.stderr)
        return 0
    candidate = with_fixture_gamut(model, args.fixture, updated)
    problems = validate(candidate)
    if problems:
        for p in problems:
            print(f"{args.fixture}: refused: {p}", file=sys.stderr)
        return 1
    print(
        f"{args.fixture}: ct_range_k {updated.ct_range_k} ct_sources {updated.ct_sources}",
        file=sys.stderr,
    )
    if args.write is not None:
        args.write.write_text(dumps(candidate), encoding="utf-8")
        print(f"written to {args.write}", file=sys.stderr)
    return 0


def _cmd_gamut_ids(args: argparse.Namespace, model: Any) -> int:
    """Every fixture's device as its coordinator lists it: identity, model id, firmware."""
    from pascl.estimator.gamut import statuses
    from pascl.shell.gamut_runtime import device_ids, device_infos

    link = _ha_link(args)
    if link is None:
        return 2
    try:
        infos = device_infos(model, link)
    finally:
        link.close()
    ids = device_ids(model, infos)
    missing = 0
    for s in statuses(model):
        info = infos.get(s.fixture)
        if info is None:
            missing += 1
            print(f"{s.fixture} is not listed by its coordinator", file=sys.stderr)
            continue
        print(f"{s.fixture}\t{ids[s.fixture]}\t{info.model_id or ''}\t{info.firmware or ''}")
    print(f"{len(ids)} devices, {missing} not listed", file=sys.stderr)
    return 1 if missing else 0


def _cmd_gamut_measure(args: argparse.Namespace, model: Any) -> int:
    """Measure one or more fixtures: each reached the way its transport allows (a Zigbee2MQTT
    command topic, or the light entity the host exposes), measured together in batches sized
    per coordinator or transport, the verdicts printed with every rule's fit, and the model
    written with the records (refusing any the model would not validate with)."""

    from pascl.estimator.gamut import confirm_seed, with_measured_ct
    from pascl.harness.binding import expand, load_binding
    from pascl.model import dumps, validate, with_fixture_gamut
    from pascl.shell.airtime import measure_adaptively
    from pascl.shell.gamut_measure import gamut_from
    from pascl.shell.gamut_runtime import channel_for, device_infos, takes_ct
    from pascl.shell.hub import Hub

    binding = load_binding(args.binding.read_text(encoding="utf-8"))
    index = expand(binding, model)
    link = _ha_link(args)
    if link is None:
        return 2
    updated = model
    failed = 0
    try:
        infos = device_infos(model, link)
        hub = Hub(link)
        keyed: list[tuple[str, str]] = []
        found: dict[str, Any] = {}
        for fixture in dict.fromkeys(args.fixture):
            got = channel_for(model, index, hub.view(), fixture, infos)
            if got is None:
                print(
                    f"{fixture}: neither a command topic nor a light entity reaches it",
                    file=sys.stderr,
                )
                failed += 1
                continue
            channel, key = got
            if args.identity and len(args.fixture) == 1:
                channel = _rebind(channel, args.identity)
            found[fixture] = channel
            keyed.append((fixture, key))
        channels = {f: found[f] for f, _key in keyed}
        hub.start()  # the snapshots read through the hub too
        try:
            for f, ch in channels.items():
                before = ch.snapshot()
                print(
                    f"{f}: device {ch.identity} firmware {ch.firmware} before {before}",
                    file=sys.stderr,
                )
            results, windows = measure_adaptively(
                channels,
                dict(keyed),
                allow_lit=args.allow_lit,
                ceiling=max(1, args.parallel),
                on_launch=lambda n, f, w: print(
                    f"launch {n} ({f} of {w} on its transport)", file=sys.stderr
                ),
                ct={f for f in channels if takes_ct(model, f)},
                xy=not args.ct_only,
            )
        finally:
            hub.stop()
        for key, w in sorted(windows.items()):
            print(
                f"airtime {key or '-'}: window ended at {w.final}, peak {w.peak}, "
                f"{w.good} of {w.samples} samples prompt, {w.increases} up / {w.decreases} down",
                file=sys.stderr,
            )
        for f, _key in keyed:
            verdict = results[f]
            if isinstance(verdict, BaseException):
                print(f"{f}: FAILED {type(verdict).__name__}: {verdict}", file=sys.stderr)
                failed += 1
                continue
            if verdict is None:
                print(
                    f"{f} is lit or its transport needs the forced mode; pass --force",
                    file=sys.stderr,
                )
                failed += 1
                continue
            for note in verdict.notes:
                print(f"{f} note: {note}", file=sys.stderr)
            fits = " ".join(
                f"{r.rule}={r.error:.5f}" + (f"(-{r.outliers})" if r.outliers else "")
                for r in verdict.fits
            )
            ct = verdict.ct_range_k
            ct_text = "declined" if ct is None else f"{ct[0]}-{ct[1]} K"
            if args.ct_only:
                print(f"{f}: ct_range_k {ct_text} answers {list(verdict.ct_answers)}")
                held = _held_gamut(updated, f)
                if held is None:
                    print(f"{f}: no polygon on record; measure it first", file=sys.stderr)
                    failed += 1
                    continue
                merged = with_measured_ct(held, verdict)
                for d in verdict.ct_declined:
                    print(f"{f} {d.tier} {d.end}: {d.verdict}: {d.reason}", file=sys.stderr)
                if merged == held:
                    continue
                candidate = with_fixture_gamut(updated, f, merged)
                problems = validate(candidate)
                if problems:
                    for p in problems:
                        print(f"{f}: refused: {p}", file=sys.stderr)
                    failed += 1
                    continue
                updated = candidate
                continue
            print(
                f"{f}: polygon {verdict.polygon} clip_rule {verdict.clip_rule} model_error "
                f"{verdict.model_error} fits [{fits}] device_reports {verdict.device_reports} "
                f"unanswered {verdict.unanswered} ct_range_k {ct_text}"
            )
            gamut = gamut_from(verdict, channels[f])
            if gamut is None:
                failed += 1
                continue
            held = _held_gamut(updated, f)
            if held is not None and held.inherited_from is not None:
                agreed, gap = confirm_seed(held, gamut.vertices)
                word = "confirmed" if agreed else "CONTRADICTED"
                print(
                    f"{f}: seed from {held.inherited_from} {word} (gap {gap:.5f})",
                    file=sys.stderr,
                )
            candidate = with_fixture_gamut(updated, f, gamut)
            problems = validate(candidate)
            if problems:
                for p in problems:
                    print(f"{f}: refused: {p}", file=sys.stderr)
                failed += 1
                continue
            updated = candidate
    finally:
        link.close()
    if args.write is not None and updated is not model:
        args.write.write_text(dumps(updated), encoding="utf-8")
        print(f"recorded in {args.write}", file=sys.stderr)
    return 1 if failed else 0


def _rebind(channel: Any, identity: str) -> Any:
    """An explicit --identity overrides what discovery bound."""
    channel._identity = identity
    return channel


def _cmd_gamut_auto(args: argparse.Namespace, model: Any) -> int:
    """The runtime loop from the command line."""
    from pascl.harness.binding import expand, load_binding
    from pascl.shell.gamut_runtime import Tick, run

    binding = load_binding(args.binding.read_text(encoding="utf-8"))
    index = expand(binding, model)
    link = _ha_link(args)
    if link is None:
        return 2

    def report(t: Tick) -> None:
        if t.picked is None:
            print(f"idle: {t.remaining} left; {'; '.join(t.notes)}", file=sys.stderr)
            return
        head = f"{t.picked.status} {t.picked.room} {t.picked.fixture}"
        if t.verdict is None:
            print(f"{head}: {'; '.join(t.notes)}", file=sys.stderr)
            return
        v = t.verdict
        print(
            f"{head}: polygon {v.polygon} rule {v.clip_rule} error {v.model_error} "
            f"{'written' if t.written else 'not written'}; {len(t.seeds)} seed(s); "
            f"{t.remaining} left",
            file=sys.stderr,
        )
        for note in t.notes:
            print(f"  note: {note}", file=sys.stderr)

    try:
        run(
            model,
            index,
            link,
            write=args.write,
            interval_s=args.interval,
            report=report,
            once=args.once,
            force=args.force,
        )
    finally:
        link.close()
    return 0


def _room_of(model: Any, fixture_id: str) -> str:
    for rid, room in model.rooms.items():
        if fixture_id in room.fixtures:
            return str(rid)
    return ""


def _transport_base(model: Any, fixture_id: str) -> str | None:
    for room in model.rooms.values():
        fx = room.fixtures.get(fixture_id)
        if fx is not None:
            tr = model.transports.get(fx.transport)
            return None if tr is None else tr.base_topic
    return None


def _held_gamut(model: Any, fixture_id: str) -> Any:
    for room in model.rooms.values():
        fx = room.fixtures.get(fixture_id)
        if fx is not None:
            return fx.gamut
    return None


def _cmd_palette(args: argparse.Namespace) -> int:
    """Palette colours against the measured gamuts: what renders, and what does not."""
    from pascl.core.palette_reach import cct_credibility, cct_unreachable, unreachable
    from pascl.model import load

    if args.palette_command != "check":
        print("usage: pascl palette check --model F", file=sys.stderr)
        return 2
    model = load(args.model.read_text(encoding="utf-8"))
    rows = unreachable(model)
    for u in rows:
        where = (
            f"{u.family}[{u.index}]" if u.index is not None else f"{u.family} @ {u.kelvin:.0f} K"
        )
        print(
            f"{u.palette:16s} {where:14s} {u.intent[0]:.4f} {u.intent[1]:.4f} -> "
            f"{u.reachable[0]:.4f} {u.reachable[1]:.4f}  gap {u.gap:.4f}  "
            f"{len(u.fixtures)} fixture(s): {', '.join(u.fixtures)}"
        )
    ct_rows = cct_unreachable(model)
    for c in ct_rows:
        print(
            f"{c.palette:16s} {'ct arc':14s} {c.kelvin:.0f} K -> {c.reachable} K  "
            f"gap {c.gap:.0f} K  (range {c.ct_range_k[0]}-{c.ct_range_k[1]} K)  "
            f"{len(c.fixtures)} fixture(s): {', '.join(c.fixtures)}"
        )
    questions = 0
    for cred in cct_credibility(model):
        ends = " - ".join(
            f"{'?' if v is None else v} K ({s or 'unknown'})"
            for v, s in zip(cred.ct_range_k, cred.sources, strict=True)
        )
        print(
            f"ct range {ends} on {len(cred.fixtures)} fixture(s) of {cred.material}: {cred.note}: "
            f"{', '.join(cred.fixtures)}",
            file=sys.stderr,
        )
        if cred.question is not None:
            questions += 1
            print(f"QUESTION {questions}: {cred.question}", file=sys.stderr)
    measured = sum(
        1 for room in model.rooms.values() for fx in room.fixtures.values() if fx.gamut is not None
    )
    print(
        f"{len(rows)} unreachable palette colour(s) across {measured} fixture(s) with a gamut; "
        f"{len(ct_rows)} colour temperature arc(s) parked at a white-only fixture's range; "
        f"{questions} question(s) for the author",
        file=sys.stderr,
    )
    # A device clipping a colour the author chose is a surprise worth a non-zero exit; a
    # white-only fixture parked at its floor for the night arc is the render's own, deliberate
    # floor, and a range that is not credible is a call to measure, not a failure.
    return 1 if rows else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "preview":
        return _cmd_preview(args)
    if args.command == "gamut":
        return _cmd_gamut(args)
    if args.command == "palette":
        return _cmd_palette(args)
    if args.command == "assemble":
        return _cmd_assemble(args)
    if args.command == "replay":
        return _cmd_replay(args)
    if args.command == "binding-ids":
        return _cmd_binding_ids(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
