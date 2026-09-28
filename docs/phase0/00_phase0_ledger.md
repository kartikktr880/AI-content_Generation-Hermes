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

