# P4.5 — FFmpeg Environment + Minimal Renderer (Smoke)

## What it is

`src/ayce/render.py` is the fifth bounded, Hermes-compatible capability —
and the first that produces **real media**: it renders ONE timeline scene
into an ffprobe-verified MP4 via the FFmpeg CLI.

```text
Timeline Manifest (renderer-neutral — untouched)
      ↓ Renderer.health() gate (truthful AdapterHealth)
      ↓ Renderer.render(scene, run_dir)      ← ONE scene (smoke scope)
FFmpeg CLI (subprocess, argument list, no shell)
      → render/scene-XXX.mp4 (run-local output)
      ↓ ffprobe validation (streams, duration, dimensions — verified)
RunState lifecycle → ArtifactRegistry (kind rendered_video) → reload
      ↓
RenderResult
```

**Separation preserved:** the Scene/Asset/Narration/Timeline manifests are
never mutated. `Timeline = WHAT and WHEN`, `Renderer = renders it`,
`Hermes = orchestrates (selects renderer, decides when)`.

## FFmpeg environment

- Requirement: `ffmpeg` AND `ffprobe` CLI tools.
- Detection: optional `AYCE_FFMPEG_PATH` / `AYCE_FFPROBE_PATH` config,
  falling back to PATH lookup. No machine paths are hardcoded in source.
- Local dev install (this machine): FFmpeg 9.0.1 essentials from the
  gyan.dev release (the distribution officially linked from ffmpeg.org),
  installed under the user's local AppData and added to the user PATH.
  Chocolatey was present but blocked by non-admin permissions; no other
  install method was viable.
- The project health check (`ayce health`) truthfully verifies BOTH
  executables with version queries.

## Renderer adapter boundary

`Renderer(Adapter)` extends the P0 `Adapter` ABC with one bounded
operation: `render(timeline, scene_id, *, run_dir) -> RenderedOutput`
— exactly ONE timeline scene. The stage depends on this interface;
`FFmpegRenderer` is one implementation. Registry-compatible (tested).

## FFmpeg renderer (smoke scope)

For the target scene: resolves run-relative sources (`assets/…`,
`audio/…`), then runs (argument list, no shell):

```text
ffmpeg -y -loop 1 -framerate 25 -i <visual>
       [-i <narration.wav>] -map 0:v -c:v libx264 -preset ultrafast
       -pix_fmt yuv420p [-map 1:a -c:a aac -b:a 64k]
       -t <scene.duration> render/<scene_id>.mp4
```

Static visual for the full scene duration; narration audio is muxed
as-is (shorter narration = unambiguously visual-only time, per the P4
policy — no stretching/trimming). `FFmpeg exit 0` alone is NOT success:
ffprobe must parse the output and verify a video stream, valid
dimensions, and positive duration, or the render fails. Output metadata
is only what ffprobe verified.

## Failure behavior (fail-fast)

Unhealthy renderer → stage fails before rendering. Missing run-local
source → failure. FFmpeg non-zero exit → failed (stderr tail captured
for diagnosis). ffprobe validation failure → failed even when FFmpeg
exited zero. No placeholder videos, no false-success artifacts.

## Smoke renderer vs production renderer

This renderer: ONE scene, static visual, no effects, tiny output. A
production renderer would render ALL scenes into one video, add
transitions/captions/graphics, and handle audio policy (e.g., silence
filling, ducking). Those are future capabilities; P4.5 only proves the
render boundary and produces the first real media artifact.

## P5 — multi-scene production render

`FFmpegRenderer.render_production(timeline, *, run_dir)` renders the
COMPLETE timeline into ONE production MP4:
`render/{production_id}.mp4` (kind `rendered_video`).

- Per-scene clips are encoded (same documented composition policies as
  the smoke render: looped still / stream-looped video / black filler for
  narration-only scenes, narration muxed as-is) into `render/tmp/`,
  then concatenated with the FFmpeg concat demuxer (`-c copy`) in
  canonical timeline order.
- Intermediates are scratch: removed after successful completion,
  preserved on failure for diagnosis. Never written into source fixtures,
  never committed.
- Production duration is validated against the timeline total within a
  documented tolerance (`±0.5 s + 2%`) — proving all scene intervals were
  incorporated. Additional lightweight coverage proof: per-scene fixture
  visuals use distinct solid colors, so a frame sampled at each scene
  midpoint must match that scene's color (tests sample raw RGB via
  ffmpeg — no computer vision).
- Idempotency (within a run): an existing production artifact for the
  same output path is ffprobe re-validated — a valid file is reused; a
  stale/corrupt file triggers a fresh render (never accepted as success).
- Stage: `run_production_render_stage(timeline, run_state, registry,
  renderer)` → `render/{production_id}.mp4`, state
  `production_render`.