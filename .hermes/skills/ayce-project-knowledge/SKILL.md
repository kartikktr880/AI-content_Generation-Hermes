---
name: ayce-project-knowledge
description: "AYCE constitution, Golden Path and stage contracts for Hermes."
version: 1.0.0
author: kartikktr880, Hermes Agent
license: MIT
platforms: [windows, linux, macos]
metadata:
  hermes:
    tags: [AYCE, YouTube, GoldenPath, StageContracts, DirectorBoundary]
    related_skills: []
---

# AYCE Project Knowledge Skill

Project-local skill for **AYCE — Autonomous YouTube Content & Video Production
Engine** (repository root: `C:\Users\Administrator\AI-Content-Generation`).
It gives Hermes the project's engineering constitution, its verified Golden
Path, the stage-contract map and the Hermes operating surfaces, so Hermes can
direct the project without re-deriving its rules or inventing behaviour.

The skill carries only knowledge and pointers. It executes nothing and does not
duplicate any AYCE implementation.

## When to Use

- Any task that inspects, operates, extends or reports on the AYCE project.
- Before requesting a Golden Path run through the `ayce-director` MCP server.
- When a decision must respect the project's engineering constitution
  (reuse-before-create, Hermes-first, verify-before-claiming).
- Before changing a pipeline boundary: consult the matching stage-contract doc.

Don't use for: generic YouTube/creative work with no AYCE involvement, or as a
substitute for reading the canonical docs before editing a contract.

## Project Identity

- Package `ayce` 0.0.1, Python >= 3.12, Pydantic V2 for contract validation,
  pytest for verification. Local `data/` JSON + SQLite state; no cloud services.
- Verified state: **P6.5** (research -> script -> scenes -> assets -> narration ->
  timeline -> captions -> render -> media QA), plus policy/experiment/analytics/
  publishing/lineage subsystems.
- Repository evidence record: `docs/phase0/00_phase0_ledger.md` (Hermes <-> AYCE
  integration ledger; read it before assuming an integration exists).

## Engineering Constitution (project `rule.md`, condensed)

- Loop: **INSPECT > UNDERSTAND > REUSE > DECIDE > IMPLEMENT > TEST > VERIFY > REPORT**.
  Never GUESS > CODE > ASSUME > CLAIM COMPLETE.
- **Hermes-first, capability-first, custom-build-last.** Check Hermes native
  capability, skill, MCP, connector/API, CLI, OSS or existing subscription
  before writing custom code.
- **Reuse before create.** Preserve working legacy capability; adapt or wrap it
  rather than replacing it to look new.
- **Minimal-safe change.** No opportunistic refactors, renames, dependency
  upgrades or speculative infrastructure. Fix the smallest affected surface.
- **Inspect before edit**, and search references before changing an interface.
- **Implemented != Tested != Verified != Production-ready.** Completion requires
  runtime evidence, not code existence.
- **Never fabricate** IDs, runs, artifacts, tool responses or records. LLM output
  is untrusted structured input: validate schema and required fields.
- **Publishing safety**: never publish, or claim a publish, without explicit
  Commander authorization; verify channel, artifact, metadata, thumbnail and
  visibility first.
- **Secrets**: never print, log or commit credentials, tokens or cookies.
  Configuration is `AYCE_*` via `.env` (git-ignored); `.env.example` holds
  placeholders only.
- **Media integrity / provenance**: validate real codec, container, duration and
  timestamps of produced media; keep license/source metadata for external assets.

## The Golden Path (verified chain)

```
script_to_scene -> asset_resolution -> narration_audio -> timeline -> captions
                -> production_render -> media_qa          (7 stages)
```

Canonical invocation (repository root, PYTHONPATH pinned to `src`):

```
.venv\Scripts\python.exe -m ayce run tests/fixtures/script_to_scene/documentary.json ^
  --assets-dir tests/fixtures/asset_provider ^
  --narration-dir tests/fixtures/narration_fixtures --json
```

