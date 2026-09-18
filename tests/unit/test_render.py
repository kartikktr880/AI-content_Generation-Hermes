"""P4.5 render stage tests.

Covers: environment (ffmpeg/ffprobe callable), renderer health, real
render over the full P1-B→P2→P3→P4 chain, ffprobe-verified output,
artifact registration/reload, idempotent rerun, state lifecycle, and
failure paths (no false success).
"""

import hashlib
import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ayce.adapters import Adapter, AdapterHealth, AdapterRegistry
from ayce.artifacts import ArtifactKind, ArtifactRegistry
from ayce.asset_resolution import FileBackedAssetProvider, run_asset_resolution_stage
from ayce.config import Config
from ayce.ids import new_job_id, new_run_id
from ayce.logging import StructuredLogger
from ayce.narration_audio import FileBackedNarrationProvider, run_narration_audio_stage
from ayce.render import (
    RENDER_DIRNAME,
    STAGE_NAME,
    FFmpegRenderer,
    RenderedOutput,
    RenderError,
    run_render_stage,
)
from ayce.script_to_scene import load_script_input, run_script_to_scene_stage
from ayce.state import RunState
from ayce.timeline import run_timeline_stage

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def make_run(run_id: str | None = None) -> RunState:
    return RunState(run_id=run_id or new_run_id(), job_id=new_job_id())


def full_chain(tmp_path: Path):
    """Execute the real P1-B -> P2 -> P3 -> P4 pipeline in a temp run directory."""
    run_id = new_run_id()
    registry = ArtifactRegistry(tmp_path / "run", run_id)
    run = make_run(run_id)
    scene_result = run_script_to_scene_stage(
        load_script_input(FIXTURES / "script_to_scene" / "documentary.json"),
        run, registry,
    )
    assert scene_result.ok, scene_result.error
    asset_result = run_asset_resolution_stage(
        scene_result.manifest, run, registry,
        provider=FileBackedAssetProvider(Config.from_env(env={})),
    )
    assert asset_result.ok, asset_result.error
    narration_result = run_narration_audio_stage(
        scene_result.manifest, run, registry,
        provider=FileBackedNarrationProvider(Config.from_env(env={})),
    )
    assert narration_result.ok, narration_result.error
    timeline_result = run_timeline_stage(
        scene_manifest=scene_result.manifest,
        asset_manifest=asset_result.manifest,
        narration_manifest=narration_result.manifest,
        run_state=run,
        artifact_registry=registry,
    )
    assert timeline_result.ok, timeline_result.error
    return timeline_result.manifest, registry, run_id


def make_renderer() -> FFmpegRenderer:
    return FFmpegRenderer(Config.from_env(env={}))

# ---- environment / renderer health --------------------------------------------


def test_ffmpeg_and_ffprobe_are_callable():
    """Environment requirement: both executables run version queries."""
    for name in ("ffmpeg", "ffprobe"):
        proc = subprocess.run([name, "-version"], capture_output=True, text=True, timeout=15)
        assert proc.returncode == 0
        assert proc.stdout.startswith(name)


def test_renderer_health_truthful():
    health = make_renderer().health()
    assert isinstance(health, AdapterHealth)
    assert health.available is True
    assert health.name == "ffmpeg-smoke"


def test_renderer_unhealthy_when_executables_missing(tmp_path):
    from ayce.config import Config

    # explicit config paths that do not exist → unhealthy (PATH not consulted)
    renderer = FFmpegRenderer(Config(
        ffmpeg_path=str(tmp_path / "no-ffmpeg.exe"),
        ffprobe_path=str(tmp_path / "no-ffprobe.exe"),
    ))
    health = renderer.health()
    assert health.available is False
    assert "not found" in health.detail


def test_renderer_fits_the_p0_adapter_convention():
    renderer = make_renderer()
    assert isinstance(renderer, Adapter)
    registry = AdapterRegistry()
    registry.register(FFmpegRenderer)
    assert "ffmpeg-smoke" in registry.names()
    created = registry.create("ffmpeg-smoke", Config.from_env(env={}))
    assert isinstance(created, FFmpegRenderer)


# ---- real smoke render over the full chain ------------------------------------


