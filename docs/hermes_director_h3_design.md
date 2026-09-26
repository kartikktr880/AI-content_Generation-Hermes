# H3 — Hermes-as-Director Integration Design

> **DESIGN ONLY.** This document defines the future Hermes-as-Director
> boundary. Nothing in it is implemented. No write capability exists.
> No production run was triggered for this document. AYCE's production
> code, contracts, and dependencies are untouched.

## 1. H3 Status

- **H2.5 = PASS** (real agent round-trip, MCP-only, exact ground-truth match)
- **H3 = DESIGN ONLY** — this document
- **No write capability is approved or implemented yet.**
- **No production execution was performed for H3.** No `ayce run` was
  triggered during this design phase.
- The read-only `ayce-readonly` MCP server remains the ONLY AYCE surface
  exposed to Hermes, and it is unchanged.

## 2. Verified H2.5 Evidence (starting point)

| Fact | Value | Evidence |
|---|---|---|
| Hermes | NousResearch/hermes-agent **v0.21.3** (tag v2026.9.14, commit 345cd2b), MIT | install + `hermes --version` |
| Model/provider | `gemini-3.7-flash` via Google AI Studio (operator-configured credential; value never read) | `hermes status` |
| Target run | `run-20260919T110027Z-bc687f6516ac` (real-Piper Golden Path run) | session transcript |
| MCP calls | exactly 3, all successful: `list_runs` `{}` → 6 runs; `get_run_state` + `get_run_artifacts` with correct `run_id` argument | session `20260920_023033_7928fa` in Hermes `state.db` |
| Comprehension | 7 stages / 7 succeeded / 0 failed / 7 artifacts / correct kind↔stage mapping / correct job_id / final status succeeded — **exact match**, zero hallucinations | independent comparison against `RunState.load`/`ArtifactRegistry.load` data |
| MCP-only | PROVEN from transcript — only tool beside the three was `tool_describe` (built-in introspection); zero shell/file/browser tools | `state.db` `messages` table |
| AYCE regression | 246 passed / 1 skipped; health 10 pass / 0 warn / 0 fail; `src/ayce/`, `pyproject.toml` untouched | pytest, `ayce health` |
| No credentials exposed | Hermes's own masked display only (`AQ.A...7nfw`) | `hermes status` |

(Verified 2026-09-19/20; full transcripts in `docs/hermes_mcp_readonly.md`
and `mcp-ayce-readonly/agent-roundtrip-output.txt`.)

## 3. Current Architecture (verified from code, not docs)

Two boundaries exist today and they are **separate**:

```text
OBSERVATION BOUNDARY (H2/H2.5, verified):
  Hermes (MCP client)
    ↓ stdio JSON-RPC
  mcp-ayce-readonly/server.py        ← 3 read-only tools, runs-root-only surface
    ↓ REUSES (imports) — no duplication
  ayce.state.RunState.load / ayce.artifacts.ArtifactRegistry.load
    ↓ reads
  data/runs/<run_id>/{state.json, artifacts.json}

EXECUTION BOUNDARY (verified, unchanged):
  ayce run <script.json> [--assets-dir] [--narration-dir] [--json]   (CLI)
    ↓ run_pipeline()
  script_to_scene → asset_resolution → narration_audio → timeline
    → captions → production_render → media_qa
    ↓ writes
  RunState (state.json, per-stage checkpoints) · ArtifactRegistry (artifacts.json)
  + run artifacts (scene/asset/narration manifests, timeline, captions,
    MP4, SRT, qa_report)
```

**The two boundaries are currently separate.** Hermes can *inspect* AYCE
state; AYCE executes production only when a human runs `ayce run`;
**there is no way for Hermes to trigger production through MCP today.**
That separation is the deliberate H1–H2.5 outcome and the starting point
of this design.

Verified `ayce run` semantics (from `src/ayce/cli.py`, authoritative):

- args: `script` (path), `--assets-dir`, `--narration-dir`, `--json`
- validates the script path **before** creating any run state
  (invalid path → exit 2, no run dir); config error → exit 2
- creates a NEW run identity + run directory per invocation
  (`<data_dir>/runs/<run_id>`); per-stage idempotency remains
  authoritative within the run
- exit codes: `0` success (incl. QA-FAIL verdicts — evidence is the
  product), `1` stage execution failure, `2` invalid input/config
