# TASK 0.5b — HERMES CAPABILITY ONBOARDING (mcp-ayce-director)

**Scope.** Onboard the ONE already-selected write/execution boundary
(`mcp-ayce-director`) into the EXISTING Hermes installation using Hermes' own
supported mechanism, prove the runtime state with the safest representative
call, and STOP. `mcp-ayce-readonly` (VERIFIED in Task 0.5) was not touched,
reinstalled, or retested. No framework, no abstraction layer, no dashboard
editing, no re-research.

## 1. Starting state (inspected before any change)

| Item | Observed |
|---|---|
| `hermes mcp list` | exactly one server: `ayce-readonly ... ✓ enabled` (Task 0.5 state) |
| `mcp-ayce-director/.venv` | present (Task 0.4), Python 3.11.16, `mcp 1.30.0`, `pytest 9.1.1` — reused, NOT rebuilt |
| Director registration in Hermes | absent (deliberately deferred from Task 0.5) |
| Config | `%LOCALAPPDATA%\hermes\config.yaml` `mcp_servers:` had only `ayce-readonly` |

## 2. Selected resource (from existing project evidence — no new research)

| Field | Value |
|---|---|
| RESOURCE | `mcp-ayce-director` (`ayce-director` in Hermes) |
| TYPE | MCP (stdio) |
| PURPOSE | AYCE controlled write/execution director boundary: exactly ONE write tool (`trigger_golden_path`) plus one bounded local research tool (`execute_research_slice`) and one brief-conversion tool (`propose_script_brief`) |
| SOURCE/LOCATION | `C:\Users\Administrator\AI-Content-Generation\mcp-ayce-director\` (own `.venv`; entry point `server.py:1518` `mcp.run()`) |
| WHY SELECTED | `docs/phase0/00_phase0_ledger.md` §4 and `docs/phase0/05_hermes_onboarding_ayce_readonly.md` §8: the write boundary was deliberately deferred until the read-only MCP was proven through Hermes. H3/H4/H5 docs define the design and prior controlled executions. |
| REQUIRED ENV | none — `server.py` resolves all paths from its own location (`HERE`/`REPO`, server.py:75-77); every `AYCE_*` env var is an OPTIONAL operator override (`AYCE_DIRECTOR_LEDGER`, `AYCE_DIRECTOR_SCRIPTS`, `AYCE_DIRECTOR_TIMEOUT_S`, `AYCE_DIRECTOR_PYTHON`, `AYCE_POLICY_*`), never caller-controlled |
| EXPECTED TOOL SURFACE | exactly 3: `trigger_golden_path`, `execute_research_slice`, `propose_script_brief` (server.py:1430/1448/1488; asserted by `test_stdio_tool_surface_is_one_write_tool_plus_research_and_brief`) |
| WRITE BOUNDARY | script_id is an allowlist key (never a path); caller supplies no argv/shell/env/cwd; ledger idempotency + single-flight; policy context fails closed |

## 3. Resource health verified BEFORE registration (Task 0.4 procedure, re-run)

| Check | Command | Result |
|---|---|---|
| Interpreter | `.venv\Scripts\python.exe --version` | `Python 3.11.16` |
| Full own suite | `.venv\Scripts\python.exe -m pytest mcp-ayce-director -q` (all 4 documented modules) | **EXIT=0** — 121 passed (72+49), exactly the count recorded in `docs/phase0/04_mcp_environments.md` §4 / `docs/stage13_experiment_intake.md` §Q. Suite runs NO production runs (stub runner env-gated, disabled by default). |

## 4. Onboarding executed (exact mechanism)

```text
hermes mcp add ayce-director ^
  --command C:\Users\Administrator\AI-Content-Generation\mcp-ayce-director\.venv\Scripts\python.exe ^
  --args C:\Users\Administrator\AI-Content-Generation\mcp-ayce-director\server.py
```

Operational note (truthful record): the first invocation was executed from a
non-interactive shell and stalled AFTER a successful connect — the CLI then
prompts `Enable all N tools? [Y/n/select]:` (`hermes_cli/mcp_config.py:587`)
and `input()` blocked with no answering TTY. The stalled process was killed;
NOTHING had been persisted (`hermes mcp list` unchanged at that point). The
command was then re-run with `y` piped to stdin and completed normally. No
config file was ever hand-edited.

Observed:

```text
✓ Connected! Found 3 tool(s) from 'ayce-director':
  trigger_golden_path / execute_research_slice / propose_script_brief
