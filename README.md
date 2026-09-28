# AYCE — Autonomous YouTube Content & Video Production Engine

A scratch-built autonomous production engine (trend discovery → research →
script → scenes → assets → audio → compositing → QA → publishing), grown
from a verified minimal foundation, one bounded capability at a time.

**Status: P6.5 — scene-level captions VERIFIED** (P0 + P1-A Scene
Contract + P1-B Script → Scene + P2 assets + P3 narration + P4 timeline
+ P4.5 single-scene render + P5 complete multi-scene production MP4 +
P5.5 technical media QA + P6 single-command orchestration of the verified
chain (`python -m ayce run <script.json>`) + P6.5 deterministic
scene-level captions artifact (captions.json + captions.srt)).

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
  timeline.py    renderer-neutral timeline/composition stage (P4)
  render.py      FFmpeg renderer smoke stage (P4.5)
  captions.py    scene-level captions stage (P6.5; captions.json + captions.srt)
  tts_piper.py   opt-in Piper TTS narration provider (P7; external CLI subprocess)
  health.py      baseline diagnostics (`python -m ayce health`)
  media_qa.py    technical media QA stage (P5.5)
  pipeline.py    Golden Path pipeline runner (P6 orchestration only)
  cli.py         CLI entry point (`health`, `run`)
docs/
  scene_contract.md   Scene Contract architecture & boundaries
  script_to_scene.md  P1-B stage: input/output contracts & lifecycle
  asset_resolution.md P2 stage: provider adapter seam & policies
  narration_audio.md  P3 stage: audio provider adapter seam
  timeline.md         P4 stage: renderer-neutral temporal composition
  render.md           P4.5 stage: FFmpeg renderer boundary & smoke render
tests/
  unit/          unit tests (fast, no external effects)
  integration/   process-level tests (CLI)
  fixtures/      regression fixtures (scene_contract/, script_to_scene/, asset_provider/, narration_fixtures/)
```

## Commands

```pwsh
# run all tests (from repo root)
python -m pytest

# baseline health / diagnostics (no installation required)
./scripts/health.ps1              # or: $env:PYTHONPATH='src'; python -m ayce health
python -m ayce health --json

# Golden Path pipeline: script → scenes → assets → narration → timeline
# → production render → technical media QA, in one command
$env:PYTHONPATH='src'; python -m ayce run tests/fixtures/script_to_scene/documentary.json `
  --assets-dir tests/fixtures/asset_provider `
  --narration-dir tests/fixtures/narration_fixtures
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
| Adapter convention  | VERIFIED (render/asset/audio providers registered) |
| Test harness        | VERIFIED |
| Health check        | VERIFIED |
| FFmpeg + ffprobe    | VERIFIED (9.0.1 essentials, gyan.dev build, on PATH) |

## Environment requirement (resolved in P4.5)

FFmpeg/ffprobe are required for the render stage. They are now installed
on the dev machine (FFmpeg 9.0.1 essentials from the gyan.dev release —
the distribution officially linked from ffmpeg.org — added to the user
PATH). Override/point to other installs via `AYCE_FFMPEG_PATH` /
`AYCE_FFPROBE_PATH` (see `.env.example`).

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
- **Real provider (opt-in)**: `PexelsVisualProvider`
  (`src/ayce/visual_pexels.py`) implements the same seam against the
  official Pexels REST API, selected only when `AYCE_PEXELS_API_KEY` is
  configured. It enforces production minimums (landscape ≥1920×1080,
  orientation-configurable), requires adequate-duration MP4 renditions for
  video scenes, caches downloads, content-hashes the resolved copy, and
  records full provenance/licensing in `assets/<scene_id>.provenance.json`.
  See [`docs/visual_pexels.md`](docs/visual_pexels.md).
- **P2 is one bounded stage — not Hermes.** The orchestrator selects and
  injects the provider; the stage never chooses providers or fallbacks.
  See [`docs/asset_resolution.md`](docs/asset_resolution.md) and
  `tests/fixtures/asset_provider/`.

## Narration Audio (P3)

The audio provider seam lives in `src/ayce/narration_audio.py`: a
deterministic, Hermes-compatible stage that consumes the Scene Manifest,
resolves each scene's `Narration` through an injected `AudioProvider`
adapter, and persists a separate `narration_manifest.json` artifact
(registered as `ArtifactKind.AUDIO`). The Scene Contract is never mutated
with audio data.

- `AudioProvider` extends the P0 `Adapter` ABC; `FileBackedNarrationProvider`
  resolves fixtures deterministically (`<fixture_dir>/<scene_id>.wav`),
  copies them into the run directory (`audio/`), records sha256, and reads
  **truthful** `duration_seconds`/`format` from the WAV header via the
  stdlib `wave` module (never invented; `None` when not derivable).
