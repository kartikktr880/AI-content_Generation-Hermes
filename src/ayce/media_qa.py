"""P5.5 — Technical Media QA stage (independent verification of the render).

A bounded, Hermes-compatible capability that consumes the verified P5
production render artifact and the upstream Timeline/Narration manifests
and answers exactly one question:

    "Is this rendered production artifact technically valid enough to
     proceed to downstream production steps?"

    Rendered Production MP4 (rendered_video artifact reference)
            + Timeline Manifest + Narration Manifest
        ↓ independent ffprobe + lightweight deterministic checks
    qa_report.json (ArtifactKind.QA_REPORT)
        ↓
    PASS / FAIL verdict (deterministic, no scoring)

This is TECHNICAL MEDIA QA, not complete production QA. It proves the
artifact is a readable, technically valid MP4 matching the declared
timeline. It does NOT evaluate creative quality, factual correctness,
captions, rights, audio loudness, visual aesthetics, or narrative
quality — those belong to future, independently-scoped QA layers.

Boundary rules:

- The QA stage independently inspects the actual output file with
  ffprobe. "P5 said render succeeded" is never QA evidence.
- Inputs are resolved through the existing artifact registry
  conventions: the stage consumes an ``ArtifactRef`` (kind
  ``rendered_video``), never an arbitrary filesystem path.
- FFmpeg/ffprobe are resolved with the shared P4.5 policy (optional
  ``AYCE_FFMPEG_PATH`` / ``AYCE_FFPROBE_PATH`` config, PATH fallback).
  Subprocess execution uses explicit argument lists — never
  ``shell=True``, never concatenated shell commands.
- Duration tolerance reuses the P5 production-render policy exactly
  (``±0.5 s + 2%``) — no second tolerance system.
- Scene coverage reuses the lightweight, deterministic frame-color
  sampling approach proven in the P5 tests (raw RGB via ffmpeg — no
  computer vision, no ML, no perceptual similarity).
- Failure semantics: a QA FAIL (readable media that fails a technical
  gate) produces a persisted, registered QA report with ``verdict=FAIL``
  and a SUCCEEDED stage — the stage's job is to produce truthful
  evidence. A QA EXECUTION ERROR (the stage itself cannot do its job:
  invalid artifact reference, malformed manifest, missing ffprobe,
  persistence failure) fails the stage and publishes no artifact.
- No QA → repair loop: this stage never re-renders, never repairs, and
  never orchestrates other stages. It only produces evidence.
- Idempotency: the report is bound to the rendered artifact's current
  content identity (sha256). A rerun against the same valid render
  reuses the existing QA artifact (no duplicate registration); a stale
  or corrupt report (or a changed render) is re-executed and registered
  under a new artifact identity — an existing file is never accepted as
  valid merely because it exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .artifacts import ArtifactKind, ArtifactRef, ArtifactRegistry
from .config import Config
from .logging import StructuredLogger
from .narration_audio import NarrationManifest
from .render import (
    DURATION_TOLERANCE_ABS_SECONDS,
    DURATION_TOLERANCE_RELATIVE,
    resolve_media_tool,
)
from .state import RunState
from .timeline import TimelineElementKind, TimelineManifest, TimelineScene

__all__ = [
    "STAGE_NAME",
    "ARTIFACT_STAGE",
    "QA_REPORT_FILENAME",
    "QA_REPORT_SCHEMA_VERSION",
    "COLOR_MATCH_TOLERANCE",
    "MediaQAError",
    "QAVerdict",
    "QACheckStatus",
    "QACheck",
    "SceneCoverageEvidence",
    "QAArtifactRef",
    "QASummary",
    "QAReport",
    "MediaQAResult",
    "run_media_qa_stage",
]

#: RunState stage label for the P5.5 capability (stage-specific; never
#: reuses the P4.5/P5 render stage labels).
STAGE_NAME = "media_qa"
#: ArtifactRegistry stage label identifying the QA-producing stage.
ARTIFACT_STAGE = "media_qa"
#: Persisted QA report filename inside the run directory.
QA_REPORT_FILENAME = "qa_report.json"
#: Version stamped onto newly created QA reports.
QA_REPORT_SCHEMA_VERSION = "1.0"
#: The only QA-report schema major version this build understands.
QA_REPORT_SUPPORTED_MAJOR = 1

#: Per-channel RGB tolerance for the lightweight color checks; mirrors
#: the variance allowance proven by the P5 frame-color tests.
COLOR_MATCH_TOLERANCE = 24

#: Frame-sampling geometry for scene coverage (center pixel of a scaled
#: frame — solid colors survive this; content detail does not matter).
_SAMPLE_WIDTH = 64
_SAMPLE_HEIGHT = 64

_PROBE_TIMEOUT_SECONDS = 30
_FRAME_TIMEOUT_SECONDS = 30

_STILL_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".gif"}


class MediaQAError(RuntimeError):
    """Raised when the QA stage itself cannot perform its job.

    This is a QA EXECUTION ERROR (invalid artifact reference, malformed
    required manifest, unavailable ffprobe, persistence failure) —
    distinct from a QA FAIL, where the media was inspected and failed a
    technical gate.
    """


class QAVerdict(str, Enum):
    """Deterministic overall verdict. No scoring, no percentages."""

    PASS = "PASS"
    FAIL = "FAIL"


class QACheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class QACheck(BaseModel):
    """One structured technical check result (never human prose only)."""

    model_config = ConfigDict(extra="forbid")

    check_id: str
    #: file | container | video | audio | duration | scene_coverage
    category: str
    status: QACheckStatus
    expected: str | None = None
    actual: str | None = None
    message: str


class SceneCoverageEvidence(BaseModel):
    """Lightweight per-scene coverage evidence (deterministic, no CV).

    Records what was sampled at each scene's midpoint and against what
    it was compared. NOT frame-accurate scene-boundary detection.
    """

    model_config = ConfigDict(extra="forbid")

    scene_id: str
    expected_start_seconds: float
    expected_end_seconds: float
    #: Center-pixel RGB sampled from the production at the scene midpoint.
    sampled_color: tuple[int, int, int] | None = None
    #: Expected color, when a deterministic expectation exists.
    expected_color: tuple[int, int, int] | None = None
    #: Why the expectation holds: still_image_source | video_source |
    #: no_visual_black_filler.
    basis: str
    status: QACheckStatus
    detail: str


class QAArtifactRef(BaseModel):
    """Artifact reference embedded in the report (never absolute paths)."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    stage: str
    kind: str
    #: Run-relative posix path.
    path: str
    #: sha256 of the rendered file bytes at QA time (identity binding).
    sha256: str