Enable all 3 tools? [Y/n/select]: y
✓ Saved 'ayce-director' to ~/AppData/Local/hermes/config.yaml (3/3 tools enabled)
```

## 5. Verification (configuration is NOT sufficient — runtime proven)

| Check | Command | Observed |
|---|---|---|
| Registry state | `hermes mcp list` | `ayce-director ... all ✓ enabled` (alongside untouched `ayce-readonly`) |
| Real connect + discovery | `hermes mcp test ayce-director` | `✓ Connected (5621ms)` — `✓ Tools discovered: 3`, names exactly `trigger_golden_path`, `execute_research_slice`, `propose_script_brief` — no extras, no missing tools |
| Config persisted by CLI | `config.yaml` | `mcp_servers.ayce-director`: command / args / `enabled: true`; NO env block (none required) |
| Actual tool CALL | direct stdio JSON-RPC `initialize` → `notifications/initialized` → `tools/list` → `tools/call` against the SAME registered command/args | `INITIALIZED: ayce-director (mcp 1.30.0)`; `TOOLS_DISCOVERED: 3`; `tools/call propose_script_brief {"research_id":"res-does-not-exist-proof"}` returned the structured truthful rejection `{"ok": false, "error": {"code": "unknown_artifact", ...}}`, `isError: false`, child exit 0 |
| Startup/config health | `hermes doctor` | `✓ No active security advisories`, `✓ No suspicious MCP stdio commands`, `✓ Config version up to date (v46)`. Remaining doctor warnings are PRE-EXISTING and unrelated (agent-browser npm advisory; missing optional API keys) |

Safe-capability choice (write boundary discipline): NO destructive or
production action was triggered. `trigger_golden_path` was NOT called.
`execute_research_slice` was NOT called (spawns yt-dlp/network ingestion).
The representative real call used the brief tool's designed rejection path
for an unknown artifact id — it exercises the full stdio + tool-invocation
path with zero execution, zero network, and no fixture fallback. The server
recorded that rejection in its own audit ledger
(`mcp-ayce-director/brief_requests.json`, status `rejected`) — designed,
truthful bookkeeping, committed as evidence.

## 6. State before → after

| | Before | After |
|---|---|---|
| `hermes mcp list` | `ayce-readonly` only | `ayce-readonly` (unchanged) + `ayce-director ✓ enabled (all 3 tools)` |
| `config.yaml mcp_servers:` | one block | two blocks; `ayce-director` added by the CLI |
| venvs / servers | VERIFIED (Task 0.4/0.5) | unchanged, reused — no reinstall |

## 7. Dashboard-visible expectation

The Hermes UI reads the same `config.yaml`. The **MCP servers** section should
list BOTH servers:

```text
ayce-readonly   stdio   enabled   10 tools
ayce-director   stdio   enabled    3 tools
```

Independent read-only cross-check without the UI: `hermes mcp list`.

## 8. Known limitations

- Same as Task 0.5: an in-agent (LLM-driven) tool-selection round-trip through
  Hermes remains gated on a configured provider credential; MCP
  registration/testing and the direct stdio proof are credential-free.
- `ayce-director` exposes the WRITE boundary; operational use of
  `trigger_golden_path` stays under the H3/H4/H5 approval discipline
  (allowlist, ledger idempotency, single-flight, bounded timeout).
- The proof added one `rejected` brief-ledger record (audit trail of the
  verification call itself).

## 9. Rollback / removal (exact supported mechanism)

```text
hermes mcp remove ayce-director
```

Component-level: delete `mcp-ayce-director/` (AYCE `src/ayce/` has no
dependency on it; `mcp-ayce-readonly` is independent).

## 10. STOP condition honored

Exactly ONE resource onboarded (`ayce-director`). No other MCP, no skills, no
project-knowledge skill set, no other tooling touched.

## PHASE_0_5B_HERMES_ONBOARDING = VERIFIED (mcp-ayce-director only)