def test_real_render_of_first_timeline_scene(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)

    result = run_render_stage(timeline, run, registry, renderer=make_renderer())

    assert result.ok, result.error
    assert result.renderer == "ffmpeg-smoke"
    assert result.output is not None and result.artifact is not None
    output = result.output
    assert isinstance(output, RenderedOutput)
    mp4 = tmp_path / "run" / output.path
    # real file, non-zero size
    assert mp4.is_file() and mp4.stat().st_size > 0
    # ffprobe-verified metadata carried on the output record
    assert output.duration_seconds is not None and output.duration_seconds > 0
    assert output.width == 64 and output.height == 64
    assert output.has_audio is True
    # integrity digest matches the actual bytes
    assert hashlib.sha256(mp4.read_bytes()).hexdigest() == output.sha256
    # RunState reached succeeded
    record = run.stage(STAGE_NAME)
    assert record.status.value == "succeeded"
    assert record.attempts == 1


def test_ffprobe_parses_rendered_mp4_streams(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)
    result = run_render_stage(
        timeline, run, registry, renderer=make_renderer(), scene_id="scene-001"
    )
    assert result.ok
    mp4 = tmp_path / "run" / result.output.path
    # independent ffprobe validation (outside the renderer)
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(mp4)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0
    probe = json.loads(proc.stdout)
    streams = probe["streams"]
    video = next(s for s in streams if s["codec_type"] == "video")
    audio = next((s for s in streams if s["codec_type"] == "audio"), None)
    assert video["codec_name"] == "h264"
    assert video["width"] == 64 and video["height"] == 64
    assert float(probe["format"]["duration"]) > 0
    assert any(s["codec_type"] == "audio" for s in streams)  # audio stream present


def test_render_output_registered_and_reloadable(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)
    result = run_render_stage(timeline, run, registry, renderer=make_renderer())
    assert result.ok
    ref = result.artifact
    assert ref.kind is ArtifactKind.RENDERED_VIDEO
    assert ref.path == f"{RENDER_DIRNAME}/scene-001.mp4"
    assert ref.metadata["scene_id"] == "scene-001"
    reloaded_registry = ArtifactRegistry.load(tmp_path / "run", run_id)
    ref2 = reloaded_registry.require(ref.artifact_id)
    assert ref2.kind is ArtifactKind.RENDERED_VIDEO
    assert (tmp_path / "run" / ref2.path).is_file()


def test_idempotent_rerun_reuses_existing_render(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    first = run_render_stage(timeline, make_run(run_id), registry, renderer=make_renderer())
    assert first.ok
    second = run_render_stage(
        timeline, make_run(run_id), registry, renderer=make_renderer()
    )
    assert second.ok
    assert second.artifact.artifact_id == first.artifact.artifact_id
    render_refs = [a for a in registry.all() if a.kind is ArtifactKind.RENDERED_VIDEO]
    assert len(render_refs) == 1

# ---- failure paths: no false success --------------------------------------------


def test_unhealthy_renderer_fails_before_render(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)
    renderer = FFmpegRenderer(Config(
        ffmpeg_path=str(tmp_path / "no-ffmpeg.exe"),
        ffprobe_path=str(tmp_path / "no-ffprobe.exe"),
    ))
    result = run_render_stage(timeline, run, registry, renderer=renderer)
    assert result.ok is False
    assert result.renderer == "ffmpeg-smoke"
    assert "unhealthy" in result.error
    assert run.stage(STAGE_NAME).status.value == "failed"
    assert [a for a in registry.all() if a.kind is ArtifactKind.RENDERED_VIDEO] == []


def test_missing_source_file_fails_without_false_success(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)
    # the visual source was never rendered into this run dir — only the
    # manifests exist; the renderer must fail when resolving sources
    empty_registry = ArtifactRegistry(tmp_path / "empty-run", run.run_id)
    result = run_render_stage(timeline, run, empty_registry, renderer=make_renderer())
    assert result.ok is False
    assert "does not exist in the run directory" in result.error
    assert run.stage(STAGE_NAME).status.value == "failed"
    assert [a for a in empty_registry.all() if a.kind is ArtifactKind.RENDERED_VIDEO] == []


def test_ffmpeg_failure_on_invalid_source_fails(tmp_path):
    """FFmpeg process failure: a run dir whose visual source exists but is
    not decodable media → ffmpeg non-zero exit → stage failed."""
    timeline, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)
    garbage_dir = tmp_path / "garbage-run"
    (garbage_dir / "assets").mkdir(parents=True)
    (garbage_dir / "assets" / "scene-001.png").write_text("not a png", encoding="utf-8")
    (garbage_dir / "audio").mkdir(parents=True)
    shutil.copyfile(
        FIXTURES / "narration_fixtures" / "scene-001.wav",
        garbage_dir / "audio" / "scene-001.wav",
    )
    garbage_registry = ArtifactRegistry(garbage_dir, run.run_id)
    result = run_render_stage(timeline, run, garbage_registry, renderer=make_renderer())
    assert result.ok is False
    assert run.stage(STAGE_NAME).status.value == "failed"
    assert [a for a in garbage_registry.all() if a.kind is ArtifactKind.RENDERED_VIDEO] == []