- `--json` report: `ok, run_id, job_id, run_dir, production_id, stages[],
  qa_verdict, failed_stage, error`
- environment-driven: `Config.from_env()` (`AYCE_*`) — narration provider
  selection (fixture vs Piper) is env-driven; the CLI passes no arbitrary
  environment through

## 4. Director Responsibility Boundary

Classification of every proposed responsibility:

| Responsibility | Class | Rationale |
|---|---|---|
| Inspect current runs | **ALLOWED NOW** | `list_runs` (verified H2.5) |
| Inspect one run's state | **ALLOWED NOW** | `get_run_state` (verified) |
| Inspect artifact metadata | **ALLOWED NOW** | `get_run_artifacts` (verified) |
| Determine whether a run appears successful/failed | **ALLOWED NOW** | derivable from state JSON (verified comprehension) |
| Decide a Golden Path run should be requested | **FUTURE - requires the controlled write boundary (§6-§7)** | the decision is Hermes's; the execution is AYCE's |
| Decide an identical production input should be re-run | **FUTURE** - semantically a new `trigger_golden_path` request with the same `script_id` and a fresh `request_id` | no new mechanism beyond the write boundary |
| Decide a failed partial run should be resumed mid-pipeline | **NOT ALLOWED (today)** | AYCE has no resume-from-checkpoint execution (`ayce run` always creates a new run); an AYCE-side contract change that is not designed or approved here |
| Decide which script is produced | **FUTURE, CONSTRAINED** - choose among allowlisted `script_id`s only; never a path | see §6 |
| Decide how the pipeline executes (stage order, providers, model, timing) | **NOT ALLOWED, ever** | AYCE sovereignty (§5) |
| Mutate RunState / ArtifactRegistry / artifacts directly | **NOT ALLOWED, ever** | AYCE sovereignty |
| Repair by patching artifacts or state | **NOT ALLOWED** | future repair must be an AYCE capability, designed separately |
| Publish, upload, or touch YouTube | **NOT ALLOWED** | out of scope; not designed |
| Execute arbitrary commands / read arbitrary files | **NOT ALLOWED, ever** | security boundary |

The Director's authority is deciding and requesting; AYCE's authority is
validating and executing. A decision without an approved command shape
has no effect.

## 5. AYCE Sovereignty Boundary

These remain exclusively inside AYCE. The director boundary must never
accept parameters that would move any of them under Hermes control:

- pipeline stage execution and stage ordering (fixed inside `run_pipeline`)
- RunState mutation (only AYCE stage code calls set_stage/save)
- ArtifactRegistry mutation (only AYCE stage code registers artifacts)
- artifact creation (manifests, captions, MP4, SRT, QA report)
- rendering (FFmpeg invocation details live in render.py)
- captions semantics (P6.5)
- QA semantics (P5.5 - including the QA-FAIL-is-evidence rule)
- failure semantics (first-failure stop, truthful per-stage state)
- retry semantics (per-stage idempotency inside a run; re-runs are new runs)
- filesystem layout (data/runs/<run_id>/...) and path resolution
- subprocess execution discipline (argv lists, never shell - Piper/FFmpeg)
- media tool invocation (FFmpeg, Piper) - incl. AYCE_PIPER_* config
- provider selection (fixture vs Piper) - env-driven, boundary-pinned

Anti-goal stated explicitly: Hermes must never become a second
production engine - no re-implementing stages, no orchestrating AYCE
internal functions directly, no composing pipeline steps from outside.
control to AYCE's existing entry point.

## 6. Proposed Director Command Contract (DESIGN - not implemented)

Chosen shape - derived from existing AYCE conventions (strict schemas
with unknown-field rejection, structured {ok, error} results, run-id
grammar), not invented:

```json
{
  "request_id": "req-20260920T120000Z-a1b2c3d4e5f6",
  "command": "trigger_golden_path",
  "script_id": "documentary",
  "reason": "channel needs the next production; last run succeeded"
}
```

- `request_id` - grammar `^req-[A-Za-z0-9._-]{1,64}$` (AYCE id convention
  with a distinguishing prefix). Generated by Hermes. The idempotency key.
- `command` - the ONLY approved value initially: `trigger_golden_path`.
  Unknown command -> structured rejection (`unknown_command`).
