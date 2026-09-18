# P1-B — Script → Scene Manifest Stage

## What it is

`src/ayce/script_to_scene.py` is the first **bounded production capability**
of the engine: a deterministic stage adapter that turns a structured script
input into a validated, persisted Scene Contract artifact.

```text
Structured Script (ScriptInput)
      ↓ validate (structural)
      ↓ build_manifest() — pure deterministic transformation
      ↓ P1-A contract validation (ProductionManifest)   ← single validation authority
      ↓ RunState: pending → running → succeeded/failed
      ↓ persist_manifest() + ArtifactRegistry
      → scene_manifest.json   (ArtifactKind.SCENE_MANIFEST)
      → reload + verify       → ScriptToSceneResult
```

**P1-B is a bounded stage, not Hermes.** Future Hermes (master director)
invokes it as one callable capability:

```python
result = run_script_to_scene_stage(
    script_input=...,          # ScriptInput | dict | path to JSON
    run_state=run_state,       # existing P0 RunState
    artifact_registry=registry # existing P0 registry for this run
)
result.ok                        # trustworthy success flag
result.manifest / result.artifact
result.run_id / result.production_id
result.error                     # f"{type}: {message}" on failure
```

The stage contains no planning, routing, retry policy, or coordination —
that remains Hermes's job.

## Input contract (`ScriptInput`)

```json
{
  "production_id": "job-...",       // stable production identity
  "title": "Working title",
  "scenes": [                       // 1..N (3–5 for the MVP)
    {
      "scene_id": null,             // optional → derived "scene-001"...
      "sequence": null,             // optional → array position (1..N)
      "duration_seconds": null,     // optional → deterministic rule below
      "narration_text": "...",      // required
      "visual_description": "…",    // required
      "asset_requirement": null     // optional P1-A AssetRequirement
    }
  ]
}
```

Input DTOs are **structural only** (required fields, types, unknown fields
rejected). All value semantics are owned by the P1-A contract — invalid
values flow through the stage and fail it via contract rejection, so there
is exactly one validation authority.

## Deterministic behavior

- **Durations**: if the input supplies `duration_seconds` it is preserved;
  otherwise `max(2.0 s, words / 2.5)` (~150 wpm). A documented, replaceable
  MVP rule — not production-grade narration timing.
- **Scene IDs**: `scene-001`, `scene-002`, … by array position (or the
  explicit `scene_id` from the input).
- **No randomness, no timestamps, no network**: identical input always
  yields identical `scene_manifest.json` bytes (verified by tests).

## State lifecycle (existing P0 `RunState`)

```text
pending → running → succeeded          happy path
pending → running → failed             any stage-critical failure
failed  → running → ...                retry allowed
succeeded is terminal — rerunning the same RunState is rejected
```

No false success: input validation, contract validation, persistence,
artifact registration, and reload verification must all succeed before
`succeeded` is recorded. Any exception marks the stage `failed` with the
error stored in the run state and returned in `ScriptToSceneResult.error`.

## Artifact behavior

Persists via the existing `persist_manifest()` / `ArtifactRegistry` as kind
`scene_manifest` (`scene_manifest.json` inside the run directory), then
reloads and re-validates it before claiming success. Rerunning with
identical input against the same run directory **reuses** the existing
artifact (no duplicates); a changed manifest legitimately registers a new
artifact ref.

## Intentionally NOT implemented

No LLM/AI generation, no natural-language script understanding, no asset
resolution, TTS, captions, renderer, FFmpeg, publishing, analytics,
orchestration loop, Hermes itself, MCP, queues, or databases. This is one
bounded capability that future Hermes will orchestrate.