class QASummary(BaseModel):
    """Check tallies backing the deterministic verdict rule."""

    model_config = ConfigDict(extra="forbid")

    total_checks: int
    passed: int
    failed: int


class QAReport(BaseModel):
    """Structured, machine-actionable technical media QA report.

    Verdict rule (explicit, deterministic): ``FAIL`` iff any check is
    ``FAIL``; ``PASS`` only when every required check passes. No
    subjective scoring, no percentage quality score.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = QA_REPORT_SCHEMA_VERSION
    production_id: str
    render_artifact: QAArtifactRef
    #: sha256 of the rendered file bytes at QA time (staleness binding).
    render_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: Run-relative references to the upstream manifests consumed.
    timeline_manifest: str
    narration_manifest: str
    verdict: QAVerdict
    checks: tuple[QACheck, ...] = Field(min_length=1)
    scene_coverage: tuple[SceneCoverageEvidence, ...] = ()
    summary: QASummary

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9]+\.[0-9]+", value):
            raise ValueError(
                f"schema_version {value!r} must be 'MAJOR.MINOR' "
                f"(e.g. {QA_REPORT_SCHEMA_VERSION!r})"
            )
        major = int(value.split(".", 1)[0])
        if major != QA_REPORT_SUPPORTED_MAJOR:
            raise ValueError(
                f"unsupported schema_version {value!r}; this build understands "
                f"major version {QA_REPORT_SUPPORTED_MAJOR} only"
            )
        return value


@dataclass(frozen=True)
class MediaQAResult:
    """Outcome of one media-QA execution, for future Hermes.

    ``ok`` is trustworthy and means: the QA stage EXECUTED successfully
    and produced a persisted, registered QA report. The technical
    verdict lives in ``verdict`` / ``report`` — a QA FAIL (bad media)
    is ``ok=True, verdict=FAIL``; a QA execution error is ``ok=False``
    with ``error`` set and NO artifact published.
    """

    ok: bool
    stage: str
    run_id: str
    production_id: str | None
    verdict: str | None
    report: QAReport | None
    artifact: ArtifactRef | None
    error: str | None = None
    reused: bool = False


# ---- probing / sampling helpers -------------------------------------------------


def _run_tool(executable: str, args: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    """Run an external tool with an explicit argument list (no shell)."""
    try:
        return subprocess.run(
            [executable, *args],
            capture_output=True,
            timeout=timeout,
        )
    except OSError as exc:
        raise MediaQAError(f"external tool invocation failed: {exc}") from exc


def _probe_media(ffprobe: str, media_path: Path) -> dict[str, Any]:
    """Independently ffprobe a media file; return the parsed JSON report."""
    proc = _run_tool(
        ffprobe,
        ["-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(media_path)],
        timeout=_PROBE_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        detail = stderr[-1] if stderr else "no output"
        raise MediaQAError(f"ffprobe exited {proc.returncode}: {detail}")
    try:
        probe = json.loads(proc.stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MediaQAError(f"ffprobe produced unparseable output: {exc}") from exc
    if not isinstance(probe, dict):
        raise MediaQAError("ffprobe produced unexpected output (no JSON object)")
    return probe


def _sample_frame_rgb(ffmpeg: str, video: Path, at_seconds: float) -> tuple[int, int, int]:
    """Sample the center pixel of one frame as raw RGB (the P5 approach).

    Lightweight and deterministic: ffmpeg decodes ONE frame, scales it
    to the sampling geometry, and emits raw rgb24 bytes on stdout. No
    computer vision — this cannot and does not claim scene-boundary
    detection.
    """
    proc = _run_tool(
        ffmpeg,
        [
            "-loglevel", "error",
            "-ss", f"{max(at_seconds, 0.0):.3f}",
            "-i", str(video),
            "-frames:v", "1",
            "-vf", f"scale={_SAMPLE_WIDTH}:{_SAMPLE_HEIGHT}",
            "-pix_fmt", "rgb24",
            "-f", "rawvideo",
            "pipe:1",
        ],
        timeout=_FRAME_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        detail = stderr[-1] if stderr else "no output"
        raise MediaQAError(f"ffmpeg frame sampling exited {proc.returncode}: {detail}")
    raw = proc.stdout
    needed = _SAMPLE_WIDTH * _SAMPLE_HEIGHT * 3
    if len(raw) < needed:
        raise MediaQAError(
            f"ffmpeg frame sampling returned {len(raw)} bytes, expected {needed}"
        )
    center = ((_SAMPLE_HEIGHT // 2) * _SAMPLE_WIDTH + (_SAMPLE_WIDTH // 2)) * 3
    return (raw[center], raw[center + 1], raw[center + 2])


def _colors_match(a: tuple[int, int, int], b: tuple[int, int, int]) -> bool:
    return all(abs(x - y) <= COLOR_MATCH_TOLERANCE for x, y in zip(a, b))


def _ev(evidence: SceneCoverageEvidence, **updates: Any) -> SceneCoverageEvidence:
    """Immutable update for a coverage-evidence record (pydantic model)."""
    return evidence.model_copy(update=updates)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stream_duration(stream: dict[str, Any]) -> float | None:
    """Stream-level duration, validated where ffprobe reports one."""
    raw = stream.get("duration")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _check(check_id: str, category: str, ok: bool, message: str, *,
           expected: str | None = None, actual: str | None = None) -> QACheck:
    return QACheck(
        check_id=check_id,
        category=category,
        status=QACheckStatus.PASS if ok else QACheckStatus.FAIL,
        expected=expected,
        actual=actual,
        message=message,
    )


def _not_evaluated(check_id: str, category: str, message: str, reason: str) -> QACheck:
    return _check(
        check_id, category, False, f"{message} (not evaluated: {reason})",
        actual="not evaluated",
    )


def _build_file_checks(
    ref: ArtifactRef, media_path: Path, run_dir: Path
) -> list[QACheck]:
    """A. File existence: resolves inside the run dir, exists, non-empty."""
    inside = media_path.resolve().is_relative_to(Path(run_dir).resolve())
    exists = media_path.is_file()
    non_empty = exists and media_path.stat().st_size > 0
    return [
        _check(
            "file.path_inside_run_dir", "file", inside,
            "artifact path must resolve inside the run directory",
            expected="run-relative path inside the run directory",
            actual=str(ref.path),
        ),
        _check(
            "file.exists", "file", exists,
            "production artifact file must exist",
            expected="existing file",
            actual="file exists" if exists else "file missing",
        ),
        _check(
            "file.non_empty", "file", non_empty,
            "production artifact file must be non-empty",
            expected="size > 0 bytes",
            actual=f"{media_path.stat().st_size} bytes" if exists else "file missing",
        ),
    ]


def _build_stream_checks(
    checks: list[QACheck],
    probe: dict[str, Any] | None,
    probe_error: str | None,
) -> float | None:
    """B/C/D. Container validity + video stream + audio stream checks.

    Appends checks to ``checks`` and returns the verified container
    duration (``None`` when the container could not be probed).
    """
    if probe is None:
        for check_id, category, message in (
            ("container.ffprobe_parses", "container", "ffprobe must parse the container"),
            ("container.duration_positive", "container",
             "container duration must exist and be positive"),
            ("video.stream_present", "video", "a video stream with a codec must exist"),
            ("video.dimensions", "video", "video width and height must be positive"),
            ("video.stream_duration", "video", "video stream duration must be valid"),
            ("audio.stream_present", "audio", "an audio stream with a codec must exist"),
            ("audio.stream_duration", "audio",
             "audio stream duration must be valid where reported"),
        ):
            checks.append(_not_evaluated(check_id, category, message, probe_error or "unknown"))
        return None

    fmt = probe.get("format") if isinstance(probe.get("format"), dict) else {}
    streams = probe.get("streams") if isinstance(probe.get("streams"), list) else []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    # B. container validity (independent ffprobe)
    checks.append(_check(
        "container.ffprobe_parses", "container", True,
        "ffprobe parsed the container successfully",
        actual=f"format={fmt.get('format_name', 'unknown')!r}",
    ))
    raw_duration = fmt.get("duration")
    try:
        container_duration = float(raw_duration) if raw_duration is not None else None
    except (TypeError, ValueError):
        container_duration = None
    checks.append(_check(
        "container.duration_positive", "container",
        container_duration is not None and container_duration > 0,
        "container duration must exist and be positive",
        expected="duration > 0 seconds",
        actual=str(container_duration) if container_duration is not None else "missing",
    ))

    # C. video stream
    video_codec = video.get("codec_name") if video else None
    checks.append(_check(
        "video.stream_present", "video", bool(video and video_codec),
        "a video stream with a codec must exist",
        expected="video stream with a codec",
        actual=f"codec={video_codec!r}" if video else "no video stream",
    ))
    width, height = (video or {}).get("width"), (video or {}).get("height")
    dims_ok = isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0
    checks.append(_check(
        "video.dimensions", "video", dims_ok,
        "video dimensions must be positive",
        expected="width > 0 and height > 0",
        actual=f"{width}x{height}",
    ))
    video_duration = _stream_duration(video) if video else container_duration
    checks.append(_check(
        "video.stream_duration", "video",
        video_duration is not None and video_duration > 0,
        "video stream duration must be valid",
        expected="duration > 0 seconds",
        actual=(
            str(video_duration) if video_duration is not None
            else "not reported and no valid container duration"
        ),
    ))

    # D. audio stream
    audio_codec = audio.get("codec_name") if audio else None
    checks.append(_check(
        "audio.stream_present", "audio", bool(audio and audio_codec),
        "an audio stream with a codec must exist",
        expected="audio stream with a codec",
        actual=f"codec={audio_codec!r}" if audio else "no audio stream",
    ))
    audio_duration = _stream_duration(audio) if audio else None
    if audio_duration is not None:
        audio_ok, audio_actual = True, str(audio_duration)
    else:
        # a stream-level duration that ffprobe does not report is
        # acceptable where the container duration is valid
        # ("valid where available")
        audio_ok = container_duration is not None and container_duration > 0
        audio_actual = (
            "not reported; container duration applies" if audio else "no audio stream"
        )
    checks.append(_check(
        "audio.stream_duration", "audio", audio_ok,
        "audio stream duration must be valid where reported",
        expected="duration > 0 seconds where reported",
        actual=audio_actual,
    ))
    return container_duration


def _classify_scene_evidence(
    evidence: SceneCoverageEvidence,
    scene: TimelineScene,
    sampled: tuple[int, int, int],
    run_dir: Path,
    ffmpeg: str,
) -> SceneCoverageEvidence:
    """Compare the sampled midpoint color against the scene's composition."""
    visual = next(
        (e for e in scene.elements if e.kind is TimelineElementKind.VISUAL), None
    )
    if visual is None:
        # documented P4 composition policy: a scene with no visual
        # element renders as black filler for its interval
        black = (0, 0, 0)
        ok = _colors_match(sampled, black)
        return _ev(
            evidence,
            expected_color=black,
            status=QACheckStatus.PASS if ok else QACheckStatus.FAIL,
            detail=(
                "documented no-visual black filler; sampled color matches"
                if ok else
                f"documented no-visual black filler violated: sampled {sampled}"
            ),
        )
    if Path(visual.source).suffix.lower() in _STILL_IMAGE_SUFFIXES:
        source_path = Path(run_dir) / visual.source
        try:
            source_color = _sample_frame_rgb(ffmpeg, source_path, 0.0)
        except MediaQAError:
            return _ev(evidence, detail="resolved still-image source not sampleable")
        ok = _colors_match(sampled, source_color)
        return _ev(
            evidence,
            expected_color=source_color,
            basis="still_image_source",
            status=QACheckStatus.PASS if ok else QACheckStatus.FAIL,
            detail=(
                "midpoint frame matches the scene's resolved still image"
                if ok else
                f"midpoint frame {sampled} does not match the scene's "
                f"still image {source_color}"
            ),
        )
    # video source: frames are stream-copied into the production, so a
    # frame exists, but solid-color equivalence is not guaranteed for
    # general video content — documented lightweight-coverage limitation
    return _ev(
        evidence,
        basis="video_source",
        status=QACheckStatus.PASS,
        detail=(
            "frame sampled at the scene midpoint; video sources are "
            "stream-copied and are not color-compared (documented "
            "lightweight-coverage limitation)"
        ),
    )


