# AYCE — Autonomous YouTube Content & Video Production Engine

A scratch-built autonomous production engine (trend discovery → research →
script → scenes → assets → audio → compositing → QA → publishing), grown
from a verified minimal foundation, one bounded capability at a time.

**Status: P2 — provider adapter seam VERIFIED** (P0 foundation + P1-A Scene
Contract + P1-B Script → Scene stage + P2 file-backed asset resolution).

## Stack

- Python ≥ 3.12; standard library + **Pydantic V2** (contract validation only)
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
  script_to_scene.py deterministic Script → Scene Manifest stage (P1-B)
  asset_resolution.py file-backed asset resolution stage (P2)
  health.py      baseline diagnostics (`python -m ayce health`)
  cli.py         CLI entry point
docs/
  scene_contract.md   Scene Contract architecture & boundaries
  script_to_scene.md  P1-B stage: input/output contracts & lifecycle
  asset_resolution.md P2 stage: provider adapter seam & policies
tests/
  unit/          unit tests (fast, no external effects)
  integration/   process-level tests (CLI)
  fixtures/      regression fixtures (scene_contract/, script_to_scene/, asset_provider/)
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

## Script → Scene Manifest stage (P1-B)

The first **executable production stage** lives in
`src/ayce/script_to_scene.py`: a deterministic, Hermes-compatible capability
that turns a structured script input (`ScriptInput` JSON) into a validated
`ProductionManifest`, records the stage lifecycle in `RunState`
(`pending → running → succeeded/failed`, retries allowed after failure),
persists it through the existing `ArtifactRegistry` as `scene_manifest.json`,
reloads and verifies it, and returns a `ScriptToSceneResult`.

- Deterministic: no LLMs/randomness/network — identical input always
  produces identical manifest bytes. Derived scene IDs (`scene-001`…)
  and durations (`max(2.0 s, words / 2.5)` when unspecified) are
  documented, replaceable MVP rules.
- **No false success**: any stage-critical failure leaves the run state
  observably `failed` with the error recorded; reruns with identical input
  reuse the existing artifact instead of duplicating it.
- **P1-B is one bounded stage — not Hermes.** Future Hermes (master
  director) calls `run_script_to_scene_stage(...)` as a single capability.
  See [`docs/script_to_scene.md`](docs/script_to_scene.md) and
  `tests/fixtures/script_to_scene/`.

## Asset Resolution (P2)

The provider adapter seam lives in `src/ayce/asset_resolution.py`: a
deterministic, Hermes-compatible stage that consumes a persisted Scene
Manifest, resolves each scene's `AssetRequirement` through an injected
`AssetProvider` adapter, and persists a **separate** `asset_manifest.json`
artifact. Resolved-asset data is never merged back into the Scene Contract.

- `AssetProvider` extends the P0 `Adapter` ABC (`name` + truthful
  `health()`); `FileBackedAssetProvider` resolves fixtures
  deterministically (`<fixture_dir>/<scene_id><ext>`), copies them into
  the run directory (`assets/`), and records sha256 + provenance.
- **Fail-fast policy:** if any requirement fails, the stage fails and
  publishes no manifest. Unhealthy provider → stage refuses to run.
  Scenes without requirements are skipped (no fake assets).
- Fixture provenance (`local_fixture`) makes **no real-world licensing
  claim**; the `license` field exists for future real providers.
- **P2 is one bounded stage — not Hermes.** The orchestrator selects and
  injects the provider; the stage never chooses providers or fallbacks.
  See [`docs/asset_resolution.md`](docs/asset_resolution.md) and
  `tests/fixtures/asset_provider/`.


