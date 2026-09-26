"""Research worker CLI entry (MCP-invoked, stdin→stdout JSON contract).

The Director MCP server spawns ONE fixed argv template::

    <pinned python> -m ayce.research --json

The validated request JSON arrives on **stdin** (server-controlled; the
MCP caller can never place anything into argv). Exactly ONE JSON
document is written to stdout — the compact envelope or a structured
error. All diagnostics go to stderr. Exit codes classify failures so
the MCP boundary can map them to structured errors.

Exit codes:
    0 success · 2 invalid_request · 3 source_unavailable ·
    4 rate_limited · 5 extraction_failed · 6 analysis_failed ·
    7 artifact_validation_failed · 8 internal_error ·
    9 insufficient_evidence (--brief only: no usable candidates)

Modes:
    default   request JSON on stdin  -> compact research envelope on stdout
    --brief   ResearchArtifact JSON on stdin -> validated ScriptInput
              JSON on stdout (the deterministic Stage 2.5 bridge; see
              :mod:`ayce.research.brief`)
"""

from __future__ import annotations

import json
import sys

from pydantic import ValidationError

from ..config import Config
from .brief import BriefError, build_script_input
from .models import ResearchArtifact
from .worker import ResearchError, run_research

__all__ = ["main", "EXIT_CODES"]

EXIT_CODES = {
    "invalid_request": 2,
    "source_unavailable": 3,
    "rate_limited": 4,
    "extraction_failed": 5,
    "analysis_failed": 6,
    "artifact_validation_failed": 7,
    "internal_error": 8,
    "insufficient_evidence": 9,
}

USAGE = (
    "usage: python -m ayce.research [--brief] --json\n"
    "  default: a ResearchRequest JSON on stdin -> compact envelope on stdout\n"
    "  --brief: a ResearchArtifact JSON on stdin -> a validated ScriptInput\n"
    "           JSON on stdout (feed it directly to `ayce run <script.json>`);\n"
    "           failures return the structured error envelope + exit code"
)



def _error_envelope(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message[:2000]}}


def _force_utf8_stdio() -> None:
    """Windows consoles/pipes default to a legacy codepage (cp1252); the
    JSON envelope is UTF-8 by contract. Replace on both ends of the pipe."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _run_brief(payload: dict) -> int:
    """--brief mode: ResearchArtifact JSON (already parsed) in, validated
    ScriptInput JSON out. Success stdout is the BARE ScriptInput document
    so it can be redirected straight to a file for `ayce run`; failures
    return the structured error envelope + a classified exit code.
    Brief warnings go to stderr (stdout stays single-document)."""
    try:
        artifact = ResearchArtifact.model_validate(payload)
    except ValidationError as exc:
        json.dump(_error_envelope("artifact_validation_failed", str(exc)[:2000]), sys.stdout)
        sys.stdout.write("\n")
        return EXIT_CODES["artifact_validation_failed"]

    try:
        result = build_script_input(artifact)
    except BriefError as exc:
        code = exc.code if exc.code in EXIT_CODES else "internal_error"
        json.dump(_error_envelope(code, exc.message), sys.stdout)
        sys.stdout.write("\n")
        return EXIT_CODES[code]

    for warning in result.warnings:
        sys.stderr.write(f"brief_warning: {warning}\n")
    sys.stdout.write(result.script_input.model_dump_json(indent=2, exclude_none=True))
    sys.stdout.write("\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    args = list(sys.argv[1:] if argv is None else argv)
    brief_mode = "--brief" in args
    if brief_mode:
        args.remove("--brief")
    if any(arg not in ("--json",) for arg in args):
        json.dump(_error_envelope("invalid_request", USAGE), sys.stdout)
        sys.stdout.write("\n")
        return EXIT_CODES["invalid_request"]

    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        json.dump(_error_envelope("invalid_request", f"request on stdin must be JSON: {exc}"), sys.stdout)
        sys.stdout.write("\n")
        return EXIT_CODES["invalid_request"]

    if not isinstance(payload, dict):
        json.dump(_error_envelope("invalid_request", "request must be a JSON object"), sys.stdout)
        sys.stdout.write("\n")
        return EXIT_CODES["invalid_request"]

    if brief_mode:
        return _run_brief(payload)

    try:
        envelope = run_research(payload, Config.from_env())
    except ResearchError as exc:
        code = exc.code if exc.code in EXIT_CODES else "internal_error"
        json.dump(_error_envelope(code, exc.message), sys.stdout)
        sys.stdout.write("\n")
        return EXIT_CODES[code]
    except ValidationError as exc:  # artifact contract rejected its own output
        json.dump(_error_envelope("artifact_validation_failed", str(exc)[:2000]), sys.stdout)
        sys.stdout.write("\n")
        return EXIT_CODES["artifact_validation_failed"]
    except Exception as exc:  # noqa: BLE001 — the boundary must answer truthfully
        json.dump(_error_envelope("internal_error", f"{type(exc).__name__}: {exc}"), sys.stdout)
        sys.stdout.write("\n")
        return EXIT_CODES["internal_error"]

    json.dump(envelope, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())