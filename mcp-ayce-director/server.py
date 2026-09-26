"""AYCE Director MCP server (H4 experiment) — exactly ONE write capability.

Exposes exactly ONE tool over MCP stdio JSON-RPC:

    trigger_golden_path   request ONE approved Golden Path production run

Implements the H3-approved boundary (docs/hermes_director_h3_design.md):

- Command contract (H3 §6): {request_id, command, script_id, reason} —
  exact fields, no extras (extra/missing fields -> invalid_request).
- script_id is an ALLOWLIST KEY (scripts.json, operator-maintained), never
  a path. The grammar plus exact-match lookup make traversal/shell
  metacharacters/arbitrary filenames impossible by construction.
- Execution (H3 §7/§10): ONE fixed argv template, no shell:
      <pinned python> -m ayce run <allowlisted-script> [--assets-dir ...]
      [--narration-dir ...] --json
  The interpreter, script path, options, cwd and child environment are
  ALL server-controlled. The caller can supply none of them.
- Idempotency (H3 §11): request ledger = one JSON file (requests.json in
  this directory; AYCE_DIRECTOR_LEDGER overrides), written atomically
  (tmp + os.replace). Same request_id + identical payload -> duplicate
  (returns the recorded result, NO second run). Same request_id with a
  conflicting payload -> rejected, nothing executed.
- Single-flight (H3 §11): a ledger state check — at most one request in
  "running" state; concurrent requests are rejected busy. No daemon, no
  queue, no database.
- Timeout (H3 §9 / H4): bounded experimental subprocess timeout
  (AYCE_DIRECTOR_TIMEOUT_S, default 600 s). On expiry subprocess.run
  KILLS the child; the result is a truthful timeout (never success) and
  the run directory keeps AYCE's truthful partial checkpoints.
- AYCE remains the ONLY production executor: this server never imports
  pipeline internals for execution and never mutates RunState. Its ONLY
  writes are its own ledger, the Stage 9 policy-consumption evidence log,
  and — Stage 10, observability only — the ONE hash-pinned consumption
  artifact (plus its registry entry) inside a completed run directory
  under data/runs.
- Stage 9 — POLICY EXECUTION CONTEXT: at the ONE decision boundary
  (trigger_golden_path) the director consumes the ACTIVE ProductionPolicy
  through the canonical READ-ONLY Stage 8 library
  (ayce.policy.execution.build_execution_context, SQLite mode=ro). The
  director NEVER opens a writable policy connection, NEVER issues policy
  INSERT/UPDATE/DELETE, and NEVER authors/approves/promotes/activates/
  rolls back policy. `no policy` is a normal, truthful state
  (policy_status "none") — nothing is fabricated. A corrupted or
  unverifiable active policy FAILS CLOSED: the request is rejected with
  policy_context_invalid and NO run is launched. The exact context
  received is hashed (context_hash) and recorded as an append-only
  consumption event (policy_consumption.jsonl, content-addressed,
  idempotent by decision).

Operator-controlled env (never caller-controlled): AYCE_POLICY_DB
(policy database location), AYCE_POLICY_SCOPE_CANDIDATES (ordered,
most-specific-first scope resolution list; default "global"),
AYCE_POLICY_CONSUMPTION (consumption log location).

Rollback/disable: delete the mcp-ayce-director/ directory (and remove any
temporary Hermes mcp_servers registration). Nothing else depends on it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
AYCE_SRC = REPO / "src"

# ---- H3 §6 contract constants -------------------------------------------------

#: request_id grammar (H3 §6): prefix + AYCE id convention.
REQUEST_ID_RE = re.compile(r"^req-[A-Za-z0-9._-]{1,64}$")

#: script_id grammar: a plain allowlist key. Path separators, "..", absolute
#: paths, shell metacharacters and arbitrary filenames are rejected here BY
#: CONSTRUCTION (before any allowlist lookup).
SCRIPT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

#: The ONLY approved command (H3 §6).
APPROVED_COMMAND = "trigger_golden_path"

#: Exact request fields (H3 §6): no more, no fewer.
REQUIRED_FIELDS = frozenset({"request_id", "command", "script_id", "reason"})

MAX_REASON_LEN = 2000

#: Bounded EXPERIMENTAL subprocess timeout (H4; H3 §9 left timeout tuning to
#: the MCP tool timeout). Server-side, operator-configurable via
#: AYCE_DIRECTOR_TIMEOUT_S — never caller-configurable. The default covers
#: one fixture-backed Golden Path run.
DEFAULT_TIMEOUT_S = 600.0

# ---- operator-controlled paths (never caller-controlled) ----------------------


def _ledger_path() -> Path:
    override = os.environ.get("AYCE_DIRECTOR_LEDGER", "").strip()
    return Path(override) if override else HERE / "requests.json"


def _scripts_path() -> Path:
    """The script allowlist file. Operator-maintained by default; the env
    override exists for test isolation ONLY (never caller-controllable)."""
    override = os.environ.get("AYCE_DIRECTOR_SCRIPTS", "").strip()
    return Path(override) if override else HERE / "scripts.json"


def _load_allowlist() -> dict:
    raw = json.loads(_scripts_path().read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _pinned_interpreter() -> str:
    """The child interpreter: AYCE's own project venv python (it has the
    AYCE dependencies). Server-controlled; the caller names no executable."""
    override = os.environ.get("AYCE_DIRECTOR_PYTHON", "").strip()
    if override:
        return override
    candidate = REPO / ".venv" / "Scripts" / "python.exe"
    if candidate.is_file():
        return str(candidate)
    candidate = REPO / ".venv" / "bin" / "python"
    if candidate.is_file():
        return str(candidate)
    return sys.executable


def _timeout_s() -> float:
    raw = os.environ.get("AYCE_DIRECTOR_TIMEOUT_S", "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_TIMEOUT_S


# ---- Stage 9: policy execution context (READ-ONLY consumption) ---------------


def _policy_db_path() -> Path:
    """The canonical Stage 8 policy database (READ-ONLY for this server)."""
    override = os.environ.get("AYCE_POLICY_DB", "").strip()
    return Path(override) if override else REPO / "data" / "policy" / \
        "policy.sqlite3"


def _policy_scope_candidates() -> tuple[str, ...]:
    """Ordered scope candidates (most specific FIRST; §6/§22). Operator-
    controlled via AYCE_POLICY_SCOPE_CANDIDATES; default "global". The
    resolution never invents or widens scopes."""
    raw = os.environ.get("AYCE_POLICY_SCOPE_CANDIDATES", "").strip()
    if not raw:
        return ("global",)
    candidates = tuple(p.strip() for p in raw.split(",") if p.strip())
    return candidates or ("global",)


def _policy_consumption_path(ledger_path: Path) -> Path:
    """The append-only policy consumption evidence log (§13). Lives next
    to the request ledger by default (same operator-controlled surface,
    same test isolation); AYCE_POLICY_CONSUMPTION overrides."""
    override = os.environ.get("AYCE_POLICY_CONSUMPTION", "").strip()
    if override:
        return Path(override)
    return Path(ledger_path).parent / "policy_consumption.jsonl"


def _build_policy_context() -> dict:
    """Consume the ACTIVE ProductionPolicy through the canonical READ-ONLY
    Stage 8 library. This is the ONLY policy access the director has: a
    mode=ro read that produces a structured, hashed execution context.
    Raises PolicyError on a corrupted/unverifiable active policy (fail
    closed) — it NEVER silently degrades to 'no policy'."""
    if str(AYCE_SRC) not in sys.path:
        sys.path.insert(0, str(AYCE_SRC))
    from ayce.policy.execution import build_execution_context

    return build_execution_context(_policy_db_path(),
                                   scope_candidates=_policy_scope_candidates())


def _policy_summary(context: dict) -> dict:
    """Compact, auditable policy reference for ledger/result records."""
    policy = context.get("policy") or {}
    return {
        "policy_status": context.get("policy_status"),
        "policy_id": policy.get("policy_id"),
        "policy_version": policy.get("policy_version"),
        "scope": policy.get("scope"),
        "policy_content_hash": policy.get("policy_content_hash"),
        "context_hash": context.get("context_hash"),
        "resolved_scope_candidates": (context.get("resolved") or {}).get(
            "scope_candidates"),
    }


def _policy_decision(context: dict) -> dict:
    """The structured decision explanation (§12): factual wording only —
    a policy preference is NEVER phrased as a requirement, a guarantee,
    or a causal claim."""
    decision = "trigger_golden_path"
    if context.get("policy_status") != "active":
        return {
            "decision": decision,
            "policy_status": context.get("policy_status"),
            "reason": "no active production policy; existing behavior "
                      "unchanged",
        }
    policy = context.get("policy") or {}
    if str(AYCE_SRC) not in sys.path:
        sys.path.insert(0, str(AYCE_SRC))
    from ayce.policy.execution import preferred_variant

    preferred = preferred_variant(context)
    explanation = {
        "decision": decision,
        "policy_status": "active",
        "policy": {"policy_id": policy.get("policy_id"),
                   "policy_version": policy.get("policy_version"),
                   "scope": policy.get("scope"),
                   "rule_kind": (policy.get("rules") or [{}])[0].get("kind")
                   if policy.get("rules") else None},
        "reason": "active production policy attached as execution context",
    }
    if preferred is not None:
        explanation["variant_preference"] = {
            "preferred": preferred,
            "binding": "preference",
            "note": "preference only — production proceeds "
                    "deterministically; this is NOT a requirement and "
                    "NOT a causal claim",
        }
    return explanation


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---- request ledger (H3 §11) ---------------------------------------------------

_LEDGER_IO_LOCK = threading.Lock()   # ledger read-modify-write serialization
_RUN_LOCK = threading.Lock()         # single-flight (single-process model)


def _ledger_load(path: Path) -> dict:
    if not path.is_file():
        return {"version": 1, "requests": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        # A corrupt ledger must never crash the boundary; quarantine it for
        # forensics and continue empty (no silent overwrite of evidence).
        corrupt = path.with_suffix(".corrupt.json")
        try:
            os.replace(path, corrupt)
        except OSError:
            pass
        return {"version": 1, "requests": []}
    if not isinstance(data, dict) or not isinstance(data.get("requests"), list):
        return {"version": 1, "requests": []}
    return data


def _ledger_save(path: Path, ledger: dict) -> None:
    """Atomic write (tmp + os.replace — the AYCE convention)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _ledger_record(path: Path, record: dict) -> None:
    with _LEDGER_IO_LOCK:
        ledger = _ledger_load(path)
        ledger["requests"].append(record)
        _ledger_save(path, ledger)


