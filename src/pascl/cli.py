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
    me.add_argument("--fixture", required=True)
    me.add_argument("--ha-url", help="Home Assistant base URL (default: env HA_URL)")
    me.add_argument(
        "--identity",
        help="the device identity to bind the polygon to (default: the IEEE address the "
        "coordinator reports, or the topic when it reports none)",
    )
    me.add_argument(
        "--allow-lit",
        action="store_true",
        help="measure even if the fixture is on (pause whatever renders it first)",
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
    from pascl.estimator.gamut import FAR_POINTS, consistency, inherit, pick_target, statuses
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
        print(f"{len(applied)} seed(s)", file=sys.stderr)
        if args.write is not None and applied:
            args.write.write_text(dumps(seeded), encoding="utf-8")
            print(f"written to {args.write}", file=sys.stderr)
        return 0
    if args.gamut_command == "ids":
        return _cmd_gamut_ids(args, model)
    if args.gamut_command == "measure":
        return _cmd_gamut_measure(args, model)
    if args.gamut_command == "auto":
        return _cmd_gamut_auto(args, model)
    print("usage: pascl gamut {status,plan,pick,inherit,ids,measure,auto} ...", file=sys.stderr)
    return 2


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
    """Measure one fixture: resolve its /set topic from the binding, look its device up in
    the coordinator's list, run the protocol over a Home Assistant MQTT link, print the
    verdict, and optionally record it in the model (refusing a record the model would not
    validate with)."""
    from pascl.estimator.gamut import confirm_seed
    from pascl.harness.binding import expand, load_binding
    from pascl.model import dumps, validate, with_fixture_gamut
    from pascl.shell.gamut_measure import gamut_from, measure
    from pascl.shell.gamut_runtime import command_topics, device_infos, group_topics
    from pascl.shell.z2m import Z2MDeviceChannel, group_set_topics

    binding = load_binding(args.binding.read_text(encoding="utf-8"))
    index = expand(binding, model)
    topic = command_topics(model, index).get(args.fixture)
    if topic is None:
        print(f"the binding names no command topic for {args.fixture}", file=sys.stderr)
        return 2
    link = _ha_link(args)
    if link is None:
        return 2
    try:
        # the groups to watch: the model's, plus whatever the coordinator says the device is in
        watch = set(group_topics(model, index, args.fixture))
        base = _transport_base(model, args.fixture)
        info = device_infos(model, link).get(args.fixture)
        if info is not None and base:
            watch.update(group_set_topics(link, base, info.ieee_address))
        identity = args.identity
        if info is None and identity is None:
            print(f"{args.fixture}: not in the coordinator's device list; binding to the topic")
            identity = topic
        channel = Z2MDeviceChannel(link, topic, identity=identity, watch=sorted(watch))
        if info is not None:
            channel.adopt(info)
        before = channel.snapshot()
        print(
            f"{args.fixture} via {topic}: device {channel.identity} firmware {channel.firmware} "
            f"before {before}",
            file=sys.stderr,
        )
        verdict = measure(channel, allow_lit=args.allow_lit)
    finally:
        link.close()
    if verdict is None:
        print(f"{args.fixture} is lit; pass --allow-lit after pausing its render", file=sys.stderr)
        return 1
    for note in verdict.notes:
        print(f"note: {note}", file=sys.stderr)
    fits = " ".join(
        f"{f.rule}={f.error:.5f}" + (f"(-{f.outliers})" if f.outliers else "") for f in verdict.fits
    )
    print(
        f"polygon {verdict.polygon} clip_rule {verdict.clip_rule} model_error "
        f"{verdict.model_error} fits [{fits}] device_reports {verdict.device_reports} "
        f"unanswered {verdict.unanswered}"
    )
    gamut = gamut_from(verdict, channel)
    if gamut is None:
        return 1
    held = _held_gamut(model, args.fixture)
    if held is not None and held.inherited_from is not None:
        agreed, gap = confirm_seed(held, gamut.vertices)
        word = "confirmed" if agreed else "CONTRADICTED"
        print(f"seed from {held.inherited_from} {word} (gap {gap:.5f})", file=sys.stderr)
    if args.write is not None:
        updated = with_fixture_gamut(model, args.fixture, gamut)
        problems = validate(updated)
        if problems:
            for p in problems:
                print(f"refused: {p}", file=sys.stderr)
            return 1
        args.write.write_text(dumps(updated), encoding="utf-8")
        print(f"recorded in {args.write}", file=sys.stderr)
    return 0


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
        )
    finally:
        link.close()
    return 0


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
    from pascl.core.palette_reach import unreachable
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
    measured = sum(
        1 for room in model.rooms.values() for fx in room.fixtures.values() if fx.gamut is not None
    )
    print(
        f"{len(rows)} unreachable palette colour(s) across {measured} fixture(s) with a gamut",
        file=sys.stderr,
    )
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