def _build_checks(
    *,
    ref: ArtifactRef,
    media_path: Path,
    run_dir: Path,
    probe: dict[str, Any] | None,
    probe_error: str | None,
    timeline: TimelineManifest,
    ffmpeg: str | None,
) -> tuple[list[QACheck], list[SceneCoverageEvidence]]:
    """Build every P5.5 technical check from independently gathered evidence."""
    checks = _build_file_checks(ref, media_path, run_dir)
    non_empty = next(
        c for c in checks if c.check_id == "file.non_empty"
    ).status is QACheckStatus.PASS
    _build_stream_checks(checks, probe, probe_error)
    _build_duration_check(checks, probe, timeline)
    coverage = _build_coverage_checks(
        checks, timeline, media_path, run_dir, probe, non_empty, ffmpeg
    )
    return checks, coverage


def run_media_qa_stage(
    rendered_artifact: ArtifactRef,
    timeline_manifest: TimelineManifest | dict[str, Any],
    narration_manifest: NarrationManifest | dict[str, Any],
    run_state: RunState,
    artifact_registry: ArtifactRegistry,
    config: Config,
    *,
    logger: StructuredLogger | None = None,
) -> MediaQAResult:
    """Execute the P5.5 technical media QA stage.

    This is the single callable a future orchestrator (Hermes) uses:

        result = run_media_qa_stage(
            rendered_artifact=render_ref,   # registered rendered_video ref
            timeline_manifest=timeline,     # from the timeline artifact
            narration_manifest=narration,   # from the narration artifact
            run_state=run_state,
            artifact_registry=registry,
            config=config,
        )

    Independently inspects the actual rendered production file (ffprobe
    + deterministic frame sampling), produces ``qa_report.json`` with a
    deterministic PASS/FAIL verdict, registers it as an
    ``ArtifactKind.QA_REPORT`` artifact, and reload-verifies both.

    Semantics (explicit):

    - QA FAIL (media readable but failing a technical gate): the report
      is persisted and registered with ``verdict=FAIL``; the stage state
      is ``succeeded`` — producing truthful evidence IS the stage's job.
    - QA EXECUTION ERROR (the stage cannot do its job): stage state
      ``failed``, no artifact published, ``result.ok=False``.
    - Idempotency: a report bound to the same render content identity
      (sha256) is reused; stale/corrupt reports or changed renders are
      re-executed and registered under a new artifact identity.
    """
    log = logger if logger is not None else StructuredLogger(level="INFO")
    log = log.bind(stage=STAGE_NAME, run_id=run_state.run_id, job_id=run_state.job_id)
    result = MediaQAResult(
        ok=False,
        stage=STAGE_NAME,
        run_id=run_state.run_id,
        production_id=None,
        verdict=None,
        report=None,
        artifact=None,
        error=None,
        reused=False,
    )

    def fail(exc: BaseException) -> MediaQAResult:
        error = f"{type(exc).__name__}: {exc}"
        try:
            run_state.set_stage(STAGE_NAME, "failed", error=error)
        except Exception as mark_failure_error:
            # state machine corruption must not mask the original cause
            log.error("media_qa_stage_failed", error=exc, state_error=str(mark_failure_error))
        else:
            log.error("media_qa_stage_failed", error=exc)
        return replace(result, error=error)

    try:
        run_state.set_stage(STAGE_NAME, "running")
        log.info("media_qa_stage_started")

        # 1) validate the rendered-artifact reference through the registry
        if not isinstance(rendered_artifact, ArtifactRef):
            raise MediaQAError(
                f"rendered_artifact must be an ArtifactRef, "
                f"got {type(rendered_artifact).__name__}"
            )
        if rendered_artifact.kind is not ArtifactKind.RENDERED_VIDEO:
            raise MediaQAError(
                f"rendered_artifact must have kind rendered_video, "
                f"got {rendered_artifact.kind.value}"
            )
        if rendered_artifact.run_id != run_state.run_id:
            raise MediaQAError(
                f"rendered artifact belongs to run {rendered_artifact.run_id!r}, "
                f"not to run {run_state.run_id!r}"
            )
        registered = artifact_registry.get(rendered_artifact.artifact_id)
        if registered is None or registered != rendered_artifact:
            raise MediaQAError(
                f"rendered artifact {rendered_artifact.artifact_id!r} is not registered "
                f"in this run's artifact registry"
            )

        # 2) load and validate the required manifests
        if not isinstance(timeline_manifest, TimelineManifest):
            timeline_manifest = TimelineManifest.model_validate(timeline_manifest)
        if not isinstance(narration_manifest, NarrationManifest):
            narration_manifest = NarrationManifest.model_validate(narration_manifest)
        production_id = timeline_manifest.production_id
        if narration_manifest.production_id != production_id:
            raise MediaQAError(
                f"production_id mismatch: timeline declares {production_id!r} but the "
                f"narration manifest declares {narration_manifest.production_id!r}"
            )
        ref_production_id = rendered_artifact.metadata.get("production_id")
        if ref_production_id is not None and ref_production_id != production_id:
            raise MediaQAError(
                f"production_id mismatch: render artifact declares {ref_production_id!r} "
                f"but the timeline declares {production_id!r}"
            )
        result = replace(result, production_id=production_id)
        log = log.bind(production_id=production_id)

        # 3) resolve the actual media file (inside the run directory only)
        run_dir = Path(artifact_registry.run_dir)
        media_path = run_dir / rendered_artifact.path
        if not media_path.resolve().is_relative_to(run_dir.resolve()):
            raise MediaQAError(
                f"rendered artifact path {rendered_artifact.path!r} does not resolve "
                f"inside the run directory"
            )
        render_sha256 = (
            _sha256_file(media_path)
            if media_path.is_file() and media_path.stat().st_size > 0
            else None
        )

        # 4) idempotency: reuse a report bound to the SAME render identity
        if render_sha256 is not None:
            reused = _find_reusable_report(
                artifact_registry, run_dir, rendered_artifact,
                render_sha256, production_id,
            )
            if reused is not None:
                ref, report = reused
                run_state.set_stage(STAGE_NAME, "succeeded")
                log.info(
                    "media_qa_stage_completed",
                    verdict=report.verdict.value,
                    artifact_id=ref.artifact_id,
                    reused=True,
                )
                return replace(
                    result,
                    ok=True,
                    verdict=report.verdict.value,
                    report=report,
                    artifact=ref,
                    reused=True,
                )

        # 5) resolve media tooling (shared P4.5 policy; never hardcoded)
        ffprobe = resolve_media_tool(config.ffprobe_path, "ffprobe")
        ffmpeg = resolve_media_tool(config.ffmpeg_path, "ffmpeg")
        if ffprobe is None or not Path(ffprobe).is_file():
            raise MediaQAError("ffprobe executable not found (set AYCE_FFPROBE_PATH or PATH)")
        if ffmpeg is None or not Path(ffmpeg).is_file():
            raise MediaQAError("ffmpeg executable not found (set AYCE_FFMPEG_PATH or PATH)")

        # 6) INDEPENDENT inspection — "P5 said success" is not evidence
        try:
            probe: dict[str, Any] | None = _probe_media(ffprobe, media_path)
            probe_error = None
        except MediaQAError as exc:
            probe, probe_error = None, str(exc)
        fmt = (probe or {}).get("format") or {}
        streams = (probe or {}).get("streams") or []
        log.info(
            "media_qa_probe_completed",
            parsed=probe is not None,
            duration_seconds=fmt.get("duration"),
            streams=len(streams) if isinstance(streams, list) else 0,
            probe_error=probe_error,
        )

        # 7) build every technical check from independently gathered evidence
        checks, coverage = _build_checks(
            ref=rendered_artifact,
            media_path=media_path,
            run_dir=run_dir,
            probe=probe,
            probe_error=probe_error,
            timeline=timeline_manifest,
            ffmpeg=ffmpeg,
        )
        failed_checks = [c for c in checks if c.status is QACheckStatus.FAIL]
        verdict = QAVerdict.FAIL if failed_checks else QAVerdict.PASS
        report = QAReport(
            production_id=production_id,
            render_artifact=QAArtifactRef(
                artifact_id=rendered_artifact.artifact_id,
                stage=rendered_artifact.stage,
                kind=rendered_artifact.kind.value,
                path=rendered_artifact.path,
                sha256=render_sha256 or "0" * 64,
            ),
            render_sha256=render_sha256 or "0" * 64,
            timeline_manifest="timeline.json",
            narration_manifest="narration_manifest.json",
            verdict=verdict,
            checks=tuple(checks),
            scene_coverage=tuple(coverage),
            summary=QASummary(
                total_checks=len(checks),
                passed=len(checks) - len(failed_checks),
                failed=len(failed_checks),
            ),
        )

        # 8) persist (atomic) + register through the existing mechanism
        report_path = run_dir / QA_REPORT_FILENAME
        tmp = report_path.with_name(report_path.name + ".tmp")
        tmp.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, report_path)
        ref = artifact_registry.register(
            ARTIFACT_STAGE,
            ArtifactKind.QA_REPORT,
            QA_REPORT_FILENAME,
            {
                "production_id": production_id,
                "verdict": verdict.value,
                "render_sha256": render_sha256,
                "render_artifact_id": rendered_artifact.artifact_id,
                "total_checks": report.summary.total_checks,
                "failed_checks": report.summary.failed,
            },
        )
        log.info(
            "media_qa_report_persisted",
            artifact_id=ref.artifact_id,
            verdict=verdict.value,
            total_checks=report.summary.total_checks,
            failed_checks=report.summary.failed,
        )

        # 9) reload + verify before reporting success
        reloaded_registry = ArtifactRegistry.load(run_dir, run_state.run_id)
        reloaded_registry.require(ref.artifact_id)
        reloaded_report = QAReport.model_validate_json(
            (run_dir / QA_REPORT_FILENAME).read_text(encoding="utf-8")
        )
        if reloaded_report.verdict is not verdict:
            raise MediaQAError("persisted QA report does not match the computed verdict")

        # 10) success (regardless of verdict — the evidence is the product)
        run_state.set_stage(STAGE_NAME, "succeeded")
        log.info("media_qa_stage_completed", verdict=verdict.value, artifact_id=ref.artifact_id)
        return replace(result, ok=True, verdict=verdict.value, report=report, artifact=ref)
    except Exception as exc:  # noqa: BLE001 — the stage boundary must never fake success
        return fail(exc)