- `script_id` - NOT a path. An allowlist key resolved by the boundary
  against an operator-maintained mapping (scripts.json:
  script_id -> absolute script path). Default allowlist: only the
  verified fixture script. Unknown id -> `unknown_script`. Hermes can
  choose WHAT approved content to produce but can never make AYCE read
  an arbitrary file.
- `reason` - free text, audit-only, never executed.
- No other fields. Unknown/extra fields are rejected (`invalid_request`)
  - the same extra="forbid" discipline AYCE contracts use.

Validation pipeline (boundary-side, before anything executes):
1. schema check (exact fields, grammars) -> else invalid_request /
   invalid_request_id
2. command allowlist -> else unknown_command
3. script_id allowlist -> else unknown_script
4. idempotency ledger check -> duplicate returns the recorded result
   (duplicate_request with the original run_id)
5. single-flight check -> a request while another is in flight is
   rejected (busy)
6. ONLY THEN: spawn the FIXED execution command (§7). Hermes supplies
   no executable, no arguments, no environment, no shell.

Response contract (structured, matching the read-only server style):
- success: {"ok": true, "request_id", "run_id", "job_id",
            "status": "succeeded|failed", "qa_verdict": "PASS|FAIL|null"}
- rejection: {"ok": false, "error": {"code": "...", "message": "..."}}

Rejection/error codes: unknown_command, invalid_request,
invalid_request_id, unknown_script, duplicate_request, busy,
failed_pre_run, execution_failed, interrupted.

How the contract answers each required question:
- allowed commands: exactly one (trigger_golden_path); others rejected
- allowed arguments: exactly request_id/command/script_id/reason;
  script_id only via allowlist
- request validation: steps 1-5 above (schema, allowlists, ledger,
  single-flight)
- unknown command: rejected with unknown_command, nothing executed
- path traversal: impossible - Hermes supplies script_id, never a path;
  the path comes from the operator's allowlist mapping
- arbitrary shell execution: impossible - no shell anywhere; the child
  is one fixed executable with a fixed argv template
- arbitrary Python execution: impossible - no python -c, no code input
- command/argument injection: impossible - Hermes-controlled strings
  (reason, request_id echo) never enter the argv
- authorization: local stdio transport (only the local Hermes reaches
  the server) + operator-maintained allowlists; network exposure of the
  server is forbidden by design
- idempotency: request_id ledger (§11)
- request-to-run correlation: request_id -> run_id recorded in the
  ledger BEFORE execution starts; the resulting run is then observable
  through the existing read-only tools
- how Hermes receives the resulting RunState: by calling the EXISTING
  read-only tools (get_run_state / get_run_artifacts) after completion;
  the write response carries only correlation identifiers
- failure before a run starts: CLI exit 2 (no run dir created - the CLI
  validates first) -> ledger records failed_pre_run with the CLI error
- AYCE starts but fails: CLI exit 1; the run dir persists truthful
  per-stage checkpoints -> ledger records failed WITH run_id so Hermes
  can inspect exactly where it stopped
- Hermes times out / disconnects while AYCE continues: the stdio
  transport tears the server (and child) down -> the run directory keeps
  the truthful per-stage checkpoint of whatever completed; ledger marks
  interrupted. Detached-execution mode is deliberately DEFERRED - it
  introduces orphan-run management for no current need.
- duplicate execution prevention: request_id idempotency + single
  in-flight request (§11)

## 7. Candidate Single Write Tool

Candidate: **trigger_golden_path** - "Request AYCE to execute one
approved Golden Path production run."

Evaluation against the design criteria:

| Criterion | Assessment | Evidence |
|---|---|---|
| Sufficiently narrow | YES - one command, one allowlisted script input, no parameters that reach pipeline internals | §6: nothing Hermes supplies enters AYCE logic |
| Meaningful | YES - converts the verified observation loop into a closed production loop (decide -> produce -> verify) | H2.5 proved observation; this adds exactly one action |
| Auditable | YES - request ledger + AYCE's own run dir/state/artifacts give a complete record with no new logging infrastructure | §12 |
| Safe enough to prototype later | YES - worst case is a wasted production run from the allowlisted script (the same thing a human does today); no data loss, no external effect, QA gates the output | §9 |
| Compatible with existing `ayce run` | YES - the boundary invokes the existing CLI entry point as a subprocess with fixed argv; ZERO pipeline changes | §3 verified semantics |
| Preserves AYCE sovereignty | YES - AYCE validates nothing from Hermes beyond script_id; execution, state, artifacts remain internal | §5 |

