"""Command-line entry point.

Deliberately thin: it parses arguments and hands off to the shell. There is nothing to run yet;
the engine is being extracted from its reference installation and this command reports only its
version until there is.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from pascl.version import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pascl",
        description="PASCL, a presence-adaptive solar-circadian lighting compositor.",
    )
    parser.add_argument("--version", action="version", version=f"pascl {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
