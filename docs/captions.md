# Captions — P6.5 scene-level captions stage

## Position in the Golden Path

```text
Scene Contract (scene_manifest.json)   → exact narration TEXT
Timeline Manifest (timeline.json)      → exact narration INTERVAL
Narration Manifest (narration_manifest.json) → truthful audio durations
    ↓ build_captions (pure, cross-validating, fail-fast)
captions.json  (CaptionsManifest, ArtifactKind.CAPTIONS, schema 1.0)
captions.srt   (standard SubRip serialization of the same cues)
    ↓ (does NOT feed the render — the render contract is untouched)
```

## What is true here (and what is not)

- ONE cue per scene, in scene order. `text` is the contract's
  `narration.text` VERBATIM.
- The cue interval is the timeline NARRATION element interval
  `[start, start + duration]` — the truthful spoken-audio window. It is
  NOT the full scene interval: when narration is shorter than its scene,
  the caption ends when the audio ends.
- NO phrase-level or word-level timing exists upstream, so none is
  produced here. No timing data is ever invented.
- The stage does NOT burn captions into the video, does not style,
  position, or translate.

## SRT rules

- Timestamps `HH:MM:SS,mmm`, deterministic HALF-UP rounding to
  milliseconds; negative or > 99:59:59,999 raises `CaptionsError`.
- Cue index = the scene's 1-based sequence; one blank line between cues.

## Failure / idempotency semantics

- Fail-fast: production-identity mismatch across the three manifests, a
  missing timeline scene, or a missing/ambiguous narration element fails
  the stage (`captions` → `failed`) and publishes NO artifact.
- Byte-deterministic idempotency: identical inputs produce identical
  `captions.json` bytes; a content-identical registered artifact is
  reused (no duplicates); a missing `captions.srt` beside a reused
  report is restored (it is a serialization of the same evidence).

## Stage API

```python
from ayce.captions import run_captions_stage

result = run_captions_stage(
    scene_manifest=scene_manifest,   # ProductionManifest
    timeline_manifest=timeline,      # TimelineManifest
    narration_manifest=narration,    # NarrationManifest
    run_state=run_state,
    artifact_registry=registry,
)
result.ok       # True only after build + persist + register + reload verify
result.manifest # CaptionsManifest
result.artifact # ArtifactRef (kind captions, path captions.json)
```
