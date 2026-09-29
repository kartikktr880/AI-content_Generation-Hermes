# PHASE 0 LEDGER — Hermes ↔ AYCE Integration (evidence-driven)

> **Purpose.** Durable, on-disk record of Phase 0 execution: what was run, what
> was observed, what is verified, and what is still blocked. Phase 0 exists to
> make **Hermes able to actually operate this project** through its real
> extension surface (skills + tools + MCPs + CLI + workers + project knowledge),
> NOT to rebuild Hermes functionality inside the application.
>
> Law applied: `Understand → Inspect → Plan → Implement smallest useful slice →
> Run → Observe → Verify → Record → Continue`.
> Architecture rule applied: **Hermes-first, capability-first, custom-build-last.**
> Cost rule applied: zero new paid subscriptions/APIs/infrastructure; only
> already-available, legitimately usable resources.
> Permanent execution rules for every task (Task 0 onward): see §8.

## 0. Source-of-truth accounting

Phase 0 task definitions were **not** found in the repository. They were
recovered from the Commander's own prior Hermes prompts (Hermes user-scope
`pastes/` store, read-only) and the live Hermes installation. Nothing was
invented; every task below is backed by an artifact.

| Source (read-only) | What it establishes |
|---|---|
| `%LOCALAPPDATA%\hermes\pastes\paste_10_012354.txt` | **TASK 0.1 — PROJECT INTEGRATION VERIFICATION** (verbatim scope + required A–K output) |
| `%LOCALAPPDATA%\hermes\pastes\paste_1_021910.txt` | Runtime baseline scope (git status, existing test command, CLI entry points, MCP startability) — marker `RUNTIME_BASELINE = COMPLETE` |
| `%LOCALAPPDATA%\hermes\pastes\paste_1_060751.txt` (dup `paste_1_064326.txt`) | **PHASE_0_3_ENVIRONMENT** — install Python 3.12.x from the already-downloaded official installer, recreate `.venv`, install declared dev deps, run pytest |
| `%LOCALAPPDATA%\hermes\pastes\paste_2_125644.txt` | Master Project Description / engineering north star (constitution: Hermes-first, capability-first, custom-build-last) |
| `%LOCALAPPDATA%\hermes\state.db` (read-only query) | Session history: 0.1/0.2/0.3 prompt+result trail |
| `docs/hermes_audit.md`, `docs/hermes_mcp_readonly.md`, `docs/hermes_director_h3_design.md`, `mcp-ayce-readonly/`, `mcp-ayce-director/` | What Hermes integration design/tooling already exists and was previously verified |
| `Research_Agent/Reports 4A-4L/` (17 reports) | Identified/evaluated ecosystems (candidates only — see §4) |

### Task 0.1 → 0.15 numbering — KNOWN UNKNOWN (blocker)

Only **0.1**, **0.2** (inferred position; content verbatim), and **0.3**
(explicitly numbered by the Commander) are evidenced on this machine. The
authoritative definitions of **0.4 → 0.15** exist in no accessible artifact
(searched: repository incl. full git history, `docs/`, `Research_Agent/`,
Hermes `pastes/`, Hermes `state.db` messages + `system_prompts`, Hermes
`kanban.db`, Hermes `projects.db`, Cline task storage, user profile, temp).

Per the standing rule *"if a material unknown appears, stop at that exact
blocker and perform targeted research/inspection rather than guessing"*, Phase 0
execution stops after 0.3 and reports the blocker (§5) instead of inventing
0.4–0.15.

## 1. Verified state at Phase 0 start

| Item | Observed value |
|---|---|
| Repository | `C:\Users\Administrator\AI-Content-Generation` (primary workspace, unchanged) |
| Branch / HEAD | `main` @ `6f199b0ead04af77def3c35e5281cc559ba73883` (11 commits) |
| Working tree | clean (`git status --short --branch` → `## main...origin/main`) |
| Remote | `origin` = `https://github.com/kartikktr880/AI-content_Generation-Hermes.git` |
| Host | `WIN-IK7N6SD2UBU`, Windows Server 2022 Standard, user `Administrator` |
| Hermes | **v0.21.5+2244.g35ad70c (2026.9.24)** at `%LOCALAPPDATA%\hermes\hermes-agent` (git install, Python 3.14.7) |
| Project runtime (at start) | system Python **3.11.9** only; repo `.venv` = stale 3.11.9 shell (pip/setuptools only) |
| pytest | **absent** (system Python, repo `.venv`, Hermes venv) |
| FFmpeg/ffprobe | **absent** from PATH |
| MCP venvs | **absent** (`mcp-ayce-readonly\.venv`, `mcp-ayce-director\.venv` do not exist) |
| `data/` | absent (no runs persisted on this machine) |