- **Fail-fast policy:** any missing narration fails the stage with no
  manifest published; unhealthy provider → stage refuses to run; fixture
  provenance (`local_fixture`) makes no licensing claim (`license=None`).
- Fixtures are **valid tiny WAV files (stdlib-generated silence)** — no
  FFmpeg required at P3; they are placeholders, not real TTS.
- **Real provider (opt-in)**: `KokoroNarrationProvider`
  (`src/ayce/tts_kokoro.py`) implements the same seam with Kokoro-82M run
  LOCALLY (official `kokoro` runtime + Apache-2.0 weights), selected only
  when `AYCE_KOKORO_VOICE` is configured. It synthesizes English and Hindi,
  emits 24 kHz mono PCM WAV, caches synthesis content-addressed, and reports
  missing prerequisites (runtime extra, espeak-ng for Hindi, the Windows
  MSVC redistributable) truthfully instead of fabricating audio.
  See [`docs/tts_kokoro.md`](docs/tts_kokoro.md);
  `PiperNarrationProvider` remains available as a configuration-selected
  alternative.
- **P3 is one bounded stage — not Hermes**, and it does not depend on P2:
  narration resolves from the Scene Contract alone.
  See [`docs/narration_audio.md`](docs/narration_audio.md).
- **P4 is one bounded stage — not Hermes.** It consumes resolved
  artifacts only; renderer selection is a later orchestration decision.
  See [`docs/timeline.md`](docs/timeline.md).

## Renderer (P4.5)

The renderer boundary lives in `src/ayce/render.py`: a deterministic,
Hermes-compatible stage that consumes the Timeline Manifest and renders
**one scene** into a real H.264/AAC MP4 via the FFmpeg CLI
(`render/scene-XXX.mp4`, kind `ArtifactKind.RENDERED_VIDEO`), validated
with ffprobe before any success is reported.

- `Renderer` extends the P0 `Adapter` ABC; `FFmpegRenderer` health is
  truthful (both executables must actually run version queries;
  `AYCE_FFMPEG_PATH`/`AYCE_FFPROBE_PATH` override PATH lookup).
- **Subprocess safety:** explicit argument lists via `subprocess.run([...])`,
  never `shell=True`, never concatenated command strings.
- **Output validation:** FFmpeg exit zero is not enough — ffprobe must
  confirm the video stream, dimensions, and duration, or the stage fails.
- **Smoke scope:** one scene, static visual + narration audio as-is.
  Multi-scene production rendering is a future capability.
  See [`docs/render.md`](docs/render.md).

## Multi-Scene Production Render (P5)

`FFmpegRenderer.render_production()` renders the COMPLETE verified Timeline
Manifest into **one production MP4**: `render/{production_id}.mp4`
(kind `rendered_video`, metadata carries `production_id`, `scene_count`,
duration, dimensions, sha256).

- All scenes render in canonical timeline order via per-scene clips
  (same composition policies as the smoke render, including black filler
  for narration-only scenes) concatenated with the FFmpeg concat demuxer.
- Production duration is validated against the timeline total within a
  documented tolerance (±0.5 s + 2%), proving scene coverage; tests add a
  lightweight per-scene frame-color check for order/coverage.
- Intermediates (`render/tmp/`) are scratch — removed on success,
  preserved on failure. Idempotency: an existing production artifact is
  ffprobe re-validated (valid → reused; stale/corrupt → re-rendered).
- **P5 is one bounded stage — not Hermes**, and does not implement
  captions, transitions, motion, or audio processing.
  See [`docs/render.md`](docs/render.md).

## Scene-Level Captions (P6.5)

`src/ayce/captions.py` produces the first real `ArtifactKind.CAPTIONS`
artifact (reserved since P0): deterministic, truthful scene-level
captions built from already-verified contracts — one stage between
`timeline` and `production_render` in the Golden Path.

- **Cue text is the Scene Contract's `narration.text` VERBATIM** — never
  reflowed, split, or rewritten.
- **Cue intervals are the timeline's narration-element intervals**
  `[start, start + audio_duration]` — the truthful spoken-audio window,
  NOT the full scene interval. No timing is invented: no phrase-level or
  word-level timing exists upstream, so none is produced here.
- Outputs: `captions.json` (structured `CaptionsManifest`, registered as
  kind `captions`) + `captions.srt` (standard SubRip serialization of the
  same cues — a directly uploadable deliverable).
- Byte-deterministic idempotency (scene-manifest pattern): identical
  inputs reuse the registered artifact; a missing SRT is restored.
- Fail-fast: any production-identity mismatch, missing timeline scene,
  or ambiguous narration element fails the stage and publishes no
  artifact. Upstream manifests are never mutated.
