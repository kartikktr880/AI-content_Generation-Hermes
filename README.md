# AYCE — Autonomous YouTube Content & Video Production Engine

P0 foundation: the smallest clean, testable, verifiable baseline that the
future autonomous production loop (trend discovery → research → script →
scenes → assets → audio → compositing → QA → publishing) can grow from.

**Status: P0 foundation — VERIFIED.** No pipeline stages are implemented yet.

## Stack

- Python ≥ 3.12, **standard library only** (zero runtime dependencies)
- pytest (test harness; already available on this machine)
- Local JSON files under `data/` for state, logs, and artifact manifests

## Layout

```
src/ayce/        engine package
  config.py      centralized AYCE_* configuration (+ minimal .env loader)
  logging.py     structured JSON-lines logging (ts, level, run/stage, error)
  ids.py         run_id / job_id / artifact_id generation
  state.py       stage status state machine + atomic JSON checkpoints
  artifacts.py   artifact kinds + immutable refs + per-run manifest
  adapters.py    provider-agnostic adapter convention + registry
  scene_contract.py  canonical Scene Contract (P1-A; Pydantic V2)
  health.py      baseline diagnostics (`python -m ayce health`)
  cli.py         CLI entry point
docs/
  scene_contract.md   Scene Contract architecture & boundaries
tests/
  unit/          unit tests (fast, no external effects)
  integration/   process-level tests (CLI)
  fixtures/      regression fixtures (incl. scene_contract/)
data/            runtime artifacts (git-ignored)
```

## Commands

```pwsh
# run all tests (from repo root)
python -m pytest

# baseline health / diagnostics (no installation required)
./scripts/health.ps1              # or: $env:PYTHONPATH='src'; python -m ayce health
python -m ayce health --json
```

## Configuration

All environment access uses the `AYCE_` prefix and goes through
`ayce.config.Config`. Copy `.env.example` to `.env` for local overrides
(`.env` is git-ignored). Missing required configuration raises
`ConfigError` explicitly — nothing is guessed and nothing is faked.

## Verified capabilities (P0)

| Capability          | Status  |
| ------------------- | ------- |
| Project boot        | VERIFIED |
| Configuration       | VERIFIED |
| Structured logging  | VERIFIED |
| Job/run identity    | VERIFIED |
| Stage state machine | VERIFIED |
| Artifact references | VERIFIED |
| Adapter convention  | VERIFIED (convention only; no providers) |
| Test harness        | VERIFIED |
| Health check        | VERIFIED |
| FFmpeg              | NOT INSTALLED on this machine (required before the render stage) |

## Known blockers for the Golden Path

- FFmpeg/ffprobe are not installed on this machine; the first vertical
  slice's composition/render step will require installing them first.

## Scene Contract (P1-A)

The canonical, machine-readable Scene Manifest model lives in
`src/ayce/scene_contract.py` (Pydantic V2, schema version `1.0`).
It is the stable seam between creative planning and downstream production
systems: it holds **creative intent only** (narration text, visual
description, optional *unresolved* asset requirements with a provenance
*requirement*) and explicitly rejects resolved-asset paths and
renderer-specific fields. See [`docs/scene_contract.md`](docs/scene_contract.md)
for the full boundary explanation, and `tests/fixtures/scene_contract/` for
example manifests (minimal, documentary, malformed).