Verdict: trigger_golden_path is the right single write capability - the
minimal action that makes the director real; every rejection path keeps
AYCE sovereign.

retry_run evaluation: NOT NOW.
- Retrying a succeeded run = a new trigger_golden_path request (same
  script_id, fresh request_id) - no second tool needed.
- Retrying a FAILED PARTIAL run (resume from the failed stage) does not
  exist in AYCE (ayce run always starts a new run) and would require
  new AYCE-side execution semantics (resume-from-checkpoint) - an
  unapproved contract change. If designed someday, retry_run becomes a
  new AYCE capability first, and only then a director command.
- Conclusion: ONE write tool now; retry_run deferred.

Placement: the write boundary should be a SEPARATE minimal server
(planned mcp-ayce-director/), NOT an addition to mcp-ayce-readonly/ -
the read-only server stays clean and independently removable, and the
write boundary can be withdrawn without touching observation.

## 8. Golden Path Control Flow (future)

```
Hermes observes state          (list_runs / get_run_state / get_run_artifacts)
  |  Hermes DECIDES a production action is needed     [authority: Hermes]
Hermes emits ONE approved director command
  (trigger_golden_path, with request_id + script_id)
  |                                                   [HAND-OFF POINT]
mcp-ayce-director boundary VALIDATES
  (schema, allowlists, ledger, single-flight)
  |  accepted                                        [authority: boundary]
boundary spawns FIXED argv:
  <pinned python> -m ayce run <allowlisted-script> --json
  |  AYCE OWNS EXECUTION from here                   [authority: AYCE]
AYCE runs the 7 verified stages, writes RunState checkpoints after each
  stage, registers artifacts, produces MP4/SRT/qa_report
  |
boundary captures exit code + --json report, records the ledger result
  |  Hermes returns to OBSERVER role (it never owned execution)
Hermes calls get_run_state / get_run_artifacts (read-only MCP)
  |
Hermes verifies the outcome against its expectation
  |
Hermes decides the next action (inspect more, request another run, stop)
```

Authority changes hands exactly twice: Hermes -> boundary at command
emission (a REQUEST, not control), and boundary -> AYCE at spawn (full
execution authority). Hermes never holds execution authority; AYCE
never holds decision authority.

## 9. Failure / Recovery Model (design only)

| Case | Designed behavior |
|---|---|
| Invalid command | unknown_command rejection; nothing executed; ledger records the rejected request |
| Invalid input (schema/grammar) | invalid_request / invalid_request_id; nothing executed |
| Unknown script_id | unknown_script; nothing executed |
| Duplicate request | duplicate_request + the ORIGINAL run_id/result (idempotent) |
| Request while a run is in flight | busy rejection; the director may observe the in-flight run via read-only tools |
| Unknown run (read side) | existing unknown_run structured error (verified H2) |
| AYCE startup failure (exit 2: bad config/script) | failed_pre_run; NO run dir (the CLI validates first); ledger records the CLI error |
| Pipeline stage failure (exit 1) | execution_failed WITH run_id; the run dir holds truthful per-stage state (completed stages succeeded, failing stage failed, later stages absent) - exactly AYCE's verified first-failure semantics |
| QA failure (verdict FAIL) | NOT a pipeline failure - the run SUCCEEDS with qa_verdict FAIL in state/artifacts (verified P5.5 semantics); the ledger records qa_verdict so the director can decide (e.g., request a new run) |
| Timeout | the MCP tool timeout (Hermes-configurable, default 120 s) may hit -> ledger interrupted; the run dir keeps truthful partial checkpoints. Mitigation: tune the tool timeout; long runs need the deferred detached mode |
| Hermes session termination | the stdio transport tears down server + child -> same as timeout: truthful partial run dir, ledger interrupted; no orphan-process management in v1 |
| AYCE continues after Hermes disconnects | NOT TRUE in v1 synchronous mode (documented honestly); the deferred detached mode addresses it |
| Partial artifact generation | intrinsic to AYCE's per-stage persistence; read-only tools expose exactly what exists (get_run_state shows the failed stage; get_run_artifacts shows registered artifacts) |
| Stale/corrupt state | AYCE's reload-verify conventions already reject corrupt state (StateError -> structured corrupt_state error, verified H2) |
| Concurrent execution requests | single-flight: a second concurrent request is rejected busy; AYCE runs are isolated directories, so even an accidental overlap cannot corrupt another run |