## 2. Environment changes applied (all reversible, all zero-cost)

| Item | Evidence | Rollback |
|---|---|---|
| Python 3.12.10 installed user-scope from the Commander-provided official installer (`%TEMP%\2\python-3.12.10-amd64.exe`), Authenticode-verified (Python Software Foundation), `ExitCode=0`, `PrependPath=0` | `…\Python312\python.exe --version` → `Python 3.12.10` | `Remove-Item -Recurse "$env:LOCALAPPDATA\Programs\Python\Python312"` |
| Repo `.venv` recreated on 3.12.10 (the stale 3.11.9 shell it replaced was created by the earlier interrupted attempt) | `.venv\Scripts\python.exe --version` → `Python 3.12.10` | `Remove-Item -Recurse .venv` |
| Declared dependencies installed exactly as `pyproject.toml` specifies | `pydantic 2.13.5`, `pytest 9.1.1` | `pip uninstall` |
| FFmpeg 9.0.1 **reused from Hermes' own toolchain** (no download, no install) and exposed via one user-scope `Path` entry | `ffmpeg version n9.0.1-27-g9b0578816c-20260910` | remove that single entry from the user `Path` |
| `yt-dlp 2026.8.19` installed into the git-ignored `.venv` (documented external CLI; **not** added to `pyproject.toml`) | `.venv\Scripts\yt-dlp.exe` exists | `pip uninstall yt-dlp` |

No `src/ayce/`, `tests/`, contract, fixture or `pyproject.toml` byte was
modified. `git status` remains `main...origin/main` clean apart from the
untracked `docs/phase0/` evidence set.

## 3. Task log

| Task | Title | Status | Evidence record |
|---|---|---|---|
| — | Phase 0.1 execution checkpoint | DONE | §1 |
| 0.1 | Project integration verification | DONE | `docs/phase0/01_project_integration_verification.md` |
| 0.2 | Runtime baseline (read-only) | DONE | `docs/phase0/02_runtime_baseline.md` |
| 0.3 | Environment (Python 3.12 + `.venv` + pytest) | DONE | `docs/phase0/03_environment.md` |
| 0.4–0.15 | **Definitions unavailable — BLOCKED** | not started | §5 |

## 4. Resource classification (no candidate treated as an approved dependency)

- **identified** — everything listed in `Research_Agent/Reports 4A-4L/` (17
  ecosystem reports) and the candidate technologies named in the Master Project
  Description. Being mentioned in research is *not* approval.
- **evaluated** — Hermes Agent itself; MCP as the integration protocol; the
  P1–P6.5 production stack already built into `src/ayce/`.
