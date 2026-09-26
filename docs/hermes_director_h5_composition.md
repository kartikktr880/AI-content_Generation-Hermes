# H5 — Hermes Director Composition & Golden Path Integration (Evidence Record)

> **STATUS: PASS.** One real, controlled Hermes→Director→AYCE Golden Path
> composition executed over the real MCP stdio transports, verified
> independently through the existing read-only server. No new orchestration
> infrastructure; no `src/ayce/` change; no engine rebuild.

## Starting state

- Commit: `a84c355` (master) — "feat: add golden path pipeline runner"
- Working tree: pre-existing H4-era uncommitted changes (captions/tts_piper,
  pipeline/config edits) — unchanged by H5.
- Hermes v0.21.3 (`%LOCALAPPDATA%\hermes\`); config already registered BOTH
  `ayce-readonly` (3 tools) and `ayce-director` (1 tool) — the composition
  mechanism already existed; H5 exercised it end-to-end for the first time.
- Prior H5a discovery attempts (12:20–12:52 sessions) all stalled on
  `tool_describe` "too many names: 12 > max 10"; none ever reached an
  invocation. No H5 run existed before this experiment.

## Changed files (H5 only)

- `mcp-ayce-director/h5_compose_prompt.txt` — the one-shot composition prompt
  (deterministic `request_id`, MCP-only constraints).
- `mcp-ayce-director/run_h5_composition.ps1` — runner (modeled on the H2
  probe; optional `-m <model>` override).
- `mcp-ayce-director/dump_session_h5.py` — read-only transcript evidence
  dumper (allows the director tool in the AYCE allowlist).
- `docs/hermes_director_h5_composition.md` — this record.
- `mcp-ayce-director/requests.json` + `data/runs/<new run>` — produced BY the
  run itself (evidence, not code).
- `src/ayce/`, `mcp-ayce-readonly/`, `pyproject.toml`: untouched by H5.

## Composition path (actual)

```text
Hermes one-shot session (gemini-3.5-flash-lite; default model was quota-blocked)
  → MCP stdio: ayce-director  → trigger_golden_path (the ONE write tool)
      → fixed argv: <repo venv python> -m ayce run <allowlisted documentary fixture> --json
  → AYCE Golden Path: script_to_scene → asset_resolution → narration_audio
      → timeline → captions → production_render → media_qa  (existing engines)
  → Hermes MCP stdio: ayce-readonly → get_run_state + get_run_artifacts
  → QA evidence: qa_report verdict PASS (16 checks / 0 failed)
```

## Execution evidence

- request_id: `req-20260920-h5-golden-compose-01` (deterministic; ledger
  `requested_by: "hermes"`, status `succeeded`, exit_code 0, wall ≈ 8.9 s)
- run_id: `run-20260920T075426Z-2c45d535f42d`; job_id:
  `job-20260920T075426Z-a5e5f9b4b44d`
- Hermes tool result: ok=true, 7/7 stages ok, `qa_verdict: "PASS"`
- Independent read-only verification (`verify_via_readonly.py`): 7/7 stages
  succeeded (attempts 1, no errors); 7 artifacts; qa_report `verdict: PASS`,
  `total_checks: 16, failed_checks: 0`, `render_sha256` equals the
  rendered_video artifact sha256
  (`516955bd9a964f2a556bbddd578196679f1a688f981f62145050e2f316013241`)
- Session transcript (read-only dump, session `20260920_132410_9f3733`):
  only `tool_describe` (builtin introspection), `tool_call` (builtin
  dispatcher), `mcp__ayce_director__trigger_golden_path`,
  `mcp__ayce_readonly__get_run_state`, `mcp__ayce_readonly__get_run_artifacts`.
  ZERO shell/terminal/file/browser tools. Agent recovered from the
  `tool_describe` 12>10 limit by splitting describe calls, correctly
  classified trigger_golden_path as the ONLY write tool, and MCP results were
  wrapped in Hermes' `<untrusted_tool_result>` injection defense.

## Tests

| Suite | Result (pre-run) | Result (post-run) |
|---|---|---|
| `mcp-ayce-director/test_server.py` | 74 passed | 74 passed |
| `mcp-ayce-readonly/test_server.py` | 21 passed, 1 skipped | 21 passed, 1 skipped |
| AYCE full regression (repo venv) | 247 tests: 0 fail, 0 error, 1 skipped | 247 tests: 0 fail, 0 error, 1 skipped |
| `hermes mcp test ayce-readonly` | Connected, 3 tools | — |
| `hermes mcp test ayce-director` | Connected, 1 tool | — |

## Acceptance criteria (A–M)

| # | Criterion | Verdict |
|---|---|---|
| A | Repo state inspected before changes | PASS |
| B | Existing Hermes Director MCP reused (no new boundary) | PASS |
| C | Existing AYCE Golden Path reused (no engine rebuild) | PASS |
| D | Hermes accessed read capability (get_run_state / get_run_artifacts / list_runs) | PASS |
| E | Hermes identified/selected the Director capability (correct classification + routing) | PASS |
| F | Hermes invoked it through the established boundary (real stdio transport) | PASS |
| G | Invocation reached existing AYCE execution path (fixed argv, ledger, run dir) | PASS |
| H | Controlled Golden Path run executed (fixtures only, no publishing) → PASS QA | PASS |
| I | Resulting state/output observed afterward (read-only tools + qa_report) | PASS |
| J | H4 write-boundary intact (74 director tests green; exact-field contract; single-flight; fixed argv in ledger) | PASS |
| K | Existing tests passing (exact baselines) | PASS |
| L | No new orchestration infrastructure (only prompt/runner/dumper/docs) | PASS |
| M | Evidence recorded (this file + ledger + transcript) | PASS |

## Known limitations

- Default model `gemini-3.7-flash` free-tier quota was exhausted (429) from
  earlier stalled sessions; the composition ran on `gemini-3.5-flash-lite`
  (separate quota bucket) via the runner's `-m` flag. No config default was
  changed.
- FastMCP auto-exposes read-only `get_prompt/list_prompts/list_resources/
  read_resource` helpers on both servers (Hermes classified them correctly);
  they are noise, not new capabilities.
- Single director run so far; idempotency/single-flight remain covered by the
  H4 test suite, not re-proven live in H5.

## Next step (smallest)

H5-B: one idempotency demonstration through Hermes — re-issue the SAME
`request_id` in a new session and confirm `duplicate_request` returns the
original result with NO second run — before any multi-objective expansion.
