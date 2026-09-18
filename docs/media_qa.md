# P5.5 — Technical Media QA

## What it is

`src/ayce/media_qa.py` is the sixth bounded, Hermes-compatible capability:
it consumes the verified P5 production render **artifact reference** plus
the upstream Timeline and Narration manifests and independently inspects
the actual output file, producing a structured, machine-actionable QA
report with a deterministic verdict.

```text
Rendered Production MP4 (registered rendered_video artifact ref)
        + Timeline Manifest + Narration Manifest
        ↓ run_media_qa_stage(...)  (Hermes-callable boundary)
independent ffprobe + lightweight deterministic frame sampling
        ↓
qa_report.json  (ArtifactKind.QA_REPORT)
        ↓
PASS / FAIL
```

> **This is technical media QA, not complete production QA.** It verifies
> that the render is a readable, technically valid MP4 that matches the
> declared timeline. It does NOT evaluate creative quality, factual
> correctness, captions, rights, audio loudness, visual aesthetics, or
> narrative quality — those belong to future, independently-scoped QA
> layers.

## Inputs (registry-resolved, never arbitrary paths)

- `rendered_artifact`: an `ArtifactRef` of kind `rendered_video`
  registered in this run's registry (validated: kind, run identity,
  registry membership, path containment).
- `timeline_manifest` (`TimelineManifest`, or raw dict → validated).
- `narration_manifest` (`NarrationManifest`, or raw dict → validated).
- `run_state`, `artifact_registry`, `config`.

Production identity must agree across the timeline, the narration
manifest, and (when present) the render artifact's metadata.

## Checks (the complete P5.5 scope)

| Category      | Check ids | Verifies |
|---------------|-----------|----------|
| `file`        | `file.path_inside_run_dir`, `file.exists`, `file.non_empty` | referenced path resolves inside the run directory, exists, non-empty |
| `container`   | `container.ffprobe_parses`, `container.duration_positive` | independent ffprobe parses the container; duration exists and > 0 |
| `video`       | `video.stream_present`, `video.dimensions`, `video.stream_duration` | video stream + codec, width/height > 0, valid duration |
| `audio`       | `audio.stream_present`, `audio.stream_duration` | audio stream + codec, duration valid where reported |
| `duration`    | `duration.matches_timeline` | rendered duration vs `TimelineManifest.total_duration_seconds` |
| `scene_coverage` | `scene_coverage.<scene_id>` (one per scene) | lightweight frame-color evidence per scene interval |

No creative categories (visual, audio_quality, caption, narrative,
rights, publishing) exist yet — future QA layers add them independently.

## Duration tolerance (reused, not reinvented)

Exactly the P5 production-render policy: `±0.5 s + 2%` of
`TimelineManifest.total_duration_seconds` (`DURATION_TOLERANCE_ABS_SECONDS`
+ `DURATION_TOLERANCE_RELATIVE` from `src/ayce/render.py`). The check
records `expected` (timeline total ± tolerance) and `actual` (rendered
duration + absolute delta) for auditability.

## Scene coverage (lightweight, deterministic — read the limitation)

Reuses the frame-color-sampling approach proven in the P5 tests: one
frame is decoded at each scene's midpoint as raw RGB (scaled to 64×64,
center pixel; per-channel tolerance 24, the allowance proven in P5).
- Still-image sources: the sampled color must match the scene's resolved
  still image (`basis: still_image_source`).
- Narration-only scenes must show the documented black filler
  (`basis: no_visual_black_filler`).
- Video sources are sampled for evidence but **not color-compared**
  (solid-color equivalence is not guaranteed for general video content).

This check catches obvious missing/incorrect scene composition. It is
**NOT** frame-accurate scene-boundary detection, computer vision, ML,
or perceptual similarity, and must not be presented as such.

## Verdict semantics

`verdict` is `FAIL` iff any check is `FAIL`; otherwise `PASS`. There is
no scoring, no percentage quality. Future Hermes/repair logic consumes
the structured report; P5.5 itself has **no QA → repair loop** (no
diagnosis, no re-render, no rerun loop).

## Failure semantics (explicit)

- **QA FAIL** (readable media failing a technical gate): the report is
  persisted and registered with `verdict=FAIL`; the stage state is
  `succeeded` — producing truthful evidence IS the stage's job.
- **QA EXECUTION ERROR** (the stage cannot do its job — invalid
  artifact reference, wrong kind, malformed manifest, production-id
  mismatch, missing ffprobe/ffmpeg, persistence failure): stage state
  `failed`, NO artifact published, `result.ok=False` with `error`.

## Idempotency / immutability

The report is bound to the rendered artifact's **current content
identity** (sha256 of the actual bytes at QA time). A rerun against the
same valid render reuses the existing QA artifact (`reused=True`, no
duplicate registration). A stale/corrupt report file, or a changed
render (different sha256), is re-executed and registered under a NEW
artifact identity — an existing `qa_report.json` is never accepted as
valid merely because the file exists. Stage label `media_qa` is
stage-specific (never reuses render stage labels).

## Execution example

```python
from ayce.media_qa import run_media_qa_stage

result = run_media_qa_stage(
    rendered_artifact=render_ref,      # registered rendered_video ArtifactRef
    timeline_manifest=timeline,        # from the timeline artifact
    narration_manifest=narration,      # from the narration artifact
    run_state=run_state,
    artifact_registry=registry,
    config=config,
)
result.ok        # True iff QA executed and produced a registered report
result.verdict   # "PASS" | "FAIL" — deterministic technical verdict
result.report    # QAReport (checks, scene coverage, summary)
```

Note `result.ok=True, verdict="FAIL"` means the QA stage executed and
truthfully failed the media; `result.ok=False` means QA itself crashed
and produced no artifact.

## What this stage does NOT do

No repair loop, no orchestration of other stages, no provider
selection, no publishing, no autonomous planning, no computer vision,
no LUFS/clipping/silence analysis, no frame-quality analysis, no OCR,
no ML. It consumes verified structured inputs and produces evidence.
