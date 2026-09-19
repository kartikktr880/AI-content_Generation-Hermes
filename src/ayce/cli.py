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
        description="AYCE — Autonomous YouTube Content Engine",
    )
    parser.add_argument("--version", action="version", version=f"ayce {__version__}")
    sub = parser.add_subparsers(dest="command")
    health = sub.add_parser("health", help="run baseline environment/diagnostic checks")
    health.add_argument("--json", action="store_true", help="output the report as JSON")
    run = sub.add_parser(
        "run", help="execute the Golden Path pipeline (script → QA report)"
    )
    run.add_argument("script", help="path to a script JSON input (ScriptInput contract)")
    run.add_argument(
        "--assets-dir",
        default=None,
        help="fixture directory for the file-backed asset provider (P2)",
    )
    run.add_argument(
        "--narration-dir",
        default=None,
        help="fixture directory for the file-backed narration provider (P3)",
    )
    run.add_argument("--json", action="store_true", help="output the pipeline report as JSON")
    return parser


def _cmd_health(args: argparse.Namespace) -> int:
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


def _cmd_run(args: argparse.Namespace) -> int:
    # Imported here so `ayce health` never pays the pipeline import cost.
    from pathlib import Path

    from .pipeline import run_pipeline

    # validate the input path BEFORE creating any run state (no partial runs)
    script = Path(args.script)
    if not script.is_file():
        print(f"ayce run: script input not found: {script}", file=sys.stderr)
        return 2

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"ayce run: configuration error: {exc}", file=sys.stderr)
        return 2

    result = run_pipeline(
        script,
        config=config,
        assets_dir=args.assets_dir,
        narration_dir=args.narration_dir,
    )

    if getattr(args, "json", False):
        print(json.dumps({
            "ok": result.ok,
            "run_id": result.run_id,
            "job_id": result.job_id,
            "run_dir": str(result.run_dir),
            "production_id": result.production_id,
            "stages": [
                {"stage": o.stage, "ok": o.ok, "error": o.error} for o in result.stages
            ],
            "qa_verdict": result.qa_verdict,
            "failed_stage": result.failed_stage,
            "error": result.error,
        }, indent=2))
    else:
        print("ayce run — golden path pipeline")
        for outcome in result.stages:
            status = "OK" if outcome.ok else "FAILED"
            line = f"  [{status}] {outcome.stage}"
            if outcome.error:
                line += f" — {outcome.error}"
            print(line)
        print(f"run_id: {result.run_id}")
        print(f"run_dir: {result.run_dir}")
        if result.production_id:
            print(f"production_id: {result.production_id}")
        if result.qa_verdict is not None:
            print(f"qa_verdict: {result.qa_verdict}")
        if not result.ok:
            print(
                f"pipeline failed at stage {result.failed_stage!r}: {result.error}",
                file=sys.stderr,
            )
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command or "health"

    if command == "health":
        return _cmd_health(args)
    if command == "run":
        return _cmd_run(args)
    parser.error(f"unknown command: {command}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