No recovery logic is implemented in H3; the director's only recovery
action is requesting a fresh run (which AYCE's own isolation and
idempotency make safe).

## 10. Security Boundary (preserving the H2 model)

The future write boundary must NOT permit (and its design makes
impossible, not merely forbidden):

- arbitrary shell -> no shell anywhere; one fixed executable + one
  fixed argv template
- arbitrary executable path -> the child is the boundary's OWN pinned
  interpreter invoking the AYCE CLI module; Hermes names no executable
- arbitrary Python -> no code input of any kind (no python -c, no eval)
- arbitrary filesystem writes -> AYCE writes only inside its own
  data/runs/<run_id> (verified convention); the boundary writes only
  its ledger
- arbitrary environment manipulation -> the boundary constructs the
  child env from AYCE defaults (optionally pinning AYCE_PIPER_* from
  its own operator config); Hermes contributes NO environment
- arbitrary subprocess execution -> one argv template, one input source
  (the allowlisted script path resolved from script_id)
- arbitrary network calls -> the Golden Path makes none (fixture
  providers; Piper is local); the server is stdio-transport only
- arbitrary MCP registration -> servers are operator-configured in
  Hermes's config.yaml; the boundary cannot register anything
- arbitrary AYCE internal method invocation -> the boundary calls ONLY
  the public `ayce run` CLI entry point (a subprocess), never imports
  pipeline internals for execution

The command vocabulary is an explicit allowlist (ONE entry initially).
Everything not allowlisted is rejected. This mirrors the H1-verified
hardline-deny approval model: dangerous shapes are blocked regardless
of mode.

## 11. Idempotency / Concurrency

Simplest mechanism compatible with AYCE (no Redis, no queues, no
database, no distributed infrastructure):

- Request ledger: a single JSON file (requests.json) in the boundary's
  own directory, written atomically (tmp + replace - the AYCE
  convention). Record: request_id, status (accepted/rejected/
  succeeded/failed/interrupted/duplicate), script_id, run_id, job_id,
  timestamps, exit_code, error, qa_verdict.
- Idempotency: the same request_id returns the recorded result without
  executing (duplicate_request + original run_id if one exists). A
  DISTINCT request_id is always a new run - AYCE runs are naturally
  isolated directories, so re-execution is safe by design.
- Single-flight: at most one request in running state; a request
  arriving during an active run is rejected busy (the director retries
  with a new request_id or observes the active run). This matches
  AYCE's single-machine, single-operator reality.
- Deterministic correlation: run_id/job_id come from AYCE's
  new_run_id/new_job_id and are captured from the --json report into
  the ledger - the read-only tools then close the loop.
- No lock daemon: single-flight is a ledger state check under the
  boundary's single-process execution model.

## 12. Observability / Audit

A future controlled request leaves this evidence chain (no new logging
infrastructure - AYCE conventions are reused):

1. the REQUEST itself (ledger: request_id, command, script_id, reason,
   requested_at, requested_by=hermes)
2. the VALIDATION result (accepted / rejected + code)
3. the EXECUTION record (ledger: started_at, finished_at, exit_code)
4. the correlation (run_id, job_id in the ledger)
5. AYCE's own truthful record: state.json (per-stage lifecycle),
   artifacts.json (artifact identities), and all run artifacts
6. the FAILURE reason when applicable (ledger error + the run's
   last_error fields, visible via get_run_state)
