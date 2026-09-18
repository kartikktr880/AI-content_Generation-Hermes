# P3 — Narration Audio (file-backed audio provider adapter)

## What it is

`src/ayce/narration_audio.py` is the third bounded, Hermes-compatible
production capability: it consumes a **persisted Scene Manifest**, resolves
each scene's `Narration` through an injected `AudioProvider` adapter, and
persists a separate **`narration_manifest.json`** artifact.

```text
Scene Contract (creative intent — stays untouched)
      ↓ Scene.narration (per scene, manifest order)
Narration Audio Stage
      ↓ AudioProvider (Adapter ABC)        ← injected by future Hermes
FileBackedNarrationProvider (local WAV fixtures)
      ↓
ResolvedNarrationAudio (run-local copy + sha256 + truthful metadata)
      ↓
NarrationManifest artifact → ArtifactRegistry → reload + verify
```

**Separation enforced:** the Scene Contract carries creative narration text
only — no audio paths, hashes, provider ids, or generated-audio metadata
(tested). `Scene Contract = creative intent`, `Audio Stage = executes
bounded narration capability`, `AudioProvider = supplies the
implementation`, `future real TTS = replaces the provider implementation`.

## AudioProvider boundary

`AudioProvider(Adapter)` extends the P0 `Adapter` ABC with one operation:
`resolve_narration(narration, scene_id, *, run_dir) -> ResolvedNarrationAudio`.
Constructible through the existing P0 `AdapterRegistry` (tested). The stage
executes the single provider it receives; selection/fallback is future
Hermes policy.

## FileBackedNarrationProvider

Deterministic fixture provider. Lookup rule: `<fixture_dir>/<scene_id>.wav`.
Missing file → explicit `NarrationAudioError` (never invented paths or fake
silence). It copies the audio into the run directory (`audio/`), computes a
sha256, and reads **truthful metadata** (`duration_seconds`, `format="wav"`)
from the WAV header via the stdlib `wave` module — leaving them `None` when
not derivable. Nothing is invented. Health is truthful: healthy only when
the fixture directory exists and is readable; the stage refuses an
unhealthy provider.

## Fixtures & FFmpeg

`tests/fixtures/narration_fixtures/scene-00{1..5}.wav` are **valid, tiny WAV
files (8 kHz mono, digital silence, 0.1–0.5 s) generated with the Python
stdlib** — no FFmpeg install required at P3. They are synthetic silence
placeholders, NOT real TTS output. Beyond stdlib `wave` parsing, no decoder
validation is claimed. FFmpeg remains an unresolved environment blocker for
later media stages.

## Narration Manifest

`narration_manifest.json` via the existing `ArtifactKind.AUDIO` (reused, no
new kind), schema version `1.0` (major-pinned): `production_id`, `provider`,
run-relative `scene_manifest` reference, `narration_audio[]` in
deterministic scene order. Atomic write + idempotency guard identical to
P1-B/P2.

## Behavior rules

- **Fail-fast:** if any narration fails, the stage fails and publishes no
  manifest. No partial success, no fallback engine, no fake silence files.
- **Lifecycle:** existing P0 `RunState` (`pending → running →
  succeeded/failed`; retry after failure; `succeeded` is terminal).
- **Idempotency:** identical input + same run directory reuses the
  existing artifact; identical input always yields identical manifest bytes.
- **P3 does not require P2:** narration resolves from the Scene Contract
  alone; visual asset resolution is an independent branch.

## Hermes integration boundary

```python
result = run_narration_audio_stage(
    scene_manifest=manifest,     # loaded from the scene_manifest artifact
    run_state=run_state,
    artifact_registry=registry,
    provider=provider,           # selected/injected by the orchestrator
)
# result: ok / manifest / artifact / run_id / production_id / provider / error
```

## Explicit non-scope

No real TTS (ElevenLabs/Google/Azure/Polly/OpenAI/Piper/Coqui/...), no voice
cloning, no SSML, no audio processing (FFmpeg, loudness, mixing, ducking),
no timeline/render, no publishing/analytics, no MCP, no VPS, no databases,
no orchestration loop. P3 proves the audio provider seam.