Exit codes: `0` success (**including a QA FAIL** - the QA report is evidence),
`1` stage failure, `2` pre-run input/config failure.

Per-run artifacts: `scene_manifest`, `asset_manifest`, `audio`, `timeline`,
`captions`, `rendered_video`, `qa_report`; run state under `data/runs/<run_id>/`
(`state.json`, `artifacts.json`).

## Stage Contracts (canonical docs in `docs/`)

| Stage / boundary | Contract doc | Produces |
|---|---|---|
| Scene Contract (P1-A) | `docs/scene_contract.md` | scene schema |
| Script -> Scene (P1-B) | `docs/script_to_scene.md` | `scene_manifest` |
| Asset resolution (P2) | `docs/asset_resolution.md` | `asset_manifest` |
| Narration audio (P3) | `docs/narration_audio.md`, `docs/tts_piper.md`, `docs/tts_kokoro.md`, `docs/visual_pexels.md` | `audio` |
| Timeline (P4) | `docs/timeline.md` | `timeline` |
| Captions (P6.5) | `docs/captions.md` | `captions` |
| Render (P4.5/P5) | `docs/render.md` | `rendered_video` |
| Media QA (P5.5) | `docs/media_qa.md` | `qa_report` |
| Policy (Stage 8/12) | `docs/stage12_policy_effectiveness.md` | policy evidence |
| Experiments (Stage 13) | `docs/stage13_experiment_intake.md` | experiment records |
| Research intelligence (Stage 2) | `docs/stage2_research_intelligence.md` | research artifact |

## Hermes Operating Surfaces (this project)

| Surface | Kind | Tools / purpose |
|---|---|---|
| `ayce-readonly` | MCP (stdio) | read-only observation: `list_runs`, `get_run_state`, `get_run_artifacts` (`AYCE_RUNS_ROOT=<repo>\data\runs`) |
| `ayce-director` | MCP (stdio) | `trigger_golden_path` (**the only write tool**), `execute_research_slice`, `propose_script_brief` |
| Cron `ayce-analytics-collect` | Hermes cron (no-agent script) | paused; `%LOCALAPPDATA%\hermes\scripts\ayce_analytics_collect.py` |
| Component docs | `mcp-ayce-readonly/`, `mcp-ayce-director/`, `docs/hermes_*.md` | designs and experiment records |

## Rules for Director Runs

`trigger_golden_path` accepts exactly four fields:

```
request_id  idempotency key, ^req-[A-Za-z0-9._-]{1,64}$
command     must be exactly "trigger_golden_path"
script_id   allowlist KEY from mcp-ayce-director/scripts.json - never a path
reason      free text, audit only, <=2000 chars
```

- The caller never supplies an executable, argv, shell, env, cwd or output path.
- Same `request_id` + identical payload -> `duplicate_request` with the original
  result and **no second run**; conflicting payload -> rejected.
- One run at a time (single-flight); a concurrent request -> `busy`.
- Verify the result **only** through `ayce-readonly`; never inspect the run
  directory as a substitute for the read-only surface.

## Verification

```
hermes mcp list                          # ayce-readonly + ayce-director enabled
hermes mcp test ayce-director            # Connected + 3 tools, no extras
hermes skills list --source local        # this skill: source local, enabled
```

Independent read-only verification of a director run (separate from Hermes):

```
mcp-ayce-readonly\.venv\Scripts\python.exe mcp-ayce-director\verify_via_readonly.py <run_id>
```

## Pitfalls

- A QA `FAIL` verdict is a **succeeded** stage - never report it as a pipeline error.
- Fixture providers are the default; real providers (Pexels, Kokoro/Piper) are
  selected only when explicitly configured. Nothing falls back silently.
- `data/` is git-ignored: run evidence lives on disk; the git record carries the
  ledgers under `mcp-ayce-director/` and the docs under `docs/`.
- YouTube publishing is externally blocked (account-side 403); do not retry or
  work around it - it is a Commander/account action.
- `mcp-ayce-readonly` is the only observation surface; it exposes no write tools.