7. the initiating Hermes session when available (session id from
   Hermes's own store - optional, best-effort)
8. qa_verdict for runs that reached QA

The ledger is the director-side audit trail; the run directory is the
production-side audit trail. Nothing else is needed.

## 13. Alternatives Considered

| Alternative | Sovereignty | Security | Auditability | Simplicity | Reuses `ayce run` | Replaceability | Attack surface | Fit | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| A. Hermes executes `ayce run` via shell | LOW - Hermes composes the command line; internals exposed to prompt manipulation | LOW - arbitrary-command risk, quoting hazards, no validation layer | LOW - no request/ledger; evidence scattered in shell history | High short-term | Yes, uncontrolled | Poor (shell coupling) | LARGE - the classic injection surface | Poor - makes Hermes the executor | REJECTED |
| B. Hermes invokes a narrow MCP write tool (trigger_golden_path) | HIGH - AYCE validates and executes; Hermes supplies only an allowlisted id | HIGH - fixed argv, allowlists, ledger, no shell | HIGH - ledger + run dir | Medium - one small server + validation | Yes - the exact CLI entry point | HIGH - one thin file, replaceable without touching AYCE | Minimal (one command, no free-form input) | Exact fit for "Hermes coordinates, AYCE executes" | SELECTED (design) |
| C. Generic command-execution MCP | NONE - AYCE becomes one of many shell targets | LOW - arbitrary execution by design | LOW - generic logs only | Medium | Incidentally | LOW - generic tools accrete | LARGE | Poor - contradicts the allowlist principle and the H1 hardline-deny evidence | REJECTED |
| D. New orchestration service (queue/daemon) | HIGH in theory | MEDIUM - new network surface + auth | MEDIUM-HIGH | LOW - new moving parts and failure modes | Indirect | MEDIUM | MEDIUM (service exposure) | Poor TODAY - premature infrastructure for a single-machine, single-operator pipeline | REJECTED for now |

Evidence-based conclusion: **B**. It is the only alternative that
preserves AYCE sovereignty, keeps the attack surface at one allowlisted
command, reuses the verified `ayce run` path unchanged, and matches the
verified Hermes MCP capability from H2.5. A and C hand execution
authority to the agent; D builds infrastructure the project does not
need yet (revisit only if multi-machine or asynchronous production is
ever required).

## 14. H3 Decision Gate

H3 DESIGN STATUS: **DESIGNED** (with the deferred items listed below).

Criteria required before ANY write capability is implemented:

1. this contract has been reviewed and approved by the operator
2. the security boundary (§10) has been reviewed - especially the
   script_id allowlist mechanism and the no-shell/no-env rules
3. idempotency (§11) has been reviewed - request_id semantics,
   duplicate behavior, single-flight
4. failure semantics (§9) have been reviewed - especially the honest
   v1 limitation that Hermes disconnect tears down the run
5. Golden Path preservation verified: the boundary invokes the existing
   `ayce run` CLI unchanged; pipeline code untouched
6. no arbitrary execution: static checks confirm the boundary source
   has no shell/eval/subprocess-beyond-fixed-argv surface
7. no unnecessary new infrastructure: no queues/daemons/databases
8. rollback/rejection behavior defined: the write boundary is a
   separate, independently removable component; rejection responses
   are structured; the ledger records every request including rejects

Deferred items (explicitly NOT part of the first write boundary):
- detached execution surviving Hermes disconnect
- resume-from-checkpoint retry (requires an AYCE-side capability first)
- multiple script allowlist entries beyond operator-configured ones
- any multi-run scheduling or autonomous production loops

## 15. Next Experiment (candidate H4 - only if H3 is approved)

H4: **Prototype exactly ONE controlled `trigger_golden_path` boundary in
an isolated experiment** (separate `mcp-ayce-director/` directory, own
venv, not part of src/ayce/):

- one MCP tool (`trigger_golden_path`) implementing §6 exactly
- strict allowlisting: one script_id -> the verified documentary fixture
- request ledger + single-flight + structured rejections per §9/§11
- subprocess boundary: pinned interpreter, `python -m ayce run
  <allowlisted-script> --json`, explicit argv list, no shell
- tests: schema rejections, unknown command/script, duplicate_request,
  busy, traversal-by-construction, no-mutation of AYCE sources, and ONE
  real execution proving request_id -> run_id -> read-only-observation
  correlation (the Golden Path run itself is AYCE's own verified code)
- acceptance: every rejection path returns structured errors; the one
  happy-path request produces a run whose state/artifacts match
  ground truth via the existing read-only tools; AYCE tests remain
  green; no src/ayce/ changes
- H4 does NOT grant Hermes standing write access: the director server
  is registered with Hermes only for the experiment and removed (or
  left disabled) afterward, pending operator review
