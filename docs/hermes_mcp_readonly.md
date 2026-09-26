# H2 Experiment — Read-Only AYCE MCP Server + Hermes Round-Trip

> **EXPERIMENT RECORD ONLY — not production integration.** This documents
> a standalone, read-only MCP server that lets the isolated Hermes Agent
> *inspect* AYCE production runs. Hermes is NOT integrated into AYCE; no
> AYCE source, contract, or dependency was touched.

## Purpose

Prove the smallest safe communication path for the future architecture:

```text
Hermes (future Master Production Director)
    ↓  MCP client (stdio)
ayce-readonly MCP server (standalone, mcp-ayce-readonly/)
    ↓  REUSES ayce.state.RunState.load / ayce.artifacts.ArtifactRegistry.load
AYCE persisted state (data/runs/<run_id>/state.json, artifacts.json)
```

## Location / runtime / isolation

- Directory: `mcp-ayce-readonly/` — **outside `src/ayce/`**, not in
  `pyproject.toml`, independently removable (delete the directory).
- Runtime: its own venv (Python 3.11.16 via the Hermes-managed uv) with
  `mcp<2` (SDK v1.30.0 — SDK 2.x renamed FastMCP→MCPServer with breaking
  changes; migration is a future revisit item) + pytest.
- AYCE readers are imported from the repo's `src/` via `sys.path` — no
  AYCE code is duplicated and none is modified.

## Tools exposed (exactly three; read-only)

| Tool | Input | Output |
|---|---|---|
| `list_runs` | none | `{ok, runs: [{run_id, job_id, stage_count, stages_succeeded, stages_failed, updated_at}], truncated}` — decoy dirs skipped; corrupt state listed truthfully |
| `get_run_state` | `run_id` | `{ok, run_id, data: {run_id, job_id, created_at, updated_at, stages: {stage: {status, attempts, updated_at, last_error}}}}` |
| `get_run_artifacts` | `run_id` | `{ok, run_id, artifact_count, artifacts: [{artifact_id, stage, kind, path, created_at, metadata}]}` |

Failures (unknown run, corrupt state/manifest, bad id) return
`{ok: false, error: {code, message}}` — never exceptions/stack traces.
Error codes: `invalid_run_id`, `path_escape`, `unknown_run`,
`missing_state`, `corrupt_state`, `missing_manifest`, `corrupt_manifest`.

## Security boundary

- **READ-ONLY**: no shell, no subprocess, no file writes/deletes, no env
  inspection, no credentials, no pipeline/FFmpeg/Piper invocation, no
  RunState/ArtifactRegistry mutation (a guarded-source test asserts the
  server source contains no dangerous capabilities).
- Filesystem surface: ONLY the configured runs root (`AYCE_RUNS_ROOT`);
  only `state.json` / `artifacts.json` are opened.
- Path safety: run-id grammar regex (rejects absolute paths/`..` by
  construction) + resolved-inside-root check (symlink-escape guard).
- Hermes registration passes NO write permission — none exists to grant.

## Install / run / register

```text
# isolated venv (one-time)
%LOCALAPPDATA%\hermes\bin\uv.exe venv --python 3.11 mcp-ayce-readonly\.venv
%LOCALAPPDATA%\hermes\bin\uv.exe pip install --python mcp-ayce-readonly\.venv\Scripts\python.exe "mcp<2" pytest

# tests (real MCP stdio transport; no Hermes/LLM needed)
set PYTHONPATH=<repo>\src
mcp-ayce-readonly\.venv\Scripts\python.exe -m pytest mcp-ayce-readonly/test_server.py

# Hermes registration (config.yaml mcp_servers entry; CLI `mcp add` needs
# interactive confirmation):
#   mcp_servers:
#     ayce-readonly:
#       command: ...\mcp-ayce-readonly\.venv\Scripts\python.exe
#       args: [...\mcp-ayce-readonly\server.py]
#       env:
#         AYCE_RUNS_ROOT: <repo>\data\runs

hermes mcp list    # → ayce-readonly  enabled
hermes mcp test ayce-readonly   # → Connected, Tools discovered: 3
```

## Actual verification results (2026-09-19)

- VERIFIED: 22 protocol tests (server start, exact 3-tool surface, JSON
  schemas, truthful state/artifact data from real AYCE structures,
  structured errors, 9 traversal/absolute-path rejections, no-mutation
  snapshot of runs root + AYCE sources, deterministic responses, guarded
  server source).
- VERIFIED: Hermes v0.21.3 `mcp list` → `ayce-readonly enabled`;
  `mcp test` → Connected 3.7 s, **3 tools discovered, no extras**.
- BLOCKED: real agent round-trip (`hermes -z "..."`) — **no LLM provider
  credential is configured on this machine** (all provider keys unset;
  model `anthropic/claude-opus-4.6` selected but unusable). NOT faked.
