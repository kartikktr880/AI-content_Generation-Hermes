# The Scene Contract (P1-A)

## What is it?

The **canonical Scene Contract** (`src/ayce/scene_contract.py`) is a small,
typed, machine-readable model of what one production *intends* to show and
say. It is the project's first stable architectural seam:

```text
Story / Script  →  Scene Intelligence  →  SCENE CONTRACT  →  Asset Resolution
                                                        →  Audio / Captions / Graphics
                                                        →  Timeline / Renderer
```

It is implemented with **Pydantic V2** (the researched contract technology)
and lives at schema version **1.0**.

## What problem does it solve?

Every later subsystem (asset resolver, TTS, captions, timeline, renderer,
QA) needs a common, validated description of the production. Without a
canonical contract, each subsystem invents its own format and the pipeline
coupled itself to whichever tool came first. The contract freezes the
*creative* meaning so adapters can vary freely.

## What it contains

```text
ProductionManifest
 ├── schema_version: "1.0"        (MAJOR.MINOR; other majors rejected on load)
 ├── production_id: str           (stable identity; typically ayce.ids.new_job_id())
 ├── title: str
 └── scenes: tuple[Scene, ...]    (≥1; sequences exactly 1..N, ascending)
      └── Scene
           ├── scene_id: str           (stable, unique, no whitespace)
           ├── sequence: int ≥ 1       (strict int, contiguous 1..N)
           ├── duration_seconds: float > 0
           ├── narration: Narration    → text: str (non-empty)
           └── visual: VisualIntent    → description: str (creative intent)
                                        → requirement: AssetRequirement | None
                                             ├── kind: image | video
                                             ├── description: resolver brief
                                             └── requires_provenance: bool = True
```

## What it intentionally does NOT contain

- **Resolved assets** — no filesystem paths, URLs, or binaries. A scene says
  *"show archival railway footage from colonial India"*, never
  `/assets/video/railway_001.mp4`.
- **Resolved provenance records** — `requires_provenance` is a *demand* for
  documented provenance; the actual provenance record belongs to the future
  asset-resolution subsystem.
- **Renderer instructions** — no FFmpeg filters, timeline graphs, keyframes,
  transitions, or effects. Unknown/extra fields are rejected
  (`extra="forbid"`), so renderer-specific data cannot silently leak in.
- **Provider concepts** — no TTS, image, video, archive, or LLM provider
  names; no AI-model references; no YouTube-specific metadata.
- Timestamps/runtime objects — serialization is fully deterministic, so
  identical manifests produce identical JSON bytes.

## Relationship to future systems

- **Asset resolution** consumes `AssetRequirement` and produces resolved
  assets + provenance records elsewhere (referenced by the P0
  `ArtifactRegistry`, not embedded here).
- **Audio/TTS** consumes `Narration.text`.
- **Captions/graphics** derive from narration and visual intent.
- **Timeline/renderers (FFmpeg, OpenTimelineIO, Remotion, ...)** are *not*
  the contract; they may be implemented as adapters that transform the
  contract into their own instruction formats.

## Validation summary

Identity (ID patterns, uniqueness), ordering (strict 1..N ascending
sequences), timing (positive durations), text (non-empty narration/descriptions),
enums (asset kinds), schema major-version pinning, and structural integrity
(≥1 scene; unknown fields forbidden). Loading is validating:
read → validate → deserialize in one step
(`ProductionManifest.model_validate_json` / `load_manifest`).

## Persistence

`persist_manifest()` writes the JSON into a run directory and registers it
with the existing P0 `ArtifactRegistry` as kind `scene_manifest`
(`ayce.artifacts.ArtifactKind.SCENE_MANIFEST`). No second artifact system
exists. Round-trip: build → `dump_manifest` → persist → reload → re-validate
→ compare equal (covered by tests).