def _ledger_find(path: Path, request_id: str) -> dict | None:
    with _LEDGER_IO_LOCK:
        ledger = _ledger_load(path)
        for record in ledger["requests"]:
            if record.get("request_id") == request_id:
                return record
    return None


def _ledger_update(path: Path, request_id: str, **fields: Any) -> None:
    with _LEDGER_IO_LOCK:
        ledger = _ledger_load(path)
        for record in ledger["requests"]:
            if record.get("request_id") == request_id and record.get("status") == "running":
                record.update(fields)
                break
        _ledger_save(path, ledger)


def _ledger_has_running(path: Path) -> bool:
    with _LEDGER_IO_LOCK:
        ledger = _ledger_load(path)
        return any(r.get("status") == "running" for r in ledger["requests"])


# ---- validation (H3 §6 validation pipeline) ------------------------------------


def _rejection(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


def validate_request(payload: Any) -> tuple[dict | None, dict | None]:
    """Strict validation. Returns (normalized, None) or (None, rejection).

    Order follows H3 §6: schema -> command allowlist -> script_id allowlist.
    Nothing here ever executes anything.
    """
    if not isinstance(payload, dict):
        return None, _rejection("invalid_request", "request must be a JSON object")

    keys = set(payload.keys())
    if keys != REQUIRED_FIELDS:
        missing = sorted(REQUIRED_FIELDS - keys)
        extra = sorted(keys - REQUIRED_FIELDS)
        detail = []
        if missing:
            detail.append(f"missing fields: {missing}")
        if extra:
            detail.append(f"unsupported fields: {extra}")
        return None, _rejection(
            "invalid_request",
            "exact fields required (request_id, command, script_id, reason); "
            + "; ".join(detail),
        )

    request_id = payload["request_id"]
    if not isinstance(request_id, str) or not REQUEST_ID_RE.match(request_id):
        return None, _rejection(
            "invalid_request_id", "request_id must match ^req-[A-Za-z0-9._-]{1,64}$"
        )

    command = payload["command"]
    if not isinstance(command, str) or command != APPROVED_COMMAND:
        return None, _rejection(
            "unknown_command",
            f"unknown command {command!r}; the only approved command is {APPROVED_COMMAND!r}",
        )

    script_id = payload["script_id"]
    if not isinstance(script_id, str) or not SCRIPT_ID_RE.match(script_id):
        return None, _rejection(
            "unknown_script",
            "script_id must be a plain allowlist identifier (lowercase "
            "letters/digits/underscore/hyphen; no paths, no '..', no "
            "separators, no metacharacters)",
        )

    reason = payload["reason"]
    if not isinstance(reason, str) or len(reason) > MAX_REASON_LEN:
        return None, _rejection(
            "invalid_request", f"reason must be a string of at most {MAX_REASON_LEN} characters"
        )

    allowlist = _load_allowlist()
    entry = allowlist.get(script_id)
    if entry is None:
        return None, _rejection(
            "unknown_script",
            f"unknown script_id {script_id!r}; approved ids: {sorted(allowlist)}",
        )

    return (
        {
            "request_id": request_id,
            "command": command,
            "script_id": script_id,
            "reason": reason,
            "resolved": {
                "script": str((REPO / entry["script"]).resolve()),
                "assets_dir": str((REPO / entry["assets_dir"]).resolve()),
                "narration_dir": str((REPO / entry["narration_dir"]).resolve()),
            },
        },
        None,
    )


# ---- fixed subprocess execution (H3 §7/§10) -------------------------------------


def _child_env() -> dict:
    """Server-controlled child environment: the server's own environment
    (so ffmpeg/ffprobe resolve) with PYTHONPATH pinned to AYCE's src. The
    caller contributes NO environment. Never logged."""
    env = {k: v for k, v in os.environ.items()}
    env["PYTHONPATH"] = str(AYCE_SRC)
    return env


def _build_argv(resolved: dict) -> list[str]:
    """The ONE fixed argv template (H3 §7). Every element is
    server-controlled; the caller supplies nothing that enters argv."""
    return [
        _pinned_interpreter(),
        "-m", "ayce", "run",
        resolved["script"],
        "--assets-dir", resolved["assets_dir"],
        "--narration-dir", resolved["narration_dir"],
        "--json",
    ]


def _default_runner(argv: list[str], cwd: str, env: dict, timeout_s: float) -> dict:
    """The real subprocess seam. subprocess.run never uses a shell; on
    timeout it KILLS the child and waits for it before raising.

    Windows/MCP note (verified during H4): a child spawned by a stdio MCP
    server INHERITS the server's OVERLAPPED-mode transport pipe handles,
    and CPython's interpreter startup (Py_InitializeFromConfig) then
    blocks forever in synchronous stdio I/O on those inherited handles.
    The boundary therefore gives the child completely clean stdio:
    stdin=DEVNULL and close_fds=True so NO transport handle leaks into
    the production child. AYCE reads no stdin and both output streams
    are freshly captured pipes."""
    started_at = _utcnow()
    try:
        proc = subprocess.run(
            argv, cwd=cwd, env=env, timeout=timeout_s,
            capture_output=True, text=True, shell=False,
            stdin=subprocess.DEVNULL, close_fds=True,
        )
        return {
            "timed_out": False,
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "started_at": started_at,
            "finished_at": _utcnow(),
        }
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        err = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
        return {
            "timed_out": True,
            "exit_code": None,
            "stdout": out,
            "stderr": err,
            "started_at": started_at,
            "finished_at": _utcnow(),
        }


def _stub_runner_factory() -> Callable | None:
    """TEST SEAM ONLY (H4): when AYCE_DIRECTOR_STUB_RUNNER=1 and
    AYCE_DIRECTOR_STUB_RESULT names a JSON file {exit_code, stdout, stderr,
    timed_out}, the server substitutes the recorded outcome instead of
    spawning AYCE. Disabled unless the operator/test harness sets the env;
    the caller cannot set server environment through the tool API."""
    if os.environ.get("AYCE_DIRECTOR_STUB_RUNNER", "") != "1":
        return None
    path = os.environ.get("AYCE_DIRECTOR_STUB_RESULT", "").strip()
    payload = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
    started = _utcnow()
    return lambda argv, cwd, env, timeout_s: {
        "timed_out": bool(payload.get("timed_out", False)),
        "exit_code": payload.get("exit_code", 0),
        "stdout": payload.get("stdout", ""),
        "stderr": payload.get("stderr", ""),
        "started_at": started,
        "finished_at": _utcnow(),
    }


def _parse_report(stdout: str) -> dict | None:
    try:
        data = json.loads(stdout.strip())
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


# ---- the one write capability ----------------------------------------------------


def handle_request(payload: Any, *, runner: Callable | None = None,
                   ledger_path: str | Path | None = None) -> dict:
    """Validate + (maybe) execute one director request; return the
    structured result. This is the entire write boundary."""
    path = Path(ledger_path) if ledger_path is not None else _ledger_path()

    normalized, error = validate_request(payload)
    request_id = payload.get("request_id") if isinstance(payload, dict) else None

    if error is not None:
        # H3 §12: the ledger records every request, including rejects.
        _ledger_record(path, {
            "request_id": request_id if isinstance(request_id, str) else None,
            "command": payload.get("command") if isinstance(payload, dict) else None,
            "script_id": payload.get("script_id") if isinstance(payload, dict) else None,
            "status": "rejected",
            "error": error["error"],
            "requested_at": _utcnow(),
        })
        return error

    request = normalized

    # Stage 9 — the policy-aware decision boundary (§9): consume the
    # active ProductionPolicy ONCE per decision (READ-ONLY, canonical,
    # hashed). A corrupted/unverifiable active policy FAILS CLOSED here:
    # the request is rejected and NO run is launched — it is never
    # silently treated as 'no policy' (§23).
    try:
        policy_context = _build_policy_context()
    except Exception as exc:  # noqa: BLE001 — classified fail-closed below
        from ayce.policy.errors import PolicyError as _PE  # noqa
        if isinstance(exc, _PE):
            code, message, details = exc.code, exc.message, exc.details
        else:
            code, message, details = ("policy_read_failed",
                                      f"policy context could not be read: "
                                      f"{exc}", {})
        _ledger_record(path, {
            "request_id": request["request_id"],
            "command": request["command"],
            "script_id": request["script_id"],
            "reason": request["reason"],
            "requested_by": "hermes",
            "status": "rejected",
            "policy_status": "invalid",
            "error": {"code": code, "message": message},
            "requested_at": _utcnow(),
        })
        rejection = _rejection(code, message)
        if details:
            rejection["error"]["details"] = details
        return rejection

    # Idempotency + single-flight under one process-level lock (H3 §11:
    # single-flight is a ledger state check, no lock daemon).
    with _RUN_LOCK:
        existing = _ledger_find(path, request["request_id"])
        if existing is not None:
            identical = (
                existing.get("command") == request["command"]
                and existing.get("script_id") == request["script_id"]
                and existing.get("reason") == request["reason"]
            )
            if identical:
                return {
                    "ok": False,
                    "request_id": request["request_id"],
                    "error": {
                        "code": "duplicate_request",
                        "message": (
                            "request_id already processed; no second run launched. "
                            f"original status: {existing.get('status')}"
                        ),
                    },
                    "original": {
                        "status": existing.get("status"),
                        "run_id": existing.get("run_id"),
                        "job_id": existing.get("job_id"),
                        "qa_verdict": existing.get("qa_verdict"),
                        "exit_code": existing.get("exit_code"),
                    },
                }
            return _rejection(
                "duplicate_request",
                "request_id reused with a CONFLICTING payload (command/script_id/reason "
                "differ); rejected, nothing executed",
            )

        if _ledger_has_running(path):
            _ledger_record(path, {
                "request_id": request["request_id"],
                "command": request["command"],
                "script_id": request["script_id"],
                "reason": request["reason"],
                "status": "rejected",
                "error": {"code": "busy",
                          "message": "another director request is in flight (single-flight)"},
                "requested_at": _utcnow(),
            })
            return _rejection(
                "busy",
                "another director request is in flight (single-flight); "
                "observe it via the read-only tools",
            )

        argv = _build_argv(request["resolved"])
        # Ledger BEFORE spawn: request_id -> run correlation is durable even
        # if the process dies mid-run (H3 §11).
        _ledger_record(path, {
            "request_id": request["request_id"],
            "command": request["command"],
            "script_id": request["script_id"],
            "reason": request["reason"],
            "requested_by": "hermes",
            "status": "running",
            "policy": _policy_summary(policy_context),
            "argv": argv,
            "timeout_s": _timeout_s(),
            "requested_at": _utcnow(),
        })

    runner = runner or _stub_runner_factory() or _default_runner
    outcome = runner(argv, str(REPO), _child_env(), _timeout_s())

    timed_out = bool(outcome.get("timed_out"))
    exit_code = outcome.get("exit_code")
    stdout = outcome.get("stdout") or ""
    stderr = outcome.get("stderr") or ""
    report = _parse_report(stdout)
    stderr_tail = (stderr or "").strip()[-2000:]
    run_id = report.get("run_id") if report is not None else None

    # Stage 9 — durable consumption evidence (§13): the decision consumed
    # the policy context above; record WHICH run/decision/policy/version/
    # hash — idempotently, content-addressed. A consumption failure never
    # falsifies the run outcome; it is surfaced truthfully instead.
    # Stage 10 — the event additionally becomes a FIRST-CLASS RUN ARTIFACT
    # (ArtifactKind.POLICY_CONSUMPTION, sha256-pinned, idempotent) when the
    # run directory exists, so consumption is queryable through the
    # standard artifact lineage / read-only MCP. Bounded: this writes ONLY
    # the consumption evidence artifact + its registry entry.
    policy_consumption = None
    try:
        if str(AYCE_SRC) not in sys.path:
            sys.path.insert(0, str(AYCE_SRC))
        from ayce.policy.execution import append_consumption

        consumption = append_consumption(
            _policy_consumption_path(path), run_id=run_id,
            decision_id=request["request_id"],
            decision="trigger_golden_path", context=policy_context,
            now=_utcnow())
        policy_consumption = {
            "consumption_id": consumption["consumption_id"],
            "created": consumption["created"],
            "log": str(_policy_consumption_path(path)),
        }
        _ledger_update(path, request["request_id"],
                       policy_consumption_id=consumption["consumption_id"])
        # Stage 10 artifact registration (observability only)
        report_run_dir = report.get("run_dir") if report else None
        if run_id and report_run_dir:
            resolved_run_dir = (REPO / report_run_dir).resolve()
            # containment guard: the artifact must land inside this
            # repository's runs root
            runs_root = (REPO / "data" / "runs").resolve()
            if runs_root in resolved_run_dir.parents and \
                    resolved_run_dir.is_dir():
                from ayce.policy.observability import (
                    record_consumption_artifact)
                artifact = record_consumption_artifact(
                    resolved_run_dir, run_id, consumption["event"])
                policy_consumption["artifact"] = {
                    "registered": artifact.get("registered", False),
                    "artifact_id": artifact.get("artifact_id"),
                    "sha256": artifact.get("sha256"),
                    "reason": artifact.get("reason"),
                }
                _ledger_update(path, request["request_id"],
                               policy_consumption_artifact_id=
                               artifact.get("artifact_id"))
    except Exception as exc:  # noqa: BLE001 — classified, never falsifying
        from ayce.policy.errors import PolicyError as _PE  # noqa
        if isinstance(exc, _PE):
            policy_consumption = {"error": {"code": exc.code,
                                            "message": exc.message}}
        else:
            policy_consumption = {"error": {
                "code": "policy_consumption_failed",
                "message": f"consumption event could not be recorded: {exc}"}}

    if timed_out:
        # Truthful: never claim a successful production run. subprocess.run
        # KILLED the child; AYCE's run dir keeps truthful partial state.
        _ledger_update(
            path, request["request_id"], status="timeout",
            finished_at=outcome.get("finished_at"),
            run_id=None, job_id=None, qa_verdict=None,
            error={"code": "timeout", "message": (
                f"AYCE subprocess exceeded the experimental {timeout_display(_timeout_s())}s "
                "bound and was terminated; the run directory keeps truthful partial checkpoints")},
        )
        return {
            "ok": False,
            "request_id": request["request_id"],
            "policy": _policy_summary(policy_context),
            "policy_decision": _policy_decision(policy_context),
            "policy_consumption": policy_consumption,
            "error": {"code": "timeout", "message": (
                "execution exceeded the experimental timeout bound; the child "
                "process was terminated; no successful run is claimed")},
        }

    if report is not None:
        run_id = report.get("run_id")
        job_id = report.get("job_id")
        qa_verdict = report.get("qa_verdict")
        failed_stage = report.get("failed_stage")
        ayce_error = report.get("error")
        stages = report.get("stages") or []
    else:
        run_id = job_id = qa_verdict = failed_stage = ayce_error = None
        stages = []

    if exit_code == 0 and report is not None and report.get("ok") is True:
        # QA-FAIL is NOT an execution failure (verified AYCE P5.5 semantics):
        # ok=True + qa_verdict FAIL is a SUCCEEDED run with truthful evidence.
        _ledger_update(
            path, request["request_id"], status="succeeded",
            finished_at=outcome.get("finished_at"), exit_code=exit_code,
            run_id=run_id, job_id=job_id, qa_verdict=qa_verdict,
        )
        return {
            "ok": True,
            "request_id": request["request_id"],
            "command": request["command"],
            "script_id": request["script_id"],
            "status": "succeeded",
            "policy": _policy_summary(policy_context),
            "policy_decision": _policy_decision(policy_context),
            "policy_consumption": policy_consumption,
            "run_id": run_id,
            "job_id": job_id,
            "qa_verdict": qa_verdict,
            "exit_code": exit_code,
            "failed_stage": failed_stage,
            "error": ayce_error,
            "stages": stages,
        }

    if exit_code == 2:
        # AYCE validates before creating any run dir: pre-run failure.
        code = "failed_pre_run"
        message = (f"AYCE rejected the run before execution (exit 2): "
                   f"{stderr_tail or ayce_error or 'configuration/input error'}")
    else:
        code = "execution_failed"
        message = f"AYCE pipeline execution failed (exit {exit_code}); failed_stage={failed_stage!r}"
        if ayce_error:
            message += f"; error={ayce_error}"
        if report is None:
            message += "; --json report was unparseable"
        if stderr_tail:
            message += f"; stderr: {stderr_tail}"

    _ledger_update(
        path, request["request_id"], status="failed",
        finished_at=outcome.get("finished_at"), exit_code=exit_code,
        run_id=run_id, job_id=job_id, qa_verdict=qa_verdict,
        failed_stage=failed_stage,
        error={"code": code, "message": message},
    )
    return {
        "ok": False,
        "request_id": request["request_id"],
        "policy": _policy_summary(policy_context),
        "policy_decision": _policy_decision(policy_context),
        "policy_consumption": policy_consumption,
        "error": {"code": code, "message": message},
        "run_id": run_id,
        "job_id": job_id,
        "qa_verdict": qa_verdict,
        "exit_code": exit_code,
        "failed_stage": failed_stage,
    }


def timeout_display(value: float) -> str:
    return f"{value:g}"


# ---- Stage 2 research capability (execute_research_slice) -------------------------
#
# A SECOND tool on the SAME server (never a second MCP server). The
# write boundary of trigger_golden_path is untouched: no shared ledger,
# no shared locks, no changes to H3/H4 validation or execution. The
# research capability invokes the LOCAL worker through ONE fixed argv
# template (<pinned python> -m ayce.research --json) with the validated
# request JSON passed on the child's stdin. The MCP caller can never
# supply argv, shell, environment, cwd, timeout, or output paths.

RESEARCH_MAX_OBJECTIVE = 2000
RESEARCH_MAX_TEXT = 300
RESEARCH_MAX_CHANNELS = 5
#: channel grammar: @handle / plain name / https URL fragment. No shell
#: metacharacters, quotes, or separators — rejected BY CONSTRUCTION.
RESEARCH_CHANNEL_RE = re.compile(r"^[@A-Za-z0-9_.\-/ ]{1,200}$")

RESEARCH_ALLOWED_FIELDS = frozenset({
    "objective", "niche", "query", "target_channels", "max_outliers", "max_videos",
})

DEFAULT_RESEARCH_TIMEOUT_S = 300.0


def _research_ledger_path() -> Path:
    override = os.environ.get("AYCE_DIRECTOR_RESEARCH_LEDGER", "").strip()
    return Path(override) if override else HERE / "research_requests.json"


def _research_timeout_s() -> float:
    raw = os.environ.get("AYCE_RESEARCH_TIMEOUT_S", "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_RESEARCH_TIMEOUT_S


def validate_research_request(payload: Any) -> tuple[dict | None, dict | None]:
    """Strict research input validation. Returns (normalized, None) or
    (None, rejection). Nothing here ever executes anything."""
    if not isinstance(payload, dict):
        return None, _rejection("invalid_request", "research request must be a JSON object")

    keys = set(payload.keys())
    extra = sorted(keys - RESEARCH_ALLOWED_FIELDS)
    if extra:
        return None, _rejection(
            "invalid_request",
            f"unsupported research fields: {extra}; allowed: {sorted(RESEARCH_ALLOWED_FIELDS)}",
        )

    objective = payload.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        return None, _rejection(
            "invalid_request", "objective is required and must be a non-empty string")
    if len(objective) > RESEARCH_MAX_OBJECTIVE:
        return None, _rejection(
            "invalid_request",
            f"objective must be at most {RESEARCH_MAX_OBJECTIVE} characters",
        )

    normalized: dict[str, Any] = {"objective": objective.strip()}

    for key in ("niche", "query"):
        value = payload.get(key)
        if value is None:
            normalized[key] = None
            continue
        if not isinstance(value, str) or len(value) > RESEARCH_MAX_TEXT:
            return None, _rejection(
                "invalid_request",
                f"{key} must be a string of at most {RESEARCH_MAX_TEXT} characters")
        normalized[key] = value.strip() or None

    channels = payload.get("target_channels")
    if channels is None:
        channels = []
    if not isinstance(channels, list) or len(channels) > RESEARCH_MAX_CHANNELS:
        return None, _rejection(
            "invalid_request",
            f"target_channels must be a list of at most {RESEARCH_MAX_CHANNELS} strings",
        )
    cleaned_channels: list[str] = []
    for channel in channels:
        if not isinstance(channel, str) or not RESEARCH_CHANNEL_RE.match(channel.strip()):
            return None, _rejection(
                "invalid_request",
                "target_channels entries must match ^[@A-Za-z0-9_.\\-/ ]{1,200}$ "
                f"(no shell metacharacters); rejected: {channel!r}",
            )
        cleaned_channels.append(channel.strip())
    normalized["target_channels"] = cleaned_channels

    max_outliers = payload.get("max_outliers", 5)
    if isinstance(max_outliers, bool) or not isinstance(max_outliers, int) or not 1 <= max_outliers <= 10:
        return None, _rejection("invalid_request", "max_outliers must be an integer in [1, 10]")
    normalized["max_outliers"] = max_outliers

    max_videos = payload.get("max_videos", 6)
    if isinstance(max_videos, bool) or not isinstance(max_videos, int) or not 1 <= max_videos <= 12:
        return None, _rejection("invalid_request", "max_videos must be an integer in [1, 12]")
    normalized["max_videos"] = max_videos

    return normalized, None


def _research_argv() -> list[str]:
    """The ONE fixed research argv template. Every element is
    server-controlled; the caller supplies nothing that enters argv."""
    return [_pinned_interpreter(), "-m", "ayce.research", "--json"]


def _research_default_runner(
    argv: list[str], cwd: str, env: dict, timeout_s: float, stdin_text: str
) -> dict:
    """The real research subprocess seam: no shell, bounded, request JSON
    on a fresh stdin pipe (close_fds + captured pipes — no transport
    handle leaks into the child, same Windows model as H4)."""
    started_at = _utcnow()
    try:
        proc = subprocess.run(
            argv, cwd=cwd, env=env, input=stdin_text,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            shell=False, timeout=timeout_s, close_fds=True,
        )
        return {
            "timed_out": False,
            "exit_code": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "started_at": started_at,
            "finished_at": _utcnow(),
        }
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        err = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
        return {
            "timed_out": True, "exit_code": None, "stdout": out, "stderr": err,
            "started_at": started_at, "finished_at": _utcnow(),
        }
    except OSError as exc:
        return {
            "timed_out": False, "exit_code": 127, "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "started_at": started_at, "finished_at": _utcnow(),
        }


def _ledger_update_research(path: Path, digest: str, **fields: Any) -> None:
    """Update the most recent 'running' research record with this digest."""
    with _LEDGER_IO_LOCK:
        ledger = _ledger_load(path)
        for record in reversed(ledger["requests"]):
            if (record.get("kind") == "research"
                    and record.get("digest") == digest
                    and record.get("status") == "running"):
                record.update(fields)
                record["finished_at"] = _utcnow()
                break
        _ledger_save(path, ledger)


def handle_research_request(payload: Any, *, runner: Callable | None = None,
                            ledger_path: str | Path | None = None) -> dict:
    """Validate + execute one research slice; return the structured
    result. Idempotent by payload digest (a succeeded identical slice is
    returned from the ledger instead of re-running). The
    trigger_golden_path ledger and locks are NOT shared — H4 untouched."""
    path = Path(ledger_path) if ledger_path is not None else _research_ledger_path()

    normalized, error = validate_research_request(payload)
    if error is not None:
        _ledger_record(path, {
            "kind": "research",
            "research_id": None,
            "status": "rejected",
            "error": error["error"],
            "requested_at": _utcnow(),
        })
        return error

    request = normalized
    digest = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    # Idempotent reuse of an already-succeeded identical slice.
    with _LEDGER_IO_LOCK:
        ledger = _ledger_load(path)
        for record in ledger["requests"]:
            if (record.get("kind") == "research"
                    and record.get("digest") == digest
                    and record.get("status") == "succeeded"
                    and isinstance(record.get("result"), dict)):
                return {**record["result"], "duplicate": True}

    _ledger_record(path, {
        "kind": "research",
        "research_id": None,
        "digest": digest,
        "request": request,
        "status": "running",
        "requested_at": _utcnow(),
    })

    run = runner or _research_default_runner
    # the worker's JSON stdout is UTF-8 by contract (Windows locale-safe)
    child_env = {**_child_env(), "PYTHONIOENCODING": "utf-8"}
    outcome = run(_research_argv(), str(REPO), child_env, _research_timeout_s(),
                  json.dumps(request))

    if outcome.get("timed_out"):
        message = (f"research worker exceeded the {_research_timeout_s():g}s bound "
                   "and was terminated")
        _ledger_update_research(path, digest, status="timeout",
                                error={"code": "timeout", "message": message})
        return {"ok": False, "error": {"code": "timeout", "message": message}}

    envelope = _parse_report(outcome.get("stdout") or "")
    stderr_tail = (outcome.get("stderr") or "").strip()[-1000:]

    if isinstance(envelope, dict) and envelope.get("ok") is True:
        result = {
            "ok": True,
            "research_id": envelope.get("research_id"),
            "status": envelope.get("status"),
            "candidate_count": envelope.get("candidate_count"),
            "artifact_path": envelope.get("artifact_path"),
            "artifact": envelope.get("artifact"),
        }
        _ledger_update_research(path, digest, status="succeeded",
                                research_id=result["research_id"], result=result)
        return result

    if isinstance(envelope, dict) and envelope.get("ok") is False:
        err = envelope.get("error") or {}
        code = err.get("code") if isinstance(err.get("code"), str) else "extraction_failed"
        message = err.get("message") or "research worker reported an error"
        _ledger_update_research(path, digest, status="failed",
                                error={"code": code, "message": message})
        return {"ok": False, "error": {"code": code, "message": message}}

    code = "extraction_failed"
    message = ("research worker produced no parseable result envelope"
               + (f"; stderr: {stderr_tail}" if stderr_tail else ""))
    _ledger_update_research(path, digest, status="failed",
                            error={"code": code, "message": message})
    return {"ok": False, "error": {"code": code, "message": message}}


# ---- Stage 2.5 script-brief capability (propose_script_brief) ----------------------
#
# A THIRD tool on the SAME server. It exposes the ALREADY-VERIFIED Stage 1
# deterministic bridge (src/ayce/research/brief.py) through the proven
# research-subprocess model: ONE fixed argv template
# (<pinned python> -m ayce.research --brief --json) with the artifact JSON
# on the child's stdin. The caller supplies ONLY a research artifact
# identifier — never a path, argv element, shell string, or environment.
#
# The generated ScriptInput is treated strictly as DATA conforming to the
# existing P1-B contract: it is persisted under generated_scripts/ (server-
# controlled) and registered into the scripts.json allowlist so the
# UNCHANGED trigger_golden_path boundary can execute it. There is no
# fixture fallback: a failed brief NEVER executes the documentary script.

RESEARCH_ARTIFACT_ID_RE = re.compile(r"^res-[A-Za-z0-9._-]{1,64}$")

#: Generated allowlist keys are namespaced so they can never collide with
#: operator-approved entries by accident.
GENERATED_SCRIPT_PREFIX = "brief-"

#: The brief worker is fast and bounded (no network): a small experimental
#: bound, operator-configurable, never caller-configurable.
DEFAULT_BRIEF_TIMEOUT_S = 120.0

BRIEF_ALLOWED_FIELDS = frozenset({"research_id"})


def _brief_ledger_path() -> Path:
    override = os.environ.get("AYCE_DIRECTOR_BRIEF_LEDGER", "").strip()
    return Path(override) if override else HERE / "brief_requests.json"


def _brief_timeout_s() -> float:
    raw = os.environ.get("AYCE_DIRECTOR_BRIEF_TIMEOUT_S", "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_BRIEF_TIMEOUT_S


def _research_store_dir() -> Path:
    """The ONLY research artifact store this tool reads from. Mirrors the
    worker's default (<data_dir>/research) with the AYCE_DATA_DIR override.
    The caller can never name a location."""
    override = os.environ.get("AYCE_DATA_DIR", "").strip()
    base = Path(override) if override else REPO / "data"
    return base / "research"


def _generated_scripts_dir() -> Path:
    override = os.environ.get("AYCE_DIRECTOR_GENERATED_SCRIPTS", "").strip()
    return Path(override) if override else HERE / "generated_scripts"


def _script_id_for_research(research_id: str) -> str | None:
    """Deterministic generated-script identifier: brief-<artifact token>.
    Returns None when the token cannot form a valid allowlist key (the
    caller then receives a truthful rejection, never a fallback)."""
    token = research_id.split("-")[-1]
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", token):
        return None
    return f"{GENERATED_SCRIPT_PREFIX}{token}"


def validate_brief_request(payload: Any) -> tuple[dict | None, dict | None]:
    """Strict brief input validation. Returns (normalized, None) or
    (None, rejection). Nothing here ever executes anything."""
    if not isinstance(payload, dict):
        return None, _rejection("invalid_request", "brief request must be a JSON object")

    extra = sorted(set(payload.keys()) - BRIEF_ALLOWED_FIELDS)
    if extra:
        return None, _rejection(
            "invalid_request", f"unsupported fields: {extra}; expected only research_id")

    research_id = payload.get("research_id")
    if not isinstance(research_id, str) or not RESEARCH_ARTIFACT_ID_RE.match(research_id):
        return None, _rejection(
            "invalid_request",
            "research_id must match ^res-[A-Za-z0-9._-]{1,64}$ (an artifact "
            "identifier, NEVER a path)",
        )

    script_id = _script_id_for_research(research_id)
    if script_id is None:
        return None, _rejection(
            "invalid_request",
            "research_id token cannot form a valid generated script identifier",
        )

    # Resolve INSIDE the controlled store only; the identifier grammar has
    # no separators, so traversal is impossible by construction — the
    # containment check is belt-and-braces.
    store = _research_store_dir()
    artifact_path = (store / f"{research_id}.json").resolve()
    if artifact_path.parent != store.resolve():
        return None, _rejection("invalid_request", "artifact path escaped the research store")
    if not artifact_path.is_file():
        return None, _rejection(
            "unknown_artifact",
            f"no persisted research artifact {research_id!r} in the research store",
        )

    return {
        "research_id": research_id,
        "script_id": script_id,
        "artifact_path": str(artifact_path),
    }, None


def _brief_argv() -> list[str]:
    """The ONE fixed brief argv template. Every element is server-controlled;
    the caller supplies nothing that enters argv (the artifact travels on
    stdin, exactly like the research request)."""
    return [_pinned_interpreter(), "-m", "ayce.research", "--brief", "--json"]


def _ledger_update_brief(path: Path, digest: str, **fields: Any) -> None:
    """Update the most recent 'running' brief record with this digest."""
    with _LEDGER_IO_LOCK:
        ledger = _ledger_load(path)
        for record in reversed(ledger["requests"]):
            if (record.get("kind") == "brief"
                    and record.get("digest") == digest
                    and record.get("status") == "running"):
                record.update(fields)
                record["finished_at"] = _utcnow()
                break
        _ledger_save(path, ledger)


def _looks_like_script_input(script: Any) -> bool:
    """Independent structural sanity check of the worker's output (the
    authoritative validation already happened inside the worker through
    the existing pydantic ScriptInput model). The boundary trusts but
    verifies shape before persisting or registering anything."""
    if not isinstance(script, dict):
        return False
    for key in ("production_id", "title"):
        value = script.get(key)
        if not isinstance(value, str) or not value.strip():
            return False
    scenes = script.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return False
    for scene in scenes:
        if not isinstance(scene, dict):
            return False
        for key in ("narration_text", "visual_description"):
            value = scene.get(key)
            if not isinstance(value, str) or not value.strip():
                return False
    return True


def _register_generated_script(script_id: str, entry: dict, script_sha: str) -> dict | None:
    """Idempotent, conflict-safe allowlist registration. Returns None on
    success (registered now, or byte-identical entry already present) or a
    structured rejection. NEVER overwrites an existing entry: an operator-
    approved script can never be silently replaced by generated content."""
    if not script_id.startswith(GENERATED_SCRIPT_PREFIX):
        return _rejection(
            "script_conflict",
            f"generated script ids must start with {GENERATED_SCRIPT_PREFIX!r}",
        )
    path = _scripts_path()
    with _LEDGER_IO_LOCK:
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return _rejection("script_conflict", "scripts allowlist is corrupt")
        else:
            raw = {}
        existing = raw.get(script_id)
        if existing is not None:
            generated = existing.get("generated") if isinstance(existing, dict) else None
            if isinstance(generated, dict) and generated.get("script_sha256") == script_sha:
                return None  # already registered, byte-identical: no-op
            return _rejection(
                "script_conflict",
                f"script_id {script_id!r} is already registered with different "
                "content; refusing to overwrite (operator decision required)",
            )
        raw[script_id] = entry
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    return None


def handle_brief_request(payload: Any, *, runner: Callable | None = None,
                         ledger_path: str | Path | None = None) -> dict:
    """Validate + execute one propose_script_brief request; return the
    structured result. Idempotent by payload digest (a succeeded identical
    brief is returned from the ledger instead of re-running). The
    trigger_golden_path ledger/locks and the research ledger are NOT shared.
    """
    path = Path(ledger_path) if ledger_path is not None else _brief_ledger_path()

    normalized, error = validate_brief_request(payload)
    if error is not None:
        _ledger_record(path, {
            "kind": "brief",
            "research_id": payload.get("research_id") if isinstance(payload, dict) else None,
            "status": "rejected",
            "error": error["error"],
            "requested_at": _utcnow(),
        })
        return error

    request = normalized
    digest = hashlib.sha256(
        json.dumps({"kind": "brief", "research_id": request["research_id"]},
                   sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    # Idempotent reuse of an already-succeeded identical brief.
    with _LEDGER_IO_LOCK:
        ledger = _ledger_load(path)
        for record in ledger["requests"]:
            if (record.get("kind") == "brief"
                    and record.get("digest") == digest
                    and record.get("status") == "succeeded"
                    and isinstance(record.get("result"), dict)):
                return {**record["result"], "duplicate": True}

    _ledger_record(path, {
        "kind": "brief",
        "research_id": request["research_id"],
        "script_id": request["script_id"],
        "digest": digest,
        "status": "running",
        "requested_at": _utcnow(),
    })

    def _fail(code: str, message: str) -> dict:
        _ledger_update_brief(path, digest, status="failed",
                             error={"code": code, "message": message})
        return {"ok": False, "error": {"code": code, "message": message}}

    artifact_text = Path(request["artifact_path"]).read_text(encoding="utf-8")
    run = runner or _research_default_runner
    child_env = {**_child_env(), "PYTHONIOENCODING": "utf-8"}
    outcome = run(_brief_argv(), str(REPO), child_env, _brief_timeout_s(), artifact_text)

    if outcome.get("timed_out"):
        message = (f"brief worker exceeded the {_brief_timeout_s():g}s bound "
                   "and was terminated")
        _ledger_update_brief(path, digest, status="timeout",
                             error={"code": "timeout", "message": message})
        return {"ok": False, "error": {"code": "timeout", "message": message}}

    exit_code = outcome.get("exit_code")
    stdout = outcome.get("stdout") or ""
    stderr_tail = (outcome.get("stderr") or "").strip()[-1000:]

    if exit_code != 0:
        # Classified Stage 1 worker failures pass through truthfully
        # (7 artifact_validation_failed, 9 insufficient_evidence). A failed
        # brief NEVER registers a script and NEVER falls back to a fixture.
        envelope = _parse_report(stdout)
        code, message = None, "brief worker failed"
        if isinstance(envelope, dict) and envelope.get("ok") is False:
            err = envelope.get("error") or {}
            if isinstance(err.get("code"), str):
                code = err.get("code")
            message = err.get("message") or message
        if code not in ("artifact_validation_failed", "insufficient_evidence"):
            code = "brief_failed"
            message = (f"brief worker failed with exit code {exit_code}: {message}"
                       + (f"; stderr: {stderr_tail}" if stderr_tail else ""))
        return _fail(code, message)

    script = _parse_report(stdout)
    if not _looks_like_script_input(script):
        return _fail("brief_failed",
                     "brief worker produced unparseable or structurally invalid "
                     "ScriptInput output; nothing was persisted or registered"
                     + (f"; stderr: {stderr_tail}" if stderr_tail else ""))

    return _persist_and_register(path, digest, request, script, stdout, outcome)


def _persist_and_register(path: Path, digest: str, request: dict, script: dict,
                          stdout: str, outcome: dict) -> dict:
    """Persist the byte-exact worker output and register it into the
    allowlist (split only to keep functions small)."""
    content = stdout.strip() + "\n"
    content_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    scripts_dir = _generated_scripts_dir()
    scripts_dir.mkdir(parents=True, exist_ok=True)
    script_file = scripts_dir / f"{request['script_id']}.json"
    if script_file.exists():
        existing_sha = hashlib.sha256(script_file.read_bytes()).hexdigest()
        if existing_sha != content_sha:
            return _brief_conflict(path, digest, request, (
                f"generated script {request['script_id']!r} already exists with "
                "different content; refusing to overwrite"))
    else:
        tmp = script_file.with_name(f"{script_file.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, script_file)

    # Register into the allowlist so the UNCHANGED trigger_golden_path
    # boundary can execute it (script/assets/narration resolve relative to
    # the repository root, exactly like the operator-maintained entries).
    script_rel = f"mcp-ayce-director/generated_scripts/{request['script_id']}.json"
    entry = {
        "script": script_rel,
        "assets_dir": "tests/fixtures/asset_provider",
        "narration_dir": "tests/fixtures/narration_fixtures",
        "description": (
            f"Generated ScriptInput (Stage 2.5) from research artifact "
            f"{request['research_id']} — deterministic evidence brief."
        ),
        "generated": {
            "research_id": request["research_id"],
            "script_sha256": content_sha,
            "registered_at": _utcnow(),
        },
    }
    registration_error = _register_generated_script(request["script_id"], entry, content_sha)
    if registration_error is not None:
        err = registration_error["error"]
        _ledger_update_brief(path, digest, status="failed", error=err)
        return {"ok": False, "error": err}

    warnings = [
        line[len("brief_warning: "):].strip()
        for line in (outcome.get("stderr") or "").splitlines()
        if line.startswith("brief_warning: ")
    ][:10]

    result = {
        "ok": True,
        "research_id": request["research_id"],
        "script_id": request["script_id"],
        "production_id": script.get("production_id"),
        "title": script.get("title"),
        "scene_count": len(script.get("scenes", [])),
        "warnings": warnings,
        "script_path": script_rel,
        "script_sha256": content_sha,
        "next_step": (
            "trigger_golden_path with command='trigger_golden_path' and "
            f"script_id='{request['script_id']}' to produce this video"
        ),
    }
    _ledger_update_brief(path, digest, status="succeeded", result=result)
    return result


def _brief_conflict(path: Path, digest: str, request: dict, message: str) -> dict:
    error = {"code": "script_conflict", "message": message}
    _ledger_update_brief(path, digest, status="failed", error=error)
    return {"ok": False, "error": error}


# ---- MCP server (ONE write tool + ONE research tool) -------------------------------

mcp = FastMCP(
    "ayce-director",
    instructions=(
        "H4 controlled director boundary for AYCE, extended by the Stage 2 "
        "research capability and the Stage 2.5 script-brief capability. "
        "Write tool: trigger_golden_path — request ONE approved Golden Path "
        "production run (script_id must be an allowlist identifier; paths/"
        "executables/arguments/shell/environment are NEVER accepted). "
        "Research tool: execute_research_slice — run ONE bounded local "
        "research slice (YouTube ingestion + hook extraction + outlier "
        "analytics + clustering) and receive a compact structured Research "
        "Artifact. Brief tool: propose_script_brief — convert ONE persisted "
        "research artifact (by research_id) into a validated, deterministic "
        "ScriptInput evidence brief; this performs NO code execution and NO "
        "creative generation — the output's script_id is automatically "
        "registered with the Golden Path execution boundary. Observe "
        "resulting runs through the read-only ayce-readonly server."
    ),
)


@mcp.tool()
def trigger_golden_path(request_id: str, command: str, script_id: str, reason: str) -> dict:
    """Request ONE approved AYCE Golden Path production run (write boundary).

    Contract (H3): request_id must match ^req-[A-Za-z0-9._-]{1,64}$;
    command must be exactly "trigger_golden_path"; script_id must be an
    allowlist identifier (NOT a path); reason is audit-only free text.
    Idempotent by request_id; single-flight enforced. Returns the run
    correlation (run_id/job_id/qa_verdict) or a structured rejection.
    """
    return handle_request({
        "request_id": request_id,
        "command": command,
        "script_id": script_id,
        "reason": reason,
    })


@mcp.tool()
def execute_research_slice(
    objective: str,
    niche: str = "",
    query: str = "",
    target_channels: list[str] | None = None,
    max_outliers: int = 5,
) -> dict:
    """Run ONE bounded local research slice and get structured evidence.

    The local worker (OUTSIDE Hermes's context) performs YouTube
    ingestion (quota-free yt-dlp path), opening-hook extraction
    (bounded ~45s window), outlier/velocity analytics, and local topic
    clustering, then returns a COMPACT validated Research Artifact —
    never raw pages, full transcripts, or raw yt-dlp output.

    Args:
        objective: the research objective (required, <= 2000 chars).
        niche: optional niche/context string (<= 300 chars).
        query: optional YouTube search query (<= 300 chars).
        target_channels: optional list (<= 5) of @handles / channel names
            / URLs. Plain identifiers only — no shell metacharacters.
        max_outliers: number of exemplar outliers in the compact
            envelope (1..10, default 5).

    Returns {ok: true, research_id, status, candidate_count, artifact}
    or {ok: false, error: {code, message}} with classified codes:
    invalid_request, timeout, rate_limited, source_unavailable,
    extraction_failed, artifact_validation_failed.
    """
    payload: dict = {"objective": objective, "max_outliers": max_outliers}
    if niche:
        payload["niche"] = niche
    if query:
        payload["query"] = query
    if target_channels:
        payload["target_channels"] = target_channels
    return handle_research_request(payload)


@mcp.tool()
def propose_script_brief(research_id: str) -> dict:
    """Convert ONE persisted research artifact into a validated script brief.

    Runs the deterministic Stage 1 bridge (OUTSIDE Hermes's context, no LLM,
    no network): the persisted ResearchArtifact identified by research_id is
    converted into a validated ScriptInput evidence brief whose narration is
    derived ONLY from the artifact's recorded evidence. This is a data
    transformation, NOT code execution and NOT creative generation.

    Args:
        research_id: the persisted research artifact identifier, matching
            ^res-[A-Za-z0-9._-]{1,64}$ (e.g. from execute_research_slice).
            NEVER a filesystem path — arbitrary paths are rejected.

    Returns {ok: true, research_id, script_id, production_id, title,
    scene_count, warnings, script_path, script_sha256, next_step} where
    script_id is already registered with the Golden Path execution
    boundary (pass it to trigger_golden_path to produce the video), or
    {ok: false, error: {code, message}} with classified codes:
    invalid_request, unknown_artifact, artifact_validation_failed,
    insufficient_evidence, script_conflict, brief_failed, timeout.
    Idempotent by research artifact: repeating the same request returns the
    recorded brief without regenerating.
    """
    return handle_brief_request({"research_id": research_id})


if __name__ == "__main__":
    # stdio JSON-RPC transport (same model as mcp-ayce-readonly).
    mcp.run()








