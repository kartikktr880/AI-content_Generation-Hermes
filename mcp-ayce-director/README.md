# AYCE Director MCP Server (H4 controlled experiment)

A standalone, write-capable MCP boundary implementing the H3-approved
director design (`docs/hermes_director_h3_design.md`) with **exactly ONE**
write tool:

    trigger_golden_path — request ONE approved Golden Path production run

Stage 2 added ONE read-style research capability on the same server (same
fixed-argv, no-shell, server-controlled execution model; separate
`research_requests.json` ledger, idempotent by payload digest):

    execute_research_slice — ONE bounded local research slice (yt-dlp
    ingestion + hook extraction + outlier/velocity analytics + local
    clustering) returning a compact, validated Research Artifact.

Stage 2.5 added ONE brief-conversion capability on the same server (reuses
the research subprocess seam; separate `brief_requests.json` ledger,
idempotent by research artifact):

    propose_script_brief — convert ONE persisted research artifact (by
    research_id, NEVER a path) into a validated deterministic ScriptInput
    evidence brief via `python -m ayce.research --brief --json` (no LLM,
    no network). The generated ScriptInput is DATA conforming to the P1-B
    contract: it is persisted byte-exact under `generated_scripts/` and
    registered into `scripts.json` (namespaced `brief-*`, never overwriting
    operator entries, conflict-safe) so the UNCHANGED `trigger_golden_path`
    boundary can execute it. A failed brief NEVER falls back to a fixture.

This component is **independently removable** (delete this directory; remove
any temporary Hermes `mcp_servers` registration). AYCE (`src/ayce/`) has NO
dependency on it and was not modified.


## The one tool

```json
{
  "request_id": "req-20260920T000000Z-example000001",
  "command": "trigger_golden_path",
  "script_id": "documentary",
  "reason": "free text, audit-only"
}
```

- `request_id` — grammar `^req-[A-Za-z0-9._-]{1,64}$`; the idempotency key.
- `command` — must be exactly `trigger_golden_path` (unknown → `unknown_command`).
- `script_id` — an **allowlist key** (`scripts.json`), NEVER a path. Paths,
  `..`, separators and shell metacharacters are rejected by grammar before
  the allowlist lookup (`unknown_script`).
- `reason` — free text (≤2000 chars), audit-only, never executed.
- Any extra/missing field → `invalid_request`. The caller can NEVER supply
  an executable, argv, shell string, environment, cwd, or output path.

## Execution (fixed argv, no shell)

```
<pinned python> -m ayce run <allowlisted-script> --assets-dir ... --narration-dir ... --json
```

- interpreter: the AYCE project venv python (operator-overridable via
  `AYCE_DIRECTOR_PYTHON`), server-controlled
- cwd: repository root; env: server-controlled, `PYTHONPATH` pinned to `src`
- the child gets **clean stdio** (`stdin=DEVNULL`, `close_fds=True`) — a
  verified H4 fix: children of stdio MCP servers otherwise inherit
  OVERLAPPED-mode transport handles and hang inside
  `Py_InitializeFromConfig`
- bounded EXPERIMENTAL timeout (`AYCE_DIRECTOR_TIMEOUT_S`, default 600 s):
  on expiry the child is KILLED and the result is a truthful `timeout`
  (never a claimed success); the run dir keeps AYCE's truthful partial state

## Ledger / idempotency / single-flight (H3 §11)

- `requests.json` in this directory (`AYCE_DIRECTOR_LEDGER` overrides),
  written atomically (tmp + `os.replace`); every request is recorded,
  including rejects
- same `request_id` + identical payload → `duplicate_request` + the original
  result; **no second run**
- same `request_id` + conflicting payload → rejected, nothing executed
- a second request while one is `running` → `busy` (single-flight; ledger
  state check, no daemon/queue/database)

## Result contract

- success: `{ok, request_id, command, script_id, status:"succeeded",
  run_id, job_id, qa_verdict, exit_code, failed_stage, error, stages}`
- rejection/failure: `{ok:false, error:{code,message}}` with codes
  `invalid_request, invalid_request_id, unknown_command, unknown_script,
  duplicate_request, busy, failed_pre_run, execution_failed, timeout`
- AYCE semantics preserved: QA-FAIL is a SUCCEEDED run (evidence), exit 1 is
  `execution_failed` WITH run_id, exit 2 is `failed_pre_run`

## Isolation / runtime

- own venv (Python 3.11 via uv, `mcp<2` + pytest) — same model as
  `mcp-ayce-readonly`; AYCE has no dependency on this component
- the read-only server (`mcp-ayce-readonly/`) is untouched and remains the
  ONLY observation surface; verification of runs goes through it
  (`verify_via_readonly.py`)

## Tests (no production runs in the suite)

```
set PYTHONPATH=<repo>\src   (not required; server.py needs no AYCE imports)
mcp-ayce-director\.venv\Scripts\python -m pytest mcp-ayce-director\test_server.py
```

74 tests: contract rejections, allowlist, traversal/injection, idempotency,
single-flight (incl. concurrent), AYCE result semantics (stage failure,
QA-FAIL, exit 2, timeout), correlation, ledger integrity, no-mutation of
`mcp-ayce-readonly`/`src/ayce`, real stdio-transport round-trips (with an
env-gated stub runner; disabled by default).

## One controlled real execution

`controlled_execution.py` performs ONE real `trigger_golden_path` over the
real MCP stdio transport (no stub). The result is verified independently
through the EXISTING read-only server:

```
mcp-ayce-readonly\.venv\Scripts\python mcp-ayce-director\verify_via_readonly.py <run_id>
```

Evidence and the experiment record: `docs/hermes_director_h4_experiment.md`.

## Rollback / disable

Delete this directory. The director is not registered anywhere by default
(no Hermes config change was made for H4); if an operator temporarily
registered it, remove that `mcp_servers:` block.
