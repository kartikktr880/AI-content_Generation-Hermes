# H5-B — Duplicate Request Idempotency Proof (Evidence Record)

> **STATUS: PASS.** The exact H5 request was re-issued through a new Hermes
> session; the Director returned `duplicate_request` with the original result
> and launched NO second run. Zero production-code changes.

## Original (H5)

- request_id: `req-20260920-h5-golden-compose-01`
- run_id: `run-20260920T075426Z-2c45d535f42d`
- job_id: `job-20260920T075426Z-a5e5f9b4b44d`
- status: `succeeded`, qa_verdict `PASS`, exit_code 0

## Duplicate invocation (H5-B)

- Same `request_id`, same `command`/`script_id`/`reason` (byte-identical to
  the H5 ledger record), invoked via `mcp__ayce_director__trigger_golden_path`
  from Hermes session `20260920_133448_5caf00` (gemini-3.5-flash-lite).
- Returned verbatim (session transcript + runner stdout):

```json
{
  "ok": false,
  "request_id": "req-20260920-h5-golden-compose-01",
  "error": {
    "code": "duplicate_request",
    "message": "request_id already processed; no second run launched. original status: succeeded"
  },
  "original": {
    "status": "succeeded",
    "run_id": "run-20260920T075426Z-2c45d535f42d",
    "job_id": "job-20260920T075426Z-a5e5f9b4b44d",
    "qa_verdict": "PASS",
    "exit_code": 0
  }
}
```

## Execution proof

- Director ledger (`mcp-ayce-director/requests.json`): still exactly **2
  records** before and after — an identical duplicate returns BEFORE any
  `_ledger_record` call, so it appends nothing (no execution record, no
  rejection record). H5 record unchanged (`succeeded`, original run/job/qa).
- Run directories in `data/runs`: **8 before, 8 after** (no new run dir).
- Original run dir file count unchanged (12 files).
- Read-only MCP verification after the duplicate: original run still 7/7
  stages `succeeded`, artifact_count **7**, qa_report `verdict: PASS`,
  `failed_checks: 0`.
- Session transcript: only Hermes builtins (`tool_search`, `tool_describe`,
  `tool_call`) + the 3 AYCE MCP calls; no shell/file/browser tools.

**original executions = 1 · duplicate additional executions = 0 ·
additional jobs/records = 0 · additional artifacts = 0**

## Tests

```
mcp-ayce-director\.venv\Scripts\python -m pytest mcp-ayce-director\test_server.py -q
  -k "duplicate or single_flight or busy or contract or ledger"   → 5 passed
```
Covers `test_duplicate_identical_request_id_runs_once`,
`test_conflicting_duplicate_request_id_rejected`, single-flight/busy,
exact-field contract, ledger integrity. AYCE production code untouched by
H5-B, so the full AYCE regression was not rerun (last verified state:
247 tests, 0 failures, 1 skipped).

## Production changes

None. AYCE Golden Path, engines, Director contract/boundary, ledger schema,
and Hermes configuration untouched. (Experiment tooling only: the H5 runner
gained an optional prompt-file argument; `h5b_duplicate_prompt.txt` added.)

## Git

- Starting: `a84c355` + pre-existing modified/untracked files (unchanged).
- Ending: identical, plus `docs/hermes_director_h5b_idempotency.md`,
  `mcp-ayce-director/h5b_duplicate_prompt.txt` (new untracked), and the
  runner's optional-parameter edit (tooling).

## Acceptance criteria (A–K)

| # | Criterion | Verdict |
|---|---|---|
| A | Exact H5 request ID reused | PASS |
| B | Duplicate invoked through Hermes | PASS |
| C | Director recognized duplicate | PASS (`duplicate_request`) |
| D | Response returns original result | PASS (`original.run_id/job_id/qa_verdict/exit_code`) |
| E | Original run ID remains authoritative | PASS |
| F | No second Golden Path run created | PASS (8 run dirs before/after) |
| G | No second job/execution record | PASS (ledger 2 records before/after) |
| H | No duplicate artifacts | PASS (7 artifacts before/after) |
| I | Idempotency/single-flight tests passing | PASS (5 focused tests) |
| J | No unrelated production behavior changed | PASS |
| K | Evidence recorded | PASS (this file) |