- UNKNOWN: agent-side tool *selection* quality and JSON comprehension —
  requires the blocked round-trip.

## H2.5 — Credential-Gated Agent Round-Trip (2026-09-19/20)

**Result: PASS — the real Hermes agent round-trip succeeded end-to-end,
MCP-only, with exact ground-truth comprehension.**

### Credential
- Provider: **Google / Gemini** (`hermes status` → ✓; model
  `gemini-3.7-flash`, provider Google AI Studio). Configured by the
  operator; the value was never read or reported — only Hermes's own
  masked status display (`AQ.A...7nfw`) was observed.

### Execution
- First attempt WITH `--safe-mode`: the agent ran but reported the
  `ayce-readonly` tools "not available in the current session" and
  **refused to bypass via shell/files** (correct safe behavior).
  OBSERVED finding: `--safe-mode` one-shot sessions do not load MCP
  server toolsets; agent.log confirmed no MCP server spawn.
- Second attempt WITHOUT `--safe-mode` (prompt-level MCP-only
  constraint): **SUCCESS** in ~50 s (model `gemini-3.7-flash`).
- Session transcript (`state.db`, read-only dump via
  `mcp-ayce-readonly/dump_session.py`, session `20260920_023033_7928fa`):

```text
[9]  user        — the MCP-only prompt (no answer key included)
[10] assistant   tool_describe(names=[mcp__ayce_readonly__list_runs,
                 mcp__ayce_readonly__get_run_state,
                 mcp__ayce_readonly__get_run_artifacts])   ← schema check
[11] tool        tool_describe → the 3 JSON schemas
[12] assistant   tool_call → mcp__ayce_readonly__list_runs {}
[13] tool        ← {"ok": true, "runs": [...6 runs...]}        (real AYCE data)
[14] assistant   tool_call → mcp__ayce_readonly__get_run_state
                 {"run_id": "run-20260919T110027Z-bc687f6516ac"}  ← correct arg
[15] tool        ← {"ok": true, "run_id": "run-20260919T110027Z...} (real data)
[16] assistant   tool_call → mcp__ayce_readonly__get_run_artifacts
                 {"run_id": "run-20260919T110027Z-bc687f6516ac"}  ← correct arg
[17] tool        ← {"ok": true, ... 7 artifacts}               (real data)
[18] assistant   final summary (matches ground truth exactly)
NON-AYCE tools used in this session: only `tool_describe` (a built-in
introspection helper). ZERO shell/terminal/file/other bypass tools.
```

- Hermes also wraps MCP results in `<untrusted_tool_result>` blocks
  (prompt-injection defense) — OBSERVED.

### Comprehension vs ground truth (independent verification)

| Item | Ground truth | Hermes answer | Match |
|---|---|---|---|
| run_id | run-20260919T110027Z-bc687f6516ac | same | ✓ |
| job_id | job-20260919T110027Z-896d98d95bc6 | same | ✓ |
| stage count | 7 | 7 | ✓ |
| succeeded/failed | 7 / 0 | 7 / 0 | ✓ |
| artifact count | 7 | 7 | ✓ |
| artifact kinds+stages | scene_manifest, asset_manifest, audio, timeline, captions, rendered_video (production_render), qa_report (media_qa) | identical list | ✓ |
| final status | all stages succeeded | succeeded | ✓ |

Errors: none. Hallucinations: none. Omissions: none.

### Security
- MCP-only proven from the session transcript (the only non-AYCE tool
  was `tool_describe`; no terminal/file/browser tools were invoked).
- Read-only boundary intact: the three tools are the only AYCE surface.
- `--safe-mode` finding recorded: it suppresses MCP toolsets in one-shot
  sessions — use prompt-level MCP-only constraints + post-session
  transcript verification for read-only experiments.

### AYCE non-regression (post-round-trip)
246 passed / 1 skipped; health 10/0/0; `src/ayce/`, `pyproject.toml`,
pipeline, RunState, ArtifactRegistry untouched.

## Known limitations

## Known limitations

- The server reads only persisted run state/artifacts — not live media,
  logs, or run output text.
- `list_runs` caps at 100 runs (truncated flag).
- Hermes config change is a user-scope file edit in the Hermes install
  (kept minimal; removal = delete the `mcp_servers: ayce-readonly:` block).
- SDK v1 pinned; MCP SDK 2.x migration is deferred until the agent
  round-trip phase requires it.
- The symlink-escape test skips on Windows without symlink privilege;
  the escape guard itself is enforced unconditionally by the
  resolved-inside-root check.
- `mcp-ayce-readonly/ground_truth_probe.py` and the `live`-style probe
  scripts are experiment tooling, not production code.