- **Captions are NOT burned into the render** — the render contract is
  untouched. Word timing, styling, translation, and burn-in are future
  capabilities.
- See [`docs/captions.md`](docs/captions.md).

## Golden Path Pipeline Runner (P6)

`src/ayce/pipeline.py` adds the first **production-executable Golden
Path**: one command turns a script JSON into a ffprobe-validated
production MP4 plus a persisted technical QA report, by composing the
already-verified P1-B → P5.5 stage functions — no stage logic is
duplicated and no stage implementation was modified.

- Stages, in order: `script_to_scene` → `asset_resolution` →
  `narration_audio` → `timeline` → `captions` (P6.5) →
  `production_render` → `media_qa`, sharing ONE `RunState` and ONE
  `ArtifactRegistry` per run.
- Run identity: **every invocation creates a new run** under
  `<data_dir>/runs/<run_id>` (`state.json` + `artifacts.json` + all
  artifacts). Previous runs are never overwritten or globally
  deduplicated; within a run, each stage's own idempotency remains
  authoritative.
- Failure semantics: the first stage execution failure stops the
  pipeline; completed stages stay `succeeded` with their artifacts
  persisted; later stages never execute. No rollback, no faked
  completion. A deterministic QA **FAIL verdict is NOT a pipeline
  failure** — it is truthful evidence from a succeeded stage; only a QA
  execution error fails the pipeline.
- **Orchestration with real providers available by configuration**:
  fixtures remain the default (`FileBackedAssetProvider`,
  `FileBackedNarrationProvider`), while the real production providers —
  Pexels visuals (`AYCE_PEXELS_API_KEY`) and Kokoro-82M narration
  (`AYCE_KOKORO_VOICE`, local execution) — are selected ONLY when explicitly
  configured, per capability. Nothing falls back silently. `run` is
  Hermes-callable glue, not Hermes.

## Technical Media QA (P5.5)

`src/ayce/media_qa.py` adds the first real QA gate after production
rendering: it consumes the registered `rendered_video` artifact
reference plus the Timeline and Narration manifests and **independently
inspects the actual MP4 with ffprobe**, persisting `qa_report.json`
(kind `ArtifactKind.QA_REPORT`) with a deterministic verdict.

- Checks (this scope only): file existence/containment, container
  validity, video stream (codec, dimensions, duration), audio stream
  (codec, duration where reported), rendered duration vs the timeline
  total (reusing the exact P5 tolerance, `±0.5 s + 2%`), and lightweight
  per-scene frame-color coverage (the P5-proven approach — no computer
  vision).
- Verdict is strictly `PASS`/`FAIL` (any check FAIL → FAIL). No
  scoring, no percentages.
- **This is technical media QA, not complete production QA**: no
  creative quality, factual correctness, captions, rights, loudness,
  aesthetics, or narrative checks exist yet.
- Failure semantics: a QA FAIL registers the report with
  `verdict=FAIL` and the stage succeeds (evidence is the product); a QA
  execution error (invalid artifact ref, malformed manifest, missing
  ffprobe) fails the stage with no artifact. No QA → repair loop exists.
- Idempotency: the report is bound to the render's sha256; same render
  → reused report, no duplicates; changed render or corrupt/stale
  report → fresh execution under a new artifact identity.
- **P5.5 is one bounded stage — not Hermes**, and does not implement
  repair, publishing, or any creative QA.
  See [`docs/media_qa.md`](docs/media_qa.md).

## Timeline / Composition (P4)

The renderer-neutral temporal composition contract lives in
`src/ayce/timeline.py`: a deterministic, Hermes-compatible stage that
cross-validates the Scene + Asset + Narration manifests and assembles
them into a `timeline.json` artifact (kind `ArtifactKind.TIMELINE`,
schema `1.0`).

- **Temporal spine**: `scene[n].start = sum(preceding scene durations)`
  from the Scene Contract; contiguous, validated, deterministic.
- **Elements (MVP: visual + narration only)**: a `visual` element
  occupies its full scene interval (only for scenes with a resolved
  asset); a `narration` element starts at its scene start with the
  truthful audio duration from P3.
- **Duration policy (fail-fast)**: narration longer than its scene →
  validation failure (no stretch/trim/overflow semantics); unknown audio
  duration → failure (metadata is never invented); shorter narration is
  fine — remaining scene time is unambiguously visual-only.
- **Renderer-neutral**: no FFmpeg/Remotion/OTIO commands, filtergraphs,
  or provider data — only typed timing + run-relative resolved paths.
  Captions/graphics/transitions have no upstream contract yet and are
  not faked; the schema is additive.
- **P4 is one bounded stage — not Hermes.** It consumes resolved
  artifacts only; renderer selection is a later orchestration decision.
  See [`docs/timeline.md`](docs/timeline.md).


