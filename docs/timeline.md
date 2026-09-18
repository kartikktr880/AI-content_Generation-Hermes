# P4 — Renderer-Neutral Timeline / Composition Stage

## What it is

`src/ayce/timeline.py` is the fourth bounded, Hermes-compatible production
capability: it deterministically assembles the Scene, Asset, and Narration
manifests into a **time-based, renderer-neutral composition contract**
(`timeline.json`, kind `ArtifactKind.TIMELINE`, schema version `1.0`).

```text
Scene Manifest (creative intent)      ← untouched
Asset Manifest (resolved visuals)     ← untouched
Narration Manifest (resolved audio)   ← untouched
      ↓ cross-validation
      ↓ deterministic temporal assembly
Timeline Manifest  ("WHAT appears and WHEN")
      ↓ future Hermes + renderer selection
Rendered video
```

## Temporal model

- **Temporal spine**: `scene[n].start = sum(preceding scene durations)`,
  derived from the Scene Contract's creative `duration_seconds`. The
  spine must be contiguous and is validated (`TimelineManifest` rejects
  non-contiguous starts, wrong totals, out-of-order sequences).
- **Visual elements** occupy exactly their scene interval
  (`start = scene.start`, `duration = scene.duration`). No motion,
  keyframes, or transitions — those are later capabilities.
- **Narration elements** start at their scene's start and last exactly
  the resolved audio's truthful duration from P3.
- **Overflow policy (fail-fast):** narration longer than its scene is a
  validation failure — no stretch/trim/overflow semantics exist at MVP,
  and sync problems must not be silently baked into the timeline.
- **Underflow policy:** narration shorter than its scene keeps its
  truthful duration; the remaining scene time is unambiguously
  visual-only (`narration.start = scene.start`).
- **Unknown narration duration → failure** (a renderer cannot place
  unknown-length audio; metadata is never invented).

## Cross-validation (fail-fast)

- production identity must agree across Scene + Asset + Narration
  manifests;
- upstream records must reference known scenes;
- every scene's narration must have a resolved record with a known
  duration;
- a scene declaring an `AssetRequirement` must have its resolved asset;
- upstream manifest reference fields are validated against the exact
  run-relative filenames.

Any inconsistency → stage failed → **no timeline published**.

## Renderer neutrality

The timeline contains no FFmpeg/Remotion/OTIO/OpenMontage commands,
filtergraphs, nodes, CSS, DOM instructions, or provider-specific data —
only typed temporal composition referencing run-relative resolved paths
(`assets/…`, `audio/…`). A future renderer resolves paths through the
run context.

## MVP element types

`visual` + `narration` only. Captions, music, SFX, graphics, text,
transitions, overlays have **no upstream contract yet and are not faked**;
the schema is additive under future schema minor versions.

## Persistence & idempotency

`timeline.json` (kind `timeline`) via the existing `ArtifactRegistry`,
atomic write, reload + re-validation before success, and a
content-identical idempotency guard (identical inputs reuse the existing
artifact ref — no duplicates).

## Hermes integration boundary

```python
result = run_timeline_stage(
    scene_manifest=…,        # loaded from the scene_manifest artifact
    asset_manifest=…,        # from the asset_manifest artifact
    narration_manifest=narration_manifest,  # from the narration artifact
    run_state=run_state,
    artifact_registry=registry,
)
# TimelineResult: ok / manifest / artifact / run_id / production_id / error
```

P4 consumes resolved artifacts only. Renderer selection is a later
orchestration decision (future Hermes).

## Explicit non-scope

No FFmpeg, Remotion, OTIO, OpenMontage, MoviePy, editors, keyframes,
transitions, captions (no upstream contract exists — none faked), audio
processing, publishing, analytics, MCP, VPS, databases, queues, or
orchestration loop. P4 proves the temporal composition boundary.