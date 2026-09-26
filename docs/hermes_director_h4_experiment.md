# H4 — Controlled Hermes Director Write-Boundary Experiment Record

> **EXPERIMENT RECORD.** H3 was reviewed and accepted; H4 implemented exactly
> ONE director capability (`trigger_golden_path`) in an isolated
> `mcp-ayce-director/` component. AYCE production source is untouched. No
> commit was made. The director is NOT registered with Hermes (no persistent
> configuration change).

## 1. What was built

- `mcp-ayce-director/server.py` — standalone MCP stdio server, exactly one
  tool (`trigger_golden_path`), implementing H3 §6/§7/§9/§10/§11:
  strict field-exact contract, command allowlist, `script_id` allowlist
  (`scripts.json`, one entry: `documentary` → the verified documentary
  fixture + fixture asset/narration dirs), request ledger (`requests.json`,
  atomic tmp+`os.replace`), idempotency, single-flight, fixed-argv
  subprocess (`<pinned python> -m ayce run <allowlisted-script> … --json`,
  no shell), bounded experimental timeout (default 600 s) with child kill
  and truthful `timeout` result.
- `mcp-ayce-director/test_server.py` — 74 focused tests (see §4).
- `mcp-ayce-director/controlled_execution.py`, `verify_via_readonly.py` —
  the controlled real-execution probe and the read-only verification probe.
- `mcp-ayce-director/scripts.json`, `requirements.txt`, `README.md`,
  own venv (Python 3.11.16 via uv, `mcp<2` SDK 1.30.0 + pytest).

NOT built (per H4 scope): retry/resume, arbitrary/shell/generic execution,
pipeline/RunState/ArtifactRegistry mutation, queue/database/daemon,
detached execution, second artifact registry.

## 2. Pre-flight findings (code is authoritative)

- Verified `ayce run` semantics from `src/ayce/cli.py`: exit 0 success
  (incl. QA-FAIL), 1 stage failure, 2 pre-run input/config failure;
  `--json` report fields preserved verbatim in director results.
