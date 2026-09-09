"""Command-line entry point.

Deliberately thin: it parses arguments and hands off. ``pascl preview`` renders a model at an
instant (or scrubs a window) and prints the frames; ``pascl assemble`` turns a recorded export
into a golden trace under a binding. The engine itself has nothing to run yet.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "preview":
        return _cmd_preview(args)
    if args.command == "assemble":
        return _cmd_assemble(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
