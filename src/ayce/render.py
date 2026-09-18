"""P4.5 — FFmpeg renderer smoke stage (crosses into real media output).

A bounded, Hermes-compatible production capability:

    persisted Timeline Manifest (P4)
        ↓ renderer.health() gate (truthful AdapterHealth)
        ↓ renderer.render(scene, run_dir)  — ONE scene (smoke scope)
    FFmpeg CLI (subprocess, argument list, no shell)
        → render/scene-XXX.mp4 (run-local output)
        ↓ ffprobe validation (truthful metadata: streams/duration/dimensions)
        → RunState lifecycle → ArtifactRegistry (kind rendered_video)
        → reload + verify → RenderResult

Boundary rules:

- The Timeline Manifest remains renderer-neutral: the renderer CONSUMES
  it and never injects renderer instructions back into any upstream
  manifest.
- FFmpeg is an implementation detail of THIS adapter only. Executables
  are resolved via optional AYCE_FFMPEG_PATH / AYCE_FFPROBE_PATH config,
  falling back to PATH lookup — never hardcoded machine paths.
- Subprocess execution uses explicit argument lists (``subprocess.run([...])``),
  never ``shell=True`` and never string-concatenated commands.
- The smoke renderer renders ONE scene (static visual + narration audio,
  H.264/AAC MP4). Multi-scene production rendering, captions, graphics,
  transitions, and motion are future capabilities.
- Truthful media claims only: output metadata comes from ffprobe parsing.
  A render that exits zero but fails ffprobe validation is a FAILURE.
- Idempotency: codec output is not byte-deterministic, so the guard is
  scene-based within a run — an existing rendered_video artifact for the
  same scene (with an existing output file) is reused, not re-rendered.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from abc import abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from .adapters import Adapter, AdapterHealth
from .artifacts import ArtifactKind, ArtifactRef, ArtifactRegistry
from .config import Config
from .logging import StructuredLogger
from .state import RunState
from .timeline import TimelineElementKind, TimelineManifest

__all__ = [
    "STAGE_NAME",
    "ARTIFACT_STAGE",
    "RENDER_DIRNAME",
    "PRODUCTION_STAGE_NAME",
    "PRODUCTION_ARTIFACT_STAGE",
    "RenderError",
    "RenderedOutput",
    "Renderer",
    "FFmpegRenderer",
    "RenderResult",
    "run_render_stage",
    "run_production_render_stage",
    "resolve_media_tool",
]

#: RunState stage label for the P4.5 single-scene smoke capability.
STAGE_NAME = "render"
#: ArtifactRegistry stage label identifying the single-scene producing stage.
ARTIFACT_STAGE = "render"
#: P5 production render: RunState stage + registry labels.
PRODUCTION_STAGE_NAME = "production_render"
PRODUCTION_ARTIFACT_STAGE = "production_render"
#: Run-relative directory holding rendered video files.
RENDER_DIRNAME = "render"

#: Cap for smoke-render encodes; not a production-quality setting.
_FFMPEG_TIMEOUT_SECONDS = 120
_PROBE_TIMEOUT_SECONDS = 30
#: Smoke-render output geometry (also used for narration-only black filler).
SMOKE_WIDTH = 64
SMOKE_HEIGHT = 64
#: Production-duration validation tolerance: absolute seconds plus a small
#: relative allowance for container/codec timestamp rounding.
DURATION_TOLERANCE_ABS_SECONDS = 0.5
DURATION_TOLERANCE_RELATIVE = 0.02


class RenderError(RuntimeError):
    """Raised when rendering or output validation fails."""


class RenderedOutput(BaseModel):
    """ffprobe-verified facts about one rendered scene video.

    Only verified metadata is carried: anything ffprobe could not
    determine stays ``None``. Never invented.
    """

    model_config = ConfigDict(extra="forbid")

    #: Run-relative path of the rendered MP4.
    path: str
    #: sha256 of the rendered file bytes (integrity record).
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: ffprobe container duration in seconds (verified).
    duration_seconds: float | None = None
    #: Video stream width (verified).
    width: int | None = None
    #: Video stream height (verified).
    height: int | None = None
    #: Whether an audio stream is present (verified).
    has_audio: bool = False


def resolve_media_tool(configured: str | None, name: str) -> str | None:
    """Resolve an external media tool (ffmpeg/ffprobe) executable.

    The verified P4.5 resolution policy, shared with the P5.5 media QA
    stage: a configured ``AYCE_<TOOL>_PATH`` value wins; otherwise a PATH
    lookup. No machine paths are hardcoded in source.
    """
    if configured:
        return configured
    return shutil.which(name)

# ---- renderer adapter boundary (extends the P0 Adapter ABC) ------------------


def _run_tool(executable: str, args: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    """Run an external tool with an explicit argument list (no shell)."""
    return subprocess.run(
        [executable, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class Renderer(Adapter):
    """Provider-agnostic capability boundary for rendering.

    Extends the P0 ``Adapter`` ABC. The stage depends on THIS interface;
    which renderer to use (FFmpeg or a future engine) is an orchestration
    decision (future Hermes).
    """

    @abstractmethod
    def render(
        self,
        timeline: TimelineManifest,
        scene_id: str,
        *,
        run_dir: Path,
    ) -> RenderedOutput:
        """Render ONE timeline scene into a verified media output."""

    @abstractmethod
    def render_production(
        self,
        timeline: TimelineManifest,
        *,
        run_dir: Path,
    ) -> RenderedOutput:
        """Render the COMPLETE timeline into one production video.

        All scenes in canonical timeline order, composition per the
        documented temporal policies. The output is one production MP4
        validated with ffprobe.
        """


class FFmpegRenderer(Renderer):
    """Smoke-scope renderer: one timeline scene → H.264/AAC MP4 via FFmpeg.

    Static visual + narration audio only — no motion, transitions, or
    captions. The visual and audio sources are resolved run-relative from
    the timeline (the renderer never calls upstream providers).
    """

    name: ClassVar[str] = "ffmpeg-smoke"

    def __init__(self, config: Config) -> None:
        super().__init__(config)

    def _resolve_executable(self, configured: str | None, name: str) -> str | None:
        """Configured path wins; otherwise PATH lookup (shared policy)."""
        return resolve_media_tool(configured, name)

    def health(self) -> AdapterHealth:
        """Truthful: healthy only if ffmpeg AND ffprobe actually execute."""
        resolved = {
            "ffmpeg": self._resolve_executable(self.config.ffmpeg_path, "ffmpeg"),
            "ffprobe": self._resolve_executable(self.config.ffprobe_path, "ffprobe"),
        }
        missing = [
            name for name, exe in resolved.items()
            if exe is None or not Path(exe).is_file()
        ]
        if missing:
            return AdapterHealth(
                name=self.name,
                available=False,
                detail=f"executable(s) not found: {', '.join(missing)}",
            )
        try:
            for exe in resolved.values():
                proc = _run_tool(exe, ["-version"], timeout=15)
                if proc.returncode != 0:
                    return AdapterHealth(
                        name=self.name,
                        available=False,
                        detail=f"{Path(exe).name} exited {proc.returncode}",
                    )
        except (OSError, subprocess.SubprocessError) as exc:
            return AdapterHealth(name=self.name, available=False, detail=f"invocation failed: {exc}")
        return AdapterHealth(name=self.name, available=True, detail="ffmpeg and ffprobe callable")

    def _render_scene_clip(
        self,
        ffmpeg: str,
        timeline_scene,
        run_dir: Path,
        output_path: Path,
    ) -> None:
        """Encode ONE scene clip.

        Composition policy (documented MVP):
        - visual image (non-video) → looped still for the scene duration;
        - visual video (``.mp4``) → stream-looped and trimmed;
        - NO visual element → black filler at the smoke geometry (P4
          timeline policy: narration-only scenes occupy their interval);
        - narration audio is muxed as-is (underflow → remaining time is
          unambiguously visual-only).
        """
        visual = next((e for e in timeline_scene.elements if e.kind is TimelineElementKind.VISUAL), None)
        narrations = [e for e in timeline_scene.elements if e.kind is TimelineElementKind.NARRATION]

        for element in timeline_scene.elements:
            if not (run_dir / element.source).is_file():
                raise RenderError(
                    f"resolved source missing for scene {timeline_scene.scene_id!r}: "
                    f"{element.source!r} does not exist in the run directory"
                )

        args: list[str] = ["-y"]
        if visual is not None:
            source = run_dir / visual.source
            if source.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}:
                args += ["-stream_loop", "-1", "-i", str(source)]
            else:
                args += ["-loop", "1", "-framerate", "25", "-i", str(source)]
        else:
            # documented MVP policy: a timeline scene without a resolved
            # visual renders as black filler for its interval
            args += [
                "-f", "lavfi",
                "-i",
                f"color=c=black:s={SMOKE_WIDTH}x{SMOKE_HEIGHT}:r=25"
                f":d={timeline_scene.duration_seconds}",
            ]
        for audio_element in narrations:
            args += ["-i", str(run_dir / audio_element.source)]
        args += [
            "-map", "0:v",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        ]
        if narrations:
            args += ["-map", "1:a", "-c:a", "aac", "-b:a", "64k"]
        args += ["-t", str(timeline_scene.duration_seconds), str(output_path)]

        proc = _run_tool(ffmpeg, args, timeout=_FFMPEG_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()
            detail = tail[-1] if tail else "no output"
            raise RenderError(
                f"ffmpeg exited {proc.returncode} for scene {timeline_scene.scene_id!r}: {detail}"
            )
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RenderError(
                f"ffmpeg reported success but the output is missing or empty: {output_path}"
            )

    def render(
        self,
        timeline: TimelineManifest,
        scene_id: str,
        *,
        run_dir: Path,
    ) -> RenderedOutput:
        """Render one timeline scene: static visual + narration → MP4.

        Sources are resolved run-relative from the timeline. FFmpeg runs
        with an explicit argument list; the output is validated with
        ffprobe before any success is reported.
        """
        timeline_scene = next((s for s in timeline.scenes if s.scene_id == scene_id), None)
        if timeline_scene is None:
            raise RenderError(f"scene {scene_id!r} not found in the timeline")
        if not any(e.kind is TimelineElementKind.VISUAL for e in timeline_scene.elements):
            raise RenderError(
                f"scene {scene_id!r} has no visual element — the single-scene renderer "
                f"requires a resolved visual (use render_production for narration-only scenes)"
            )

        ffmpeg = self._resolve_executable(self.config.ffmpeg_path, "ffmpeg")
        ffprobe = self._resolve_executable(self.config.ffprobe_path, "ffprobe")
        if ffmpeg is None or ffprobe is None:
            raise RenderError("ffmpeg/ffprobe executables not found")

        output_dir = Path(run_dir) / RENDER_DIRNAME
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f"{scene_id}.mp4"
        self._render_scene_clip(ffmpeg, timeline_scene, Path(run_dir), output)
        return self._validate_output(ffprobe, output)

    def render_production(
        self,
        timeline: TimelineManifest,
        *,
        run_dir: Path,
    ) -> RenderedOutput:
        """Render the COMPLETE timeline: per-scene clips → concat → one MP4.

        All scenes render in canonical timeline order. Intermediates live
        under ``render/tmp/`` and are removed after successful completion
        (preserved on failure for diagnosis). The final production video
        must match the timeline's declared total duration within the
        documented tolerance, verified with ffprobe.
        """
        ffmpeg = self._resolve_executable(self.config.ffmpeg_path, "ffmpeg")
        ffprobe = self._resolve_executable(self.config.ffprobe_path, "ffprobe")
        if ffmpeg is None or ffprobe is None:
            raise RenderError("ffmpeg/ffprobe executables not found")

        run_dir = Path(run_dir)
        tmp_dir = run_dir / RENDER_DIRNAME / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        clips: list[Path] = []
        for scene in timeline.scenes:
            clip_path = tmp_dir / f"{scene.scene_id}.mp4"
            self._render_scene_clip(ffmpeg, scene, run_dir, clip_path)
            clips.append(clip_path)

        concat_list = tmp_dir / "concat.txt"
        concat_list.write_text(
            "".join(f"file '{clip.name}'\n" for clip in clips), encoding="utf-8"
        )
        output = run_dir / RENDER_DIRNAME / f"{timeline.production_id}.mp4"
        proc = _run_tool(
            ffmpeg,
            ["-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(output)],
            timeout=_FFMPEG_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()
            detail = tail[-1] if tail else "no output"
            raise RenderError(f"ffmpeg concat exited {proc.returncode}: {detail}")
        if not output.is_file() or output.stat().st_size == 0:
            raise RenderError(f"concat reported success but the output is missing or empty: {output}")

        rendered = self._validate_output(ffprobe, output)
        # production duration must match the declared timeline total
        expected = timeline.total_duration_seconds
        tolerance = DURATION_TOLERANCE_ABS_SECONDS + DURATION_TOLERANCE_RELATIVE * expected
        if abs((rendered.duration_seconds or 0.0) - expected) > tolerance:
            raise RenderError(
                f"production duration {rendered.duration_seconds}s deviates from the "
                f"timeline total {expected}s beyond the documented tolerance "
                f"(±{tolerance:.2f}s) — scene coverage cannot be confirmed"
            )
        # intermediates are scratch; removed only after full success
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return rendered

    def validate_output(self, output_path: Path) -> RenderedOutput:
        """ffprobe-validate an existing rendered file (idempotency staleness check)."""
        ffprobe = self._resolve_executable(self.config.ffprobe_path, "ffprobe")
        if ffprobe is None:
            raise RenderError("ffprobe executables not found")
        return self._validate_output(ffprobe, Path(output_path))

    def _validate_output(self, ffprobe: str, output: Path) -> RenderedOutput:
        """Verify the rendered file with ffprobe; a validation failure here
        is a render failure — even when ffmpeg itself exited zero."""
        proc = _run_tool(
            ffprobe,
            ["-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(output)],
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            raise RenderError(f"ffprobe validation failed (exit {proc.returncode})")
        try:
            probe = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RenderError(f"ffprobe produced unparseable output: {exc}") from exc

        streams = probe.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video is None:
            raise RenderError("ffprobe validation failed: no video stream in output")
        width, height = video.get("width"), video.get("height")
        if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
            raise RenderError(f"ffprobe validation failed: invalid dimensions {width}x{height}")
        duration_raw = probe.get("format", {}).get("duration")
        try:
            duration = float(duration_raw) if duration_raw is not None else None
        except (TypeError, ValueError):
            duration = None
        if duration is None or duration <= 0:
            raise RenderError("ffprobe validation failed: duration missing or non-positive")
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        return RenderedOutput(
            path=f"{RENDER_DIRNAME}/{output.name}",
            sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
            duration_seconds=round(duration, 3),
            width=width,
            height=height,
            has_audio=has_audio,
        )

# ---- persistence / artifact registration -------------------------------------


def _find_existing_render(
    registry: ArtifactRegistry, run_dir: Path, output_path: str, *, artifact_stage: str
) -> ArtifactRef | None:
    """Stage-based idempotency guard: codec output is not byte-deterministic,
    so identity is (stage, output path) + an existing file — not a content hash."""
    for ref in registry.for_stage(artifact_stage):
        if ref.kind is not ArtifactKind.RENDERED_VIDEO:
            continue
        if ref.path == output_path and (Path(run_dir) / ref.path).is_file():
            return ref
    return None


# ---- stage execution (the Hermes-callable boundary) --------------------------


@dataclass(frozen=True)
class RenderResult:
    """Outcome of one render execution, for future Hermes.

    ``ok`` is trustworthy: ``True`` only after FFmpeg exited zero, the
    output passed ffprobe validation, and the artifact was registered and
    the registry reloaded successfully.
    """

    ok: bool
    stage: str
    run_id: str
    production_id: str | None
    renderer: str | None
    output: RenderedOutput | None
    artifact: ArtifactRef | None
    error: str | None = None


def run_render_stage(
    timeline_manifest: TimelineManifest,
    run_state: RunState,
    artifact_registry: ArtifactRegistry,
    renderer: Renderer,
    *,
    scene_id: str | None = None,
    logger: StructuredLogger | None = None,
) -> RenderResult:
    """Execute the smoke-scope render stage for ONE timeline scene.

    This is the single callable a future orchestrator (Hermes) uses:

        result = run_render_stage(
            timeline_manifest=timeline,      # from the timeline artifact
            run_state=run_state,
            artifact_registry=registry,
            renderer=renderer,               # selected/injected BY the orchestrator
            scene_id=scene_id,               # None → first timeline scene
        )

    Lifecycle (existing P0 state machine):

        pending → running → succeeded          (happy path)
        pending → running → failed             (any stage-critical failure)
        failed  → running → ...                (retry is permitted)

    Policy (explicit, deterministic):
    - Renderer health is checked first; an unhealthy renderer fails the
      stage before any render attempt.
    - The scene's sources must exist in the run directory (fail-fast).
    - FFmpeg exit zero is NOT sufficient: ffprobe must validate the
      output (video stream, dimensions, duration) or the stage fails.
    - The output is registered as a ``rendered_video`` artifact.
    """
    log = logger if logger is not None else StructuredLogger(level="INFO")
    log = log.bind(stage=STAGE_NAME, run_id=run_state.run_id, job_id=run_state.job_id)
    result = RenderResult(
        ok=False,
        stage=STAGE_NAME,
        run_id=run_state.run_id,
        production_id=None,
        renderer=None,
        output=None,
        artifact=None,
        error=None,
    )

    def fail(exc: BaseException) -> RenderResult:
        error = f"{type(exc).__name__}: {exc}"
        try:
            run_state.set_stage(STAGE_NAME, "failed", error=error)
        except Exception as mark_failure_error:
            # state machine corruption must not mask the original cause
            log.error("render_stage_failed", error=exc, state_error=str(mark_failure_error))
        else:
            log.error("render_stage_failed", error=exc)
        return replace(result, error=error)

    try:
        run_state.set_stage(STAGE_NAME, "running")
        result = replace(result, renderer=renderer.name, production_id=timeline_manifest.production_id)
        log = log.bind(renderer=renderer.name, production_id=timeline_manifest.production_id)
        log.info("render_stage_started")

        # 1) renderer must be actually usable (truthful AdapterHealth)
        health = renderer.health()
        log.info("renderer_health_checked", available=health.available, detail=health.detail)
        if not health.available:
            raise RenderError(f"renderer {renderer.name!r} is unhealthy: {health.detail}")

        # 2) resolve the target scene (None → first timeline scene)
        target_scene_id = scene_id or timeline_manifest.scenes[0].scene_id
        log = log.bind(scene_id=target_scene_id)

        # 3) render one scene (renderer owns FFmpeg details + ffprobe validation)
        output = renderer.render(timeline_manifest, target_scene_id, run_dir=artifact_registry.run_dir)
        log.info(
            "ffmpeg_process_completed",
            duration_seconds=output.duration_seconds,
            width=output.width,
            height=output.height,
            has_audio=output.has_audio,
        )
        log.info("render_output_validated", sha256=output.sha256)

        # 4) register through the existing artifact mechanism (scene-based guard)
        run_dir = artifact_registry.run_dir
        # 4) register through the existing artifact mechanism (scene-based guard)
        run_dir = artifact_registry.run_dir
        existing = _find_existing_render(
            artifact_registry, run_dir, output.path, artifact_stage=ARTIFACT_STAGE
        )
        if existing is not None:
            ref = existing
            log.info("render_artifact_persisted", artifact_id=ref.artifact_id, reused=True)
        else:
            ref = artifact_registry.register(
                ARTIFACT_STAGE,
                ArtifactKind.RENDERED_VIDEO,
                output.path,
                {
                    "scene_id": target_scene_id,
                    "renderer": renderer.name,
                    "duration_seconds": output.duration_seconds,
                    "width": output.width,
                    "height": output.height,
                    "sha256": output.sha256,
                },
            )
            log.info("render_artifact_persisted", artifact_id=ref.artifact_id)

        # 5) reload + verify the registry entry before claiming success
        reloaded_registry = ArtifactRegistry.load(run_dir, run_state.run_id)
        reloaded_registry.require(ref.artifact_id)
        if not (Path(run_dir) / ref.path).is_file():
            raise RenderError("rendered output is missing after artifact registration")

        # 6) success
        run_state.set_stage(STAGE_NAME, "succeeded")
        log.info("render_stage_succeeded", artifact_id=ref.artifact_id, output=output.path)
        return replace(result, ok=True, output=output, artifact=ref)
    except Exception as exc:  # noqa: BLE001 — the stage boundary must never fake success
        return fail(exc)


# ---- P5 production render stage (the Hermes-callable boundary) ----------------


def run_production_render_stage(
    timeline_manifest: TimelineManifest,
    run_state: RunState,
    artifact_registry: ArtifactRegistry,
    renderer: Renderer,
    *,
    logger: StructuredLogger | None = None,
) -> RenderResult:
    """Execute the P5 multi-scene production render for the WHOLE timeline.

    This is the single callable a future orchestrator (Hermes) uses:

        result = run_production_render_stage(
            timeline_manifest=timeline,   # from the timeline artifact
            run_state=run_state,
            artifact_registry=registry,
            renderer=renderer,            # selected/injected BY the orchestrator
        )

    Renders ALL timeline scenes in canonical order into ONE production
    MP4 (``render/{production_id}.mp4``), independently ffprobe-validated
    (video/audio streams, duration within the documented tolerance of the
    declared timeline total), registered as a ``rendered_video`` artifact,
    and reload-verified.

    Idempotency: if a production artifact for this run/stage already
    exists, it is ffprobe re-validated — a valid file is reused (no
    duplicate registration); a stale/corrupt file triggers a re-render.
    Codec output is not byte-deterministic, so identity is the production
    output path + a valid existing file, never a content hash.
    """
    log = logger if logger is not None else StructuredLogger(level="INFO")
    log = log.bind(
        stage=PRODUCTION_STAGE_NAME, run_id=run_state.run_id, job_id=run_state.job_id
    )
    result = RenderResult(
        ok=False,
        stage=PRODUCTION_STAGE_NAME,
        run_id=run_state.run_id,
        production_id=None,
        renderer=None,
        output=None,
        artifact=None,
        error=None,
    )

    def fail(exc: BaseException) -> RenderResult:
        error = f"{type(exc).__name__}: {exc}"
        try:
            run_state.set_stage(PRODUCTION_STAGE_NAME, "failed", error=error)
        except Exception as mark_failure_error:
            # state machine corruption must not mask the original cause
            log.error(
                "production_render_stage_failed", error=exc, state_error=str(mark_failure_error)
            )
        else:
            log.error("production_render_stage_failed", error=exc)
        return replace(result, error=error)

    try:
        run_state.set_stage(PRODUCTION_STAGE_NAME, "running")
        result = replace(result, renderer=renderer.name)
        log = log.bind(
            renderer=renderer.name, production_id=timeline_manifest.production_id
        )
        log.info("render_stage_started")

        # 1) renderer must be actually usable (truthful AdapterHealth)
        health = renderer.health()
        log.info("renderer_health_checked", available=health.available, detail=health.detail)
        if not health.available:
            raise RenderError(f"renderer {renderer.name!r} is unhealthy: {health.detail}")

        # 2) validate the timeline input when raw data is passed
        if not isinstance(timeline_manifest, TimelineManifest):
            timeline_manifest = TimelineManifest.model_validate(timeline_manifest)
        result = replace(result, production_id=timeline_manifest.production_id)

        # 3) render the complete timeline (renderer owns FFmpeg details)
        output = renderer.render_production(timeline_manifest, run_dir=artifact_registry.run_dir)
        log.info(
            "ffmpeg_process_completed",
            duration_seconds=output.duration_seconds,
            width=output.width,
            height=output.height,
            has_audio=output.has_audio,
        )
        log.info("render_output_validated", sha256=output.sha256)

        # 4) register through the existing artifact mechanism
        #    (production-path idempotency + stale-output ffprobe re-validation)
        run_dir = artifact_registry.run_dir
        # 4) register through the existing artifact mechanism
        #    (production-path idempotency + stale-output ffprobe re-validation)
        run_dir = artifact_registry.run_dir
        existing = _find_existing_render(
            artifact_registry, run_dir, output.path, artifact_stage=PRODUCTION_ARTIFACT_STAGE
        )
        ref = None
        if existing is not None:
            try:
                renderer.validate_output(Path(run_dir) / output.path)
                ref = existing
                log.info("render_artifact_persisted", artifact_id=ref.artifact_id, reused=True)
            except RenderError:
                # stale/corrupt output must not be accepted as success — re-render
                log.info("render_artifact_stale_detected", path=output.path)
        if ref is None:
            ref = artifact_registry.register(
                PRODUCTION_ARTIFACT_STAGE,
                ArtifactKind.RENDERED_VIDEO,
                output.path,
                {
                    "production_id": timeline_manifest.production_id,
                    "renderer": renderer.name,
                    "scene_count": len(timeline_manifest.scenes),
                    "duration_seconds": output.duration_seconds,
                    "width": output.width,
                    "height": output.height,
                    "sha256": output.sha256,
                },
            )
            log.info("render_artifact_persisted", artifact_id=ref.artifact_id)

        # 5) reload + verify the registry entry and output before success
        reloaded_registry = ArtifactRegistry.load(run_dir, run_state.run_id)
        reloaded_registry.require(ref.artifact_id)
        if not (Path(run_dir) / ref.path).is_file():
            raise RenderError("rendered output is missing after artifact registration")

        # 6) success
        run_state.set_stage(PRODUCTION_STAGE_NAME, "succeeded")
        log.info("render_stage_succeeded", artifact_id=ref.artifact_id, output=output.path)
        return replace(result, ok=True, output=output, artifact=ref)
    except Exception as exc:  # noqa: BLE001 — the stage boundary must never fake success
        return fail(exc)




