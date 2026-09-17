"""Command-line entry point: ``python -m ayce <command>``."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .config import Config, ConfigError
from .health import FAIL, CheckResult, render_text, run_checks


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ayce",
        description="AYCE — Autonomous YouTube Content Engine (P0 foundation)",
    )
    parser.add_argument("--version", action="version", version=f"ayce {__version__}")
    sub = parser.add_subparsers(dest="command")
    health = sub.add_parser("health", help="run baseline environment/diagnostic checks")
    health.add_argument("--json", action="store_true", help="output the report as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command or "health"

    if command != "health":
        parser.error(f"unknown command: {command}")

    try:
        config = Config.from_env()
    except ConfigError as exc:
        result = CheckResult("config", FAIL, str(exc))
        if getattr(args, "json", False):
            print(json.dumps({"checks": [result.__dict__], "ok": False}, indent=2))
        else:
            print(render_text([result]))
        return 1

    results = run_checks(config)
    ok = all(r.status != FAIL for r in results)
    if getattr(args, "json", False):
        print(json.dumps({"ok": ok, "checks": [r.__dict__ for r in results]}, indent=2))
    else:
        print(render_text(results))
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