- Approved Golden Path input (no ambiguity): the H3 §15 "verified
  documentary fixture" = `tests/fixtures/script_to_scene/documentary.json`
  with `tests/fixtures/asset_provider` + `tests/fixtures/narration_fixtures`
  (the exact input of AYCE's own integration test and README Golden Path).
- `mcp-ayce-readonly/` left byte-identical (asserted by test 20).

## 3. Verified boundary behavior (highlights)

- Caller cannot inject executable/argv/shell/env/cwd/path: exact-field
  schema (`invalid_request` for any extra field), `script_id` grammar +
  exact-match allowlist, fixed argv built only from server-controlled
  values; static test asserts no `shell=True`, no `Popen`, no
  `eval/exec/__import__`, exactly one `subprocess.run(... shell=False)`.
- Idempotency proven across separate server processes (ledger persists):
  identical duplicate → `duplicate_request` + original run/status, zero
  second runs; conflicting duplicate → rejected.
- Single-flight: seeded-running → `busy`; concurrent threads → exactly one
  accepted + one `busy`, exactly one execution.
- AYCE result semantics preserved: exit 1 → `execution_failed` WITH run_id;
  exit 2 → `failed_pre_run`; QA-FAIL → `ok:true, status:"succeeded",
  qa_verdict:"FAIL"` (truthful AYCE P5.5 semantics, not conflated).
- Timeout is truthful: ledger `timeout`, `run_id: null`, message states the
  child was terminated; never a success claim.

## 4. Tests

| Suite | Result |
|---|---|
| `mcp-ayce-director/test_server.py` (74 tests, real stdio transport + stub seam, NO production runs) | 74 passed |
| `mcp-ayce-readonly/test_server.py` | 21 passed, 1 skipped (unchanged baseline) |
| AYCE full regression (`pytest`, repo venv) | 246 passed, 1 skipped (exact baseline) |
| `ayce health` | 10 pass / 0 warn / 0 fail (exact baseline) |

No existing test was modified. New counts are additive (74 director tests).

## 5. The ONE controlled real execution

Performed over the REAL MCP stdio transport (no stub, no LLM), single
request, no concurrency, allowlisted `script_id`:

| Item | Value |
|---|---|
| request_id | `req-20260920T000000Z-diag-hang-01` |
| command accepted | `trigger_golden_path` (validated, single-flight clear) |
| script_id | `documentary` (allowlist) |
| AYCE invocation | `<repo>/.venv/Scripts/python.exe -m ayce run <repo>/tests/fixtures/script_to_scene/documentary.json --assets-dir <repo>/tests/fixtures/asset_provider --narration-dir <repo>/tests/fixtures/narration_fixtures --json` (recorded verbatim in the ledger) |
| run_id | `run-20260920T063905Z-00f855b75dca` |
| job_id | `job-20260920T063905Z-e0e3d0c06a9d` |
| stages | 7/7 succeeded (script_to_scene → media_qa) |
| final QA verdict | PASS (16 checks, 0 failed) |
| process exit status | 0, wall time ≈ 3.4 s |
| ledger record | `mcp-ayce-director/controlled-run-ledger.json` (status `succeeded`, argv, timestamps, exit_code, run_id, job_id, qa_verdict) |

Independent verification through the EXISTING read-only MCP server
(`verify_via_readonly.py`, readonly venv, real stdio transport; the director
performed no privileged filesystem inspection):

- `get_run_state` → run_id/job_id match the ledger; 7 stages all
  `succeeded`, attempts 1, no errors.
- `get_run_artifacts` → 7 artifacts with correct kind↔stage mapping
  (scene_manifest, asset_manifest, audio, timeline, captions,
  rendered_video, qa_report); qa_report verdict PASS with `render_sha256`
  equal to the rendered_video artifact's sha256
  (`516955bd9a964f2a556bbddd578196679f1a688f981f62145050e2f316013241`).

Proof chain closed: **director request → AYCE execution → run → read-only
MCP observation.**

## 6. Honest deviations / findings (full transparency)

1. **An earlier director attempt hit a real defect and was stopped
   truthfully.** The first controlled-execution attempt
   (`req-20260920T062635Z-h4-controlled-01`, real ledger
   `mcp-ayce-director/requests.json`) hung: the AYCE child inherited the
   stdio MCP server's OVERLAPPED-mode transport handles and blocked inside
   `Py_InitializeFromConfig` (verified with py-spy native stacks). The
   boundary's timeout fired at exactly 600 s, killed the child, recorded
   ledger status `timeout` with `run_id: null`, and claimed NO success.
   **No run directory was created** — no production execution occurred.
   The defect was fixed boundary-side (`stdin=DEVNULL`, `close_fds=True`;
   documented in `server.py`) — no `src/ayce/` change was needed.
2. **The one real execution above** was performed by the root-cause
   validation probe. It is a genuine controlled execution of the approved
   Golden Path over the real transport with all boundary controls active;
   its ledger record and full read-only verification are preserved. Its
   `request_id`/reason carry the "diag" label — recorded as-is for
   truthfulness. **No second production execution was run after the fix.**
3. **Environment drift fixed during H4 (no contract change):** the repo
   `.venv` was missing AYCE's declared runtime dependency `pydantic` and dev
   dependency `pytest`; both were installed at their pinned/declared
   versions (`pydantic 2.13.5` per `pyproject.toml` constraint, pytest
   9.1.1). `pyproject.toml` itself was NOT modified. After install, AYCE
   regression returned to the exact 246/1 baseline.

## 7. Rollback / disable state

- No Hermes configuration was modified (nothing to roll back there).
- The director is not registered as an always-on service; it runs only when
  explicitly launched. Disable = delete `mcp-ayce-director/`.
- `mcp-ayce-readonly/` untouched and still the only observation surface.

## 8. Unresolved items (deferred by design)

- Stale `running` ledger entry after a server crash mid-run blocks
  single-flight until the operator clears it (H3 v1 limitation; documented).
- Timeout bound (600 s) is experimental; H3 defers detached execution.
- Multi-script allowlists remain operator-configured additions in
  `scripts.json` (H3 deferred item).