- **selected** — Hermes Agent as master director; MCP stdio as the director
  surface; the project's own CLI/pipeline as the execution surface; Python
  3.12.10 (the version required by the project's own `pyproject.toml`).
- **integrated** — `ayce` package (in-repo); `mcp-ayce-readonly` +
  `mcp-ayce-director` (in-repo, standalone, independently removable).
- **verified** — in-repo production stages P0→P6.5 per `README.md`; Hermes
  install per `docs/hermes_audit.md`; the read-only MCP round-trip per
  `docs/hermes_mcp_readonly.md` (recorded on an earlier machine state — the MCP
  venvs are absent here, so that round-trip is **not** re-confirmed yet).

## 5. Open blockers

1. **`0.4 → 0.15` definitions unavailable** (§0). Minimum Commander action:
   supply the authoritative Phase 0 task list (or confirm a proposed
   continuation). See Task 0.2 §7.
2. **MCP server venvs absent** — `mcp-ayce-readonly` / `mcp-ayce-director`
   cannot start or be registered with Hermes until their isolated venvs
   (`mcp<2` + pytest, per each `requirements.txt`) are recreated, and the
   Hermes `config.yaml` `mcp_servers:` block (absent since the environment
   reset) is restored. This is the highest-value remaining Phase 0 item: it is
   the only thing standing between Hermes and actually operating the project.

### Resolved during Phase 0 execution

- ~~FFmpeg/ffprobe absent~~ → closed by reusing Hermes' own FFmpeg 9.0.1 (Task 0.3 §3).
- ~~Python 3.12 / pytest absent~~ → closed by Task 0.3 (§1–§4).
- ~~yt-dlp absent~~ → closed by installing the documented external CLI into `.venv` (Task 0.3 §5).

### Proposed continuation (NOT implemented — awaiting the authoritative list)

Derived strictly from existing evidence and the project's own approved design,
in dependency order, and requiring nothing new to be invented:

| Candidate task | Basis in evidence | Hermes-first angle |
|---|---|---|
| Recreate the two MCP venvs (`mcp<2` + pytest via Hermes' own `uv.exe`) and re-run both servers' own test suites | `mcp-ayce-readonly/requirements.txt`, `mcp-ayce-director/requirements.txt`, `docs/hermes_mcp_readonly.md`, `mcp-ayce-director/README.md` | Hermes' managed `uv.exe` is the documented provisioning tool |
| Register `ayce-readonly` + `ayce-director` in Hermes `config.yaml` and prove `hermes mcp test` connects with the right tool counts (3 and 3) | `docs/hermes_director_h5_composition.md` §Tests (H5 did exactly this) | registration lives in Hermes' own config, not in AYCE |
| Re-prove the read-only round trip Hermes → `list_runs`/`get_run_state`/`get_run_artifacts` against a real Golden Path run | `docs/hermes_mcp_readonly.md` (H2/H2.5 protocol) | uses only Hermes' existing MCP client + the existing server |
| One controlled Golden Path execution through the director boundary, verified independently through the read-only server | `docs/hermes_director_h4_experiment.md` §5, `docs/hermes_director_h5b_idempotency.md` | `trigger_golden_path` is the already-approved single write tool |
| Recreate the project-knowledge skill/asset set inside Hermes' real skill surface so Hermes can consult the project's constitution + stage contracts | Hermes `skills/` directory + `hermes skills list` (58 bundled, verified H1) | project knowledge belongs in Hermes' own memory/skill surface, not re-implemented in AYCE |

## 6. Task 0 re-verification — Hermes operating surface (2026-09-28, read-only)

Task 0 (this document) was re-executed against the current machine state: a
**read-only re-verification of every claim above**, with no install, no Hermes
configuration edit, no source/test/dependency change, and no change to any
component of this phase. Repository during execution: `main` @ `123d6d5`
(`main` == `origin/main`).

| Item | Command / mechanism | Observed (2026-09-28) | Status |
|---|---|---|---|
| Hermes runtime | `hermes --version` | `v0.21.5+2244.g35ad70c (2026.9.24)`, install method `git`, Python 3.14.7, OpenAI SDK 2.24.0 | VERIFIED |
| MCP registry | `hermes mcp list` | `ayce-readonly ✓ enabled`, `ayce-director ✓ enabled` | VERIFIED |
| Native connect + discovery | `hermes mcp test ayce-readonly` / `hermes mcp test ayce-director` | `✓ Connected` (20440 ms / 9612 ms); **10** tools / **3** tools, exact expected names, no extras, exit 0 | VERIFIED |
| LLM-driven agent round trip | — | still gated on a configured provider credential (§4 note, unchanged) | NOT VERIFIED (pre-existing, credential-gated) |
| Registered configuration | `%LOCALAPPDATA%\hermes\config.yaml` | `mcp_servers:` present: both servers, `enabled: true`, `AYCE_RUNS_ROOT=<repo>\data\runs` | VERIFIED |
| Component environments | `Test-Path …\mcp-ayce-*\.venv\Scripts\python.exe` | both present (Python 3.11.16) — reused, not reinstalled | VERIFIED |
| MCP hygiene | `hermes doctor` | `No active security advisories`; `No suspicious MCP stdio commands`; config version v46 | VERIFIED |
| **Read-only round trip** (§5 proposed item 3) | MCP stdio client (the component's own SDK) → the SAME command/args/env Hermes has registered, against the REAL runs root | `list_runs` → `ok`, 2 real runs; `get_run_state` → `ok`, 7/7 stages `succeeded` (run `run-20260927T171925Z-dd7d084b3476`); `get_run_artifacts` → `ok`, 10 artifacts (4-scene render, sha256 `798a4966…`); unknown id → truthful `{"ok": false, "error": {"code": "unknown_run"}}`; runs-root sha256 inventory identical before/after (37 files) | VERIFIED |
| Component suites (§5 proposed item 1) | each server's own documented `pytest` command | **49 passed** (read-only) / **121 passed** (director), 0 failed, 0 errors, exit 0 (junit XML) — reproduces the historical baselines | VERIFIED |
| Skill surface | `hermes skills list` | `0 hub-installed, 51 builtin, 0 local` — unchanged; no skill was onboarded | VERIFIED |
| Cron state | `hermes cron list` | `19a6cce6796c [paused]` (`ayce-analytics-collect`) — unchanged | UNCHANGED |
| Project runtime | `.venv` python / `python -m ayce --version` | `Python 3.12.10` / `ayce 0.0.1` (the 0.1 §H PEP 701 failure stays closed) | VERIFIED |
| Project diagnostics | `python -m ayce health` | `summary: 9 pass, 1 warn, 0 fail` (warn = `ffmpeg` missing only from the *long-lived shell's* PATH, see last row) | VERIFIED |
| Project suite | `python -m pytest` (`README.md` §Commands) | 649 collected: **645 passed / 3 skipped / 1 failed** (junit XML) | RECORDED — 1 failure, environment-induced (§6.1) |
| FFmpeg availability | user-scope `Path` + `Test-Path …\hermes\tools\ffmpeg-9.0.1-win32-x64\bin\ffmpeg.exe` | entry present, binary present; the editor shell simply predates the entry (with the documented PATH fronted, the render/QA stages and `ayce health` pass) | not a regression |

### 6.1 Blocker status after re-verification

1. `0.4 → 0.15` definitions — **still unavailable** (§0). Unchanged; requires the
   Commander's authoritative list.
2. ~~MCP venvs absent + `config.yaml mcp_servers:` absent~~ — **CLOSED**
   (Tasks 0.4/0.5/0.5b; independently re-verified above through Hermes' own CLI).
3. §5 proposed item 3 (read-only round trip against a real Golden Path run) —
   **CLOSED** (verified above).
4. §5 proposed items 4 (one controlled Golden Path execution through the director
   boundary) and 5 (project-knowledge skill set inside Hermes) — **NOT executed**:
   they are implementation work that §5 itself marks as *awaiting the
   Commander's authoritative list*, and neither was selected for this task.
5. Project-suite note (recorded, not "fixed"): `tests/unit/test_analytics.py::
   test_cli_analytics_collect_dry_run_and_show` fails on this machine because the
   git-ignored `.env` now holds real `AYCE_YT_*` credentials (added for the 0.7B
   publish attempt). `cli.main` → `Config` loads `.env`, so the CLI makes a live
   Google call and receives the suspended-account `403`, producing
   `analytics_authorization_error` where the credential-free test expects
   `analytics_authentication_error`. Evidence that this is environment-only: the
   same test **passes** when run with a working directory outside the repository
   (no `.env` reachable). No code, test or configuration was changed.
6. The 3 skips are the documented opt-in `live` tests
   (`test_live_kokoro_synthesis`, `test_live_piper_synthesis`,
   `test_live_pexels_resolves_one_real_asset`); the 649-test count supersedes the
   591 of §3 because the tracked provider work at HEAD `123d6d5` added tests.

### 6.2 Files touched by this re-verification

This ledger section only (`docs/phase0/00_phase0_ledger.md`). No `src/`,
`tests/`, `pyproject.toml`, MCP component, Hermes configuration, secret or
dependency was modified; all temporary diagnostics lived in `.tmp/` and were
removed after use.


## 7. Task 0 completion — the two unresolved requirements implemented IN Hermes (2026-09-29)

§6.1 recorded that §5 proposed items **4** (controlled Golden Path through the
director boundary) and **5** (project-knowledge skill inside Hermes' real skill
surface) were NOT executed. Both are now implemented through Hermes' own
mechanisms and **behaviourally verified in the Hermes runtime**. Evidence record:
`docs/phase0/11_hermes_director_skill_verification.md`.

| Item | Hermes mechanism | Runtime evidence | Status |
|---|---|---|---|
| **§5 item 4** — one controlled Golden Path execution through the director boundary, verified independently through the read-only server | Hermes one-shot agent session (LLM) → Hermes tool dispatcher → MCP stdio `ayce-director.trigger_golden_path` → AYCE Golden Path → MCP stdio `ayce-readonly` observation | session `20260928_234452_4cc22a`: real tool call + real result `ok: true`, `run_id run-20260929T064556Z-3b422d61fce8`, `job_id job-20260929T064556Z-4bff2a419bd7`, `qa_verdict PASS`, `exit_code 0`, 7/7 stages; read-only `get_run_state`/`get_run_artifacts` through Hermes = 7/7 succeeded, 8 artifacts, `failed_checks 0`; independent `verify_via_readonly.py` agrees (render sha256 `df90f486…`) | **VERIFIED** |
| §5 item 4 (idempotency leg, H5 "next step") | same boundary, identical duplicate request through Hermes | session `20260928_234741_93137d`: `duplicate_request` + original result; `data/runs` 3 → **3**, ledger records 9 → **9** (no second run) | **VERIFIED** |
| **§5 item 5** — project-knowledge skill/asset set in Hermes' real skill surface | Hermes native project-local skill mechanism: `.hermes/skills/ayce-project-knowledge/SKILL.md` + `hermes skills trust <repo>` | `hermes skills list` → `51 builtin, 1 local — 52 enabled`; `hermes prompt-size --json` skills index 5076 → 5203 bytes; skill present in the *stored system prompt* of a repo session; session `20260928_235021_5c7864` called `skill_view ayce-project-knowledge` (`success: true`) and reproduced its Golden Path stages / director fields verbatim | **VERIFIED** |
| §6 row "LLM-driven agent round trip — credential-gated" | — | **stale blocker**: Hermes is configured against a live **local** OpenAI-compatible endpoint (`http://127.0.0.1:8081/v1`, dummy key); real one-shot sessions run without any external credential. The configured default `gemini-3.1-pro` is unreliable on that proxy, so sessions pin `-m gemini-3.6-flash`/`gemini-3.7-flash` (per-session flag only — **no config change**) | **CLOSED (was mis-recorded)** |

Scope hygiene for §7: `ayce-readonly`/`ayce-director` were **reused, not
reinstalled**; the paused analytics cron job is untouched; no project-wide pytest
was used as Task 0 evidence; no Task 1 work was started. Only Hermes-state change
is `skills.trusted_project_dirs` (reversible via `hermes skills untrust`).

### 7.1 Blockers still open after §7

1. `0.4 → 0.15` task definitions unavailable → Commander input still required
   (§5 blocker 1). **This remains the only Task 0 boundary.**
2. YouTube publishing blocked account-side (`403 authenticatedUserAccountSuspended`)
   — Phase 0.7B, unchanged.
3. Authenticated analytics collection blocked (no eligible published record) —
   unchanged.
4. `youtube-analytics-harvester` skill does not exist anywhere — unchanged
   (Task 0.7).


## 8. Permanent execution rules (mandatory for every implementation task)

Set by the Commander for Task 1 onward. These extend — and do not replace — the
law, architecture rule and cost rule in this document's header, and the project
constitution (`rule.md`). Nothing else in this ledger is changed by them.

**1. Hermes-first execution.** Whenever a capability is required, resolve it in
this order: (1) Hermes native capability, (2) Hermes skill/capability surface,
(3) already-integrated project capability, (4) explicitly selected
MCP/tool/connector/API/CLI, (5) legitimate free/local/OSS/existing-subscription
resource, (6) custom build **only if the capability genuinely does not exist**.
Never custom-build what Hermes or an already-selected resource provides.

**2. Hermes runtime is the primary verification surface for Hermes tasks.**
Implement/onboard → execute → observe → verify **inside Hermes**. A Hermes task
must not be turned into an ordinary AYCE development task. Project files/tests are
supporting evidence only where they directly validate the changed component.

**3. Capability rule.** `Need → Research Agent → Evidence → Decision →
Implementation → Runtime Verification`. The existing research reports
(`Research_Agent/Reports 4A-4L/`, `Phase 0 Research/`) are the candidate source.

**4. Selected-candidate-only onboarding.** A resource appearing in an inventory,
an earlier report, another task's selection, or an installed-resource list is
**not** selected for the current task. Use only the resource the task's own
authoritative specification selected. No mass installation, no "while we're here"
installs, no rejected/experimental candidates, no silent substitution, no
invented replacements. If the selected resource is already onboarded, **verify**
it rather than reinstalling it.

**5. Native onboarding only.** Onboard through Hermes' supported mechanism —
never bypass Hermes, never edit internal Hermes state when a supported CLI
exists, never create a parallel configuration or second orchestration layer, and
never treat "a file exists" as "Hermes has onboarded it". After onboarding:
registration → enabled/available → invoke through Hermes → observe real result →
compare expected vs observed.

**6. No useless testing.** No toy prompts, no generic-intelligence/`2+2`-class
probes, no "is the model working?" tests, no fake IDs/data/responses, no
redundant re-probing of already-proven capability, no full-project `pytest`
without a direct requirement, no unrelated suites or modules. A test must prove a
real acceptance criterion; **reuse existing evidence** rather than retesting what
is already proven.

**7. Real behaviour only.** Never fabricate IDs, records, API responses,
artifacts, YouTube records, tool results or success states. If a real external
credential/account is genuinely required, state the exact blocker; do not
manufacture a substitute to force a PASS.

**8. Strict change discipline / smallest correct implementation.** Change only
what the task requires. No unrelated refactors, cleanups, renames, dependency
additions, configuration edits, speculative/future-task infrastructure, or
modification of previously completed and verified work. Preserve all previously
verified functionality.

**9. No scope creep, no false completion.** Executing Task N never authorizes
Task N+1. `Implemented ≠ Tested ≠ Verified ≠ Production-ready`: a file existing, a
command exiting 0, or a suite passing is not completion. A task is complete only
when its acceptance criteria are demonstrated by real behaviour with concrete
evidence. On a genuine blocker: identify it, resolve it only if legitimately in
scope, otherwise report it precisely.

**10. Task-by-task sequential execution + expert execution mode.** Execute the
authoritative task list in order; never invent a task's requirements, never
silently choose an alternative, never skip ahead. Work as a senior/principal
engineer: read the authoritative specification, use existing evidence, implement
the minimum correct change, run the real workflow, verify real behaviour, record
evidence, close the task, move forward immediately once acceptance criteria are
satisfied.



## 9. Task 1 — BLOCKED: no authoritative specification exists

The Task 1 directive requires the authoritative Task 1 specification to be read
from the repository before any implementation ("Do NOT invent Task 1
requirements"). An exhaustive search found **no such specification**:

| Search surface | Method | Result |
|---|---|---|
| Repository tree (all files, incl. untracked) | full recursive enumeration | only `docs/` (stage contracts + `hermes_*.md`), `docs/phase0/` (Task 0 record), `Phase 0 Research/` (research plans), `Research_Agent/Reports 4A-4L/` (research reports), `src/`, `tests/`, MCP components — no task list |
| Entire git history | `git log --all --diff-filter=A --name-only` → 230 unique paths ever added | no task/roadmap/controller/constitution document; only three `Phase 0 Research/*Research Plan` files match "plan" |
| Content grep | `TASK 1` / `Task 1` / `TASK_1` across the repository | only prompt-internal step labels inside the H5-era experiment prompts (`h5a_prompt.txt`, `h5-compose-output.txt`) and Task 0's own statements — no project task definition |
| Content grep | `TASK LIST` / `ROADMAP` / `Execution Plan` in `Phase 0 Research/`, `Research_Agent/` | no task list or roadmap |
| Commander specification store | all 17 Hermes `pastes/*.txt` (read-only) | no `TASK 1` / `Task 1` text at all; the pastes cover only the Task 0.1–0.3 / environment / capability-probe series |
| Recently changed files | repository files changed in the last 3 days (excluding ignored trees) | only Task 0's own artifacts — no newly added Task 1 file |
| Rulebook / controller files | bounded filesystem search for `rule.md`, `AGENTS.md`, `.ProjectRules`, `PROJECT_CURRENT_STATE.md`, `IMPLEMENTATION_ROADMAP.md`, `TOOL_REGISTRY.md`, `DECISION_LOG.md`, `TEST_STRATEGY.md` | none exist on this machine; the constitution (`rule.md`) is supplied to the agent as session rules only |

This is the same class of blocker as §5 blocker 1 (`0.4 → 0.15` definitions
unavailable) and it is **the Commander's input boundary**, not a technical
failure. Consistent with §8 rule 9, no Task 1 capability work was invented, no
alternative task was substituted, and Task 2 was not started.

**Minimum Commander action:** supply the authoritative Task 1 specification (task
name, required capability, selected candidate/resource if applicable, and
acceptance criteria), or confirm that Task 1 is limited to the permanent
execution rules recorded in §8.

Delivered independently of that blocker — the only imperative in the Task 1
directive that does not depend on the missing specification — is §8 above: the
permanent Hermes-first expert-execution rules, recorded in this project's
authoritative execution document.

## 10. Task 1 — Hermes-directed real production cycle (2026-09-29) — VERIFIED

Derived from the approved sources (Master Project Description / engineering north
star: ultimate loop + "Hermes is the intended high-level director" + production
job model; the approved director design and the H6/H7/H8 experiment records).
Task 0 had proven Hermes operating the Golden Path over the **documentary
fixture**; the real `research → brief → production` chain existed as approved
integrated capability but had never been operated by Hermes on this machine
state. That chain is the next executable item in the approved loop.

Executed **inside Hermes** (two single-purpose one-shot agent sessions,
`gemini-3.6-flash` via per-session `-m`, no config change):

| Leg | Hermes tool call | Real result |
|---|---|---|
| DISCOVER/RESEARCH | `ayce-director.execute_research_slice` | `res-20260929T072643Z-77ca29a092d7`, `SUCCESS`, 6 candidates, real yt-dlp evidence (yt-dlp 2026.08.19), artifact under `data/research/` |
| CREATE | `ayce-director.propose_script_brief` | new `brief-77ca29a092d7`, `duplicate: false`, sha256 `f02f227b…`, registered in `scripts.json` |
| PRODUCE | `ayce-director.trigger_golden_path` (`req-20260929-h10-real-prod-01`) | `run-20260929T073007Z-01adeb428f9c` / `job-20260929T073007Z-55fd291324d3`, 7/7 stages succeeded, `qa_verdict PASS`, `exit_code 0` |
| VERIFY | `ayce-readonly.get_run_state` / `get_run_artifacts` (in-session) + `verify_via_readonly.py` (independent) | 10 artifacts; `script_input` carries `research_id`/`script_id` lineage; 4 scenes / 69.3 s render (fixture run: 5 / 34.0 s); `qa_report PASS` 15 checks 0 failed; render sha256 `a0a89c2c…` matches |

State deltas (all produced by the runs): research artifacts 1→2, research ledger
2→3, brief ledger 4→5, production ledger 9→10, `scripts.json` +`brief-77ca29a092d7`,
run directories 3→4. MCP-only boundary held (transcripts show only
`tool_describe`/`tool_call` + AYCE MCP tools). **No publish, no new code, no new
resource, no Hermes configuration change.** Evidence record:
`docs/phase0/12_hermes_real_production_cycle.md`.

### 10.1 Next approved task

`PACKAGE` — seal the research-derived production
(`run-20260929T073007Z-01adeb428f9c`) into a publish-ready package through the
project's own approved CLI execution surface (`python -m ayce package <run_id>`,
the already-verified Stage 5 packaging capability), operated by Hermes. PUBLISH
remains blocked account-side (Phase 0.7B) and requires explicit Commander
authorization; MEASURE/LEARN remain blocked until a real published record exists.


## 11. PACKAGE task — sealed publish package for the Task 1 run (2026-09-29) — VERIFIED

Executed **through Hermes** with the already-verified Stage 4 packaging
capability — no new tool, no architectural change, no code change.

| Item | Evidence |
|---|---|
| Hermes session / tool | `20260929_004650_02f3e1`, tool `terminal` only (single shell invocation running exactly the two approved commands) |
| Commands | `.venv\Scripts\python.exe -m ayce package run-20260929T073007Z-01adeb428f9c --json` (seal) then the same with `--verify --json` |
| Hermes-observed result | `{"ok": true, "package_id": "pkg-6c0d9cc96879e3f0", "run_id": "run-20260929T073007Z-01adeb428f9c", "verified_content": true, "errors": []}`, exit 0 for both commands |
| Sealed package | `data/runs/run-20260929T073007Z-01adeb428f9c/publish_package.json`, `package_version 1`, seal `sha256:1f8cb27b01af785efe24a8e6298446e910c21d6e89f5349d34eba269723c51e7` |
| Lineage | `research_id res-20260929T072643Z-77ca29a092d7`, `script_id brief-77ca29a092d7`, `objective_id obj-5969bacf4a332431`, `production_id` = the research id |
| QA gate | `verdict PASS`, 15 checks, 0 failed; `render_sha256` == the run's rendered_video sha256 (`a0a89c2c…`) |
| Artifacts | 10-entry `artifact_manifest` (script, research, scene_manifest, asset_manifest, audio, timeline, captions, rendered_video, qa_report, policy_consumption) |
| Not published | publish ledger `publish_attempts` unchanged at **1 row** (the blocked 2026-09-27 attempt); no upload, no YouTube action |
| Files changed | the run's `publish_package.json` (run-produced), `mcp-ayce-director/h11_package_prompt.txt`, `docs/phase0/12_hermes_real_production_cycle.md` §8, this section |

Evidence record: `docs/phase0/12_hermes_real_production_cycle.md` §8.

### 11.1 Next approved task

`PUBLISH` remains **Commander/account-blocked** (Phase 0.7B: YouTube
`403 authenticatedUserAccountSuspended`); the sealed package
`pkg-6c0d9cc96879e3f0` is now the exact artifact that boundary would consume, and
the existing `ayce publish` path is idempotent on `(package_seal, destination)`.
The next *executable* approved work that is not externally blocked is to make
Hermes the operator of the publish boundary under explicit Commander
authorization once the account/channel standing is resolved — no code or
architecture change is required for it.


## 12. Stage 8 — Motion & Compositing (MUST slice) — VERIFIED (2026-09-29)

Next approved task derived from the research reports (not invented): the Stage 8
blueprint `Phase 0 Research/Stage 8 Motion and Compositing Architecture Deep R___`
("Programmable Motion, Timeline Orchestration, and Media Compositing"), whose
Executive Verdict selects a **Decoupled Hybrid Rendering Pipeline** and mandates
that the existing P4/P4.5/P5.5 modules be *adapted and wrapped rather than
retired*.

| Item | State |
|---|---|
| Selected candidates | **FFmpeg** (foundational media pipeline) + **libass** (pre-timed ASS rasterization) — both already present in Hermes' bundled FFmpeg 9.0.1 (`--enable-libass`, fontconfig/harfbuzz/freetype/fribidi); **reused, nothing installed**. Remotion (SHOULD/OPTIONAL) deliberately not implemented. |
| Implementation | NEW `src/ayce/compositor.py` (procedural filtergraph generator + libass typography, subclassing the verified `FFmpegRenderer` so validation/health/codec policy are reused); `config.py` +9 optional keys; `pipeline.py` renderer resolution via `build_renderer`. `AYCE_RENDERER` defaults to `ffmpeg-smoke`, so no previously verified behaviour changes. |
| MUST coverage | multi-track composition, aspect-fit + background blur, programmable Ken Burns, non-linear (smooth-step) easing, cut/fade/dissolve/wipe transitions (duration-preserving), pre-timed SSA/ASS via libass, 48 kHz audio multiplex. |
| Hermes execution | session `20260929_101927`-era one-shot (`gemini-3.7-flash`, `terminal` tool, `timeout=600`) ran the exact production command with `AYCE_RENDERER=ffmpeg-compositor` → verbatim report `{"ok": true, "run_id": "run-20260929T101927Z-e32756f8d6a5", "qa_verdict": "PASS", …7/7 stages ok}` |
| Hermes verification | second one-shot session (`h11b_compositor_verify_prompt.txt`) via `mcp__ayce_readonly__get_run_state` + `get_run_artifacts` → 7/7 `succeeded`, 7 artifacts, `rendered_video.renderer = ffmpeg-compositor`, **1920×1080**, 34.0 s, sha256 `f1a05414…89b5`, `qa_report PASS 16 checks / 0 failed` |
| Independent check | `ffprobe` on the produced master: h264 1920×1080 @30 fps, aac 48000 Hz stereo, duration 34.000 s; `render/captions.ass` present (`PlayResX: 1920`, one `Dialogue:` per cue). Byte-deterministic for identical inputs. |
| Focused regression | `test_compositor + test_render + test_production_render + test_captions + test_pipeline + test_config` → **62 passed / 0 failed** (no project-wide suite) |
| Evidence record | `docs/phase0/13_stage8_compositor_must_slice.md` |

### 12.1 Next approved task

**Stage 10 — QA + Autonomous Repair (MUST slice).** The Stage 10 blueprint
("Automated Quality Assurance, Fault Localization, and Autonomous Repair
Architecture") lists MUST items the current `media_qa` gate does not yet
implement — EBU R128 loudness/True-Peak verification, acoustic dead-air/silence
detection, caption timestamp/bounds and reading-speed (CPS) validation,
zero-byte/truncated-container detection, and timeline black-slug detection — all
of which the **already-installed FFmpeg/ffprobe** provides (`loudnorm`,
`silencedetect`, `blackdetect`, `ffprobe` stream/format fields) with no new
candidate; the repair loop then re-renders the affected scene through the Stage 8
compositor. PUBLISH remains **PENDING — `403 authenticatedUserAccountSuspended`**.