# ---- state lifecycle: retry / terminal -------------------------------------------


def test_retry_after_failure_is_possible(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)
    # first attempt: unhealthy renderer → failed
    bad = FFmpegRenderer(Config(
        ffmpeg_path=str(tmp_path / "no-ffmpeg.exe"),
        ffprobe_path=str(tmp_path / "no-ffprobe.exe"),
    ))
    first = run_render_stage(timeline, run, registry, renderer=bad)
    assert first.ok is False
    # retry with a healthy renderer: failed → running → succeeded
    second = run_render_stage(timeline, run, registry, renderer=make_renderer())
    assert second.ok is True
    assert run.stage(STAGE_NAME).status.value == "succeeded"
    assert run.stage(STAGE_NAME).attempts == 2


def test_rerun_after_success_is_rejected_by_state_machine(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)
    first = run_render_stage(timeline, run, registry, renderer=make_renderer())
    assert first.ok
    # succeeded is terminal: the same RunState cannot run the stage again
    second = run_render_stage(timeline, run, registry, renderer=make_renderer())
    assert second.ok is False
    assert run.stage(STAGE_NAME).status.value == "succeeded"  # unchanged


# ---- observability -----------------------------------------------------------------


def test_stage_logs_structured_events(tmp_path):
    stream = io.StringIO()
    log = StructuredLogger(level="INFO", stream=stream)
    timeline, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)

    result = run_render_stage(
        timeline, run, registry, renderer=make_renderer(), logger=log
    )
    assert result.ok

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    events = [r["message"] for r in records]
    assert "render_stage_started" in events
    assert "renderer_health_checked" in events
    assert "ffmpeg_process_completed" in events
    assert "render_output_validated" in events
    assert "render_artifact_persisted" in events
    assert "render_stage_succeeded" in events
    started = records[events.index("render_stage_started")]
    assert started["run_id"] == run.run_id
    assert started["stage"] == STAGE_NAME
    assert started["renderer"] == "ffmpeg-smoke"
    completed = records[events.index("ffmpeg_process_completed")]
    assert completed["duration_seconds"] is not None
    assert completed["width"] == 64

    # failure event with identity
    stream = io.StringIO()
    fail_run = make_run()
    fail_registry = ArtifactRegistry(tmp_path / "fail-run", fail_run.run_id)
    failed = run_render_stage(
        timeline,
        fail_run,
        fail_registry,
        renderer=FFmpegRenderer(Config(
            ffmpeg_path=str(tmp_path / "no-ffmpeg.exe"),
            ffprobe_path=str(tmp_path / "no-ffprobe.exe"),
        )),
        logger=StructuredLogger(level="INFO", stream=stream),
    )
    assert failed.ok is False
    fail_records = [json.loads(line) for line in stream.getvalue().splitlines()]
    fail_events = [r["message"] for r in fail_records]
    assert "render_stage_failed" in fail_events
    failed_rec = fail_records[fail_events.index("render_stage_failed")]
    assert failed_rec["stage"] == STAGE_NAME
    assert "error" in failed_rec