def _find_reusable_report(
    registry: ArtifactRegistry,
    run_dir: Path,
    rendered_artifact: ArtifactRef,
    render_sha256: str,
    production_id: str,
) -> tuple[ArtifactRef, QAReport] | None:
    """Idempotency guard: a QA report is reusable ONLY when it is bound to
    the current render content identity (sha256), the same render artifact,
    the same production, and the report file still parses. A stale or
    corrupt report is never accepted merely because the file exists."""
    for ref in registry.for_stage(ARTIFACT_STAGE):
        if ref.kind is not ArtifactKind.QA_REPORT:
            continue
        meta = ref.metadata
        if meta.get("render_sha256") != render_sha256:
            continue
        if meta.get("render_artifact_id") != rendered_artifact.artifact_id:
            continue
        if meta.get("production_id") != production_id:
            continue
        report_file = run_dir / ref.path
        if not report_file.is_file():
            continue
        try:
            report = QAReport.model_validate_json(report_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — a corrupt report is simply not reusable
            continue
        if report.render_sha256 != render_sha256:
            continue
        if report.render_artifact.artifact_id != rendered_artifact.artifact_id:
            continue
        if report.production_id != production_id:
            continue
        return ref, report
    return None



def _build_duration_check(
    checks: list[QACheck], probe: dict[str, Any] | None, timeline: TimelineManifest
) -> None:
    """E. Timeline duration — reuses the P5 tolerance policy exactly."""
    expected_total = timeline.total_duration_seconds
    tolerance = DURATION_TOLERANCE_ABS_SECONDS + DURATION_TOLERANCE_RELATIVE * expected_total
    actual_duration = None
    if probe is not None:
        raw = (probe.get("format") or {}).get("duration")
        try:
            actual_duration = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            actual_duration = None
    delta = abs(actual_duration - expected_total) if actual_duration is not None else None
    duration_ok = delta is not None and delta <= tolerance
    checks.append(_check(
        "duration.matches_timeline", "duration", duration_ok,
        "rendered duration must match the timeline total within the documented "
        f"P5 tolerance (±{tolerance:.2f}s)",
        expected=f"{expected_total}s ± {tolerance:.2f}s",
        actual=(
            f"{actual_duration}s (absolute delta {delta:.3f}s)" if delta is not None
            else "rendered duration unavailable"
        ),
    ))


def _build_coverage_checks(
    checks: list[QACheck],
    timeline: TimelineManifest,
    media_path: Path,
    run_dir: Path,
    probe: dict[str, Any] | None,
    non_empty: bool,
    ffmpeg: str | None,
) -> list[SceneCoverageEvidence]:
    """F. Lightweight deterministic scene coverage (frame-color sampling).

    Reuses the P5-proven approach: decode ONE frame at each scene's
    midpoint as raw RGB (scaled, center pixel — solid colors survive;
    no computer vision, no ML, no perceptual similarity). For scenes
    whose visual is a resolved still image, the sampled color must match
    that image; for narration-only scenes the documented black-filler
    policy must hold; video sources are sampled but not color-compared
    (documented limitation). This is NOT frame-accurate scene-boundary
    detection.
    """
    coverage: list[SceneCoverageEvidence] = []
    video_ok = probe is not None and any(
        c.status is QACheckStatus.PASS
        for c in checks if c.check_id == "video.stream_present"
    )
    sampleable = video_ok and non_empty and ffmpeg is not None
    for scene in timeline.scenes:
        midpoint = scene.start_seconds + scene.duration_seconds / 2
        evidence = SceneCoverageEvidence(
            scene_id=scene.scene_id,
            expected_start_seconds=scene.start_seconds,
            expected_end_seconds=scene.start_seconds + scene.duration_seconds,
            basis="no_visual_black_filler",
            status=QACheckStatus.FAIL,
            detail="frame not sampleable (container/video invalid)",
        )
        if sampleable:
            try:
                sampled = _sample_frame_rgb(ffmpeg, media_path, midpoint)
            except MediaQAError:
                sampled = None
                evidence = _ev(evidence, detail="frame sampling failed")
            else:
                evidence = _ev(evidence, sampled_color=sampled)
                evidence = _classify_scene_evidence(
                    evidence, scene, sampled, run_dir, ffmpeg
                )
        coverage.append(evidence)
        checks.append(QACheck(
            check_id=f"scene_coverage.{scene.scene_id}",
            category="scene_coverage",
            status=evidence.status,
            expected=(
                f"visual evidence for scene interval "
                f"[{scene.start_seconds}, {scene.start_seconds + scene.duration_seconds}]s"
            ),
            actual=(
                f"sampled {evidence.sampled_color}"
                if evidence.sampled_color is not None else evidence.detail
            ),
            message=(
                f"lightweight frame-color coverage at {midpoint:.3f}s: {evidence.detail}"
            ),
        ))
    return coverage
