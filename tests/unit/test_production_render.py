"""P5 multi-scene production render tests.

Covers: real five-stage chain (P1-B→P2→P3→P4→P5), production MP4 with all
timeline scenes in order, ffprobe validation (streams/duration/coverage via
frame-color sampling), artifact registration/reload, idempotency (incl.
stale-output re-render), state lifecycle, and failure paths.
"""

import hashlib
import io
import json
import subprocess
from pathlib import Path

import pytest

from ayce.adapters import AdapterHealth, AdapterRegistry
from ayce.artifacts import ArtifactKind, ArtifactRegistry
from ayce.asset_resolution import FileBackedAssetProvider, run_asset_resolution_stage
from ayce.config import Config
from ayce.ids import new_job_id, new_run_id
from ayce.logging import StructuredLogger
from ayce.narration_audio import FileBackedNarrationProvider, run_narration_audio_stage
from ayce.render import (
    FFmpegRenderer,
    RenderedOutput,
    RenderError,
    run_production_render_stage,
    run_render_stage,
)
from ayce.script_to_scene import load_script_input, run_script_to_scene_stage
from ayce.state import RunState
from ayce.timeline import run_timeline_stage

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

# expected per-scene dominant colors (matching the fixture assets / black filler)
EXPECTED_COLORS = {
    "scene-001": (16, 32, 64),
    "scene-002": (128, 16, 16),
    "scene-003": (24, 48, 96),
    "scene-004": (0, 0, 0),  # no requirement → documented black filler
    "scene-005": (48, 96, 192),
}


def make_run(run_id: str | None = None) -> RunState:
    return RunState(run_id=run_id or new_run_id(), job_id=new_job_id())


def full_chain(tmp_path: Path, run_id: str | None = None):
    """Execute the real P1-B -> P2 -> P3 -> P4 pipeline in a temp run directory."""
    run_id = run_id or new_run_id()
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


# ---- real five-scene production render ----------------------------------------


def test_production_render_all_scenes_single_mp4(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)

    result = run_production_render_stage(timeline, run, registry, renderer=make_renderer())

    assert result.ok, result.error
    assert result.renderer == "ffmpeg-smoke"
    assert result.output is not None and result.artifact is not None
    output = result.output
    assert isinstance(output, RenderedOutput)
    mp4 = tmp_path / "run" / output.path
    # one production MP4 named after the production, non-zero size
    assert output.path == f"render/{timeline.production_id}.mp4"
    assert mp4.is_file() and mp4.stat().st_size > 0
    # duration consistent with the declared timeline total (documented tolerance)
    expected = timeline.total_duration_seconds
    assert output.duration_seconds == pytest.approx(expected, abs=0.5 + 0.02 * expected)
    assert output.width == 64 and output.height == 64
    assert output.has_audio is True
    # integrity digest matches the actual bytes
    assert hashlib.sha256(mp4.read_bytes()).hexdigest() == output.sha256
    # RunState reached succeeded
    record = run.stage("production_render")
    assert record.status.value == "succeeded"
    assert record.attempts == 1


def test_independent_ffprobe_validates_production_output(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)
    result = run_production_render_stage(timeline, run, registry, renderer=make_renderer())
    assert result.ok

    mp4 = tmp_path / "run" / result.output.path
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(mp4)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0
    probe = json.loads(proc.stdout)
    streams = probe["streams"]
    video = next(s for s in streams if s["codec_type"] == "video")
    assert video["codec_name"] == "h264"
    assert video["width"] == 64 and video["height"] == 64
    audio = next((s for s in streams if s["codec_type"] == "audio"), None)
    assert audio is not None and audio["codec_name"] == "aac"
    assert float(probe["format"]["duration"]) > 0


def test_scene_order_and_coverage_via_frame_colors(tmp_path):
    """Lightweight coverage/order proof: extract one frame at each scene's
    midpoint and verify its dominant color matches that scene's fixture
    (no computer vision — raw RGB sampling via ffmpeg + stdlib)."""
    timeline, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)
    result = run_production_render_stage(timeline, run, registry, renderer=make_renderer())
    assert result.ok
    mp4 = tmp_path / "run" / result.output.path
    frame_path = tmp_path / "frame.raw"

    for scene in timeline.scenes:
        midpoint = scene.start_seconds + scene.duration_seconds / 2
        extract = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(midpoint), "-i", str(mp4),
             "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", str(frame_path)],
            capture_output=True, text=True, timeout=30,
        )
        assert extract.returncode == 0, extract.stderr[-200:]
        raw = frame_path.read_bytes()
        assert len(raw) == 64 * 64 * 3
        # sample the center pixel; allow small codec color variance
        center = (32 * 64 + 32) * 3
        got = (raw[center], raw[center + 1], raw[center + 2])
        expected = EXPECTED_COLORS[scene.scene_id]
        assert all(abs(g - e) <= 24 for g, e in zip(got, expected)), (
            f"{scene.scene_id}: expected ~{expected}, got {got}"
        )


# ---- artifact registration + idempotency ---------------------------------------


def test_production_artifact_registered_and_reloadable(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)
    result = run_production_render_stage(timeline, run, registry, renderer=make_renderer())
    assert result.ok
    ref = result.artifact
    assert ref.kind is ArtifactKind.RENDERED_VIDEO
    assert ref.metadata == {
        "production_id": timeline.production_id,
        "renderer": "ffmpeg-smoke",
        "scene_count": 5,
        "duration_seconds": result.output.duration_seconds,
        "width": 64,
        "height": 64,
        "sha256": result.output.sha256,
    }
    reloaded_registry = ArtifactRegistry.load(tmp_path / "run", run_id)
    ref2 = reloaded_registry.require(ref.artifact_id)
    assert ref2.kind is ArtifactKind.RENDERED_VIDEO
    assert (tmp_path / "run" / ref2.path).is_file()


def test_idempotent_rerun_reuses_production_artifact(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    first = run_production_render_stage(timeline, make_run(run_id), registry, renderer=make_renderer())
    assert first.ok
    second = run_production_render_stage(timeline, make_run(run_id), registry, renderer=make_renderer())
    assert second.ok
    assert second.artifact.artifact_id == first.artifact.artifact_id
    prod_refs = [a for a in registry.all() if a.kind is ArtifactKind.RENDERED_VIDEO]
    assert len(prod_refs) == 1


def test_stale_production_output_is_not_accepted(tmp_path):
    """A corrupt existing production output must not be accepted as success —
    the ffprobe staleness check triggers a fresh render."""
    timeline, registry, run_id = full_chain(tmp_path)
    # first render (valid)
    first = run_production_render_stage(timeline, make_run(run_id), registry, renderer=make_renderer())
    assert first.ok
    # corrupt the production output file
    mp4 = tmp_path / "run" / first.output.path
    mp4.write_bytes(b"corrupted bytes, not an mp4")
    # rerun: staleness detected → re-render → fresh valid artifact
    second = run_production_render_stage(timeline, make_run(run_id), registry, renderer=make_renderer())
    assert second.ok
    mp4_after = tmp_path / "run" / second.output.path
    assert mp4_after.is_file()
    # the re-rendered file is valid media again
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", str(mp4_after)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0  # the re-rendered file is valid media
    prod_refs = [a for a in registry.all() if a.kind is ArtifactKind.RENDERED_VIDEO and a.stage == "production_render"]
    assert len(prod_refs) == 1  # same path, re-validated (or replaced), no duplicates

# ---- state lifecycle -------------------------------------------------------------


def test_retry_after_failure_is_possible(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)
    # first attempt: unhealthy renderer → failed
    bad = FFmpegRenderer(Config(
        ffmpeg_path=str(tmp_path / "no-ffmpeg.exe"),
        ffprobe_path=str(tmp_path / "no-ffprobe.exe"),
    ))
    first = run_production_render_stage(timeline, run, registry, renderer=bad)
    assert first.ok is False
    assert run.stage("production_render").status.value == "failed"
    # retry with a healthy renderer: failed → running → succeeded
    second = run_production_render_stage(timeline, run, registry, renderer=make_renderer())
    assert second.ok is True
    assert run.stage("production_render").status.value == "succeeded"
    assert run.stage("production_render").attempts == 2


def test_rerun_after_success_is_rejected_by_state_machine(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)
    first = run_production_render_stage(timeline, run, registry, renderer=make_renderer())
    assert first.ok
    # succeeded is terminal: the same RunState cannot run the stage again
    second = run_production_render_stage(timeline, run, registry, renderer=make_renderer())
    assert second.ok is False
    assert run.stage("production_render").status.value == "succeeded"  # unchanged


# ---- failure modes -----------------------------------------------------------------


def test_unhealthy_renderer_fails_before_render(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)
    bad = FFmpegRenderer(Config(
        ffmpeg_path=str(tmp_path / "no-ffmpeg.exe"),
        ffprobe_path=str(tmp_path / "no-ffprobe.exe"),
    ))
    result = run_production_render_stage(timeline, run, registry, renderer=bad)
    assert result.ok is False
    assert result.renderer == "ffmpeg-smoke"
    assert "unhealthy" in result.error
    assert run.stage("production_render").status.value == "failed"
    assert [a for a in registry.all() if a.kind is ArtifactKind.RENDERED_VIDEO] == []


def test_missing_source_fails_without_false_success(tmp_path):
    timeline, _, _ = full_chain(tmp_path)
    run = make_run()
    # an empty run dir: the timeline references sources that do not exist there
    empty_registry = ArtifactRegistry(tmp_path / "empty-run", run.run_id)
    result = run_production_render_stage(timeline, run, empty_registry, renderer=make_renderer())
    assert result.ok is False
    assert "does not exist in the run directory" in result.error
    assert run.stage("production_render").status.value == "failed"
    assert [a for a in empty_registry.all() if a.kind is ArtifactKind.RENDERED_VIDEO] == []


def test_invalid_timeline_input_fails(tmp_path):
    run = make_run()
    registry = ArtifactRegistry(tmp_path / "run", run.run_id)
    result = run_production_render_stage(
        {"schema_version": "9.0"}, run, registry, renderer=make_renderer()
    )
    assert result.ok is False
    assert run.stage("production_render").status.value == "failed"
    assert result.output is None


def test_persistence_failure_does_not_fake_success(tmp_path):
    timeline, _, _ = full_chain(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    run = make_run()
    registry = ArtifactRegistry(blocker / "run", run.run_id)
    result = run_production_render_stage(timeline, run, registry, renderer=make_renderer())
    assert result.ok is False
    assert result.output is None
    assert run.stage("production_render").status.value == "failed"


# ---- observability -----------------------------------------------------------------


def test_stage_logs_structured_events(tmp_path):
    stream = io.StringIO()
    log = StructuredLogger(level="INFO", stream=stream)
    timeline, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)

    result = run_production_render_stage(
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
    assert started["stage"] == "production_render"
    assert started["renderer"] == "ffmpeg-smoke"
    completed = records[events.index("ffmpeg_process_completed")]
    assert completed["duration_seconds"] is not None
    assert completed["has_audio"] is True

    # failure event with identity
    stream = io.StringIO()
    fail_run = make_run()
    fail_registry = ArtifactRegistry(tmp_path / "fail-run", fail_run.run_id)
    failed = run_production_render_stage(
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
    assert "production_render_stage_failed" in fail_events
    failed_rec = fail_records[fail_events.index("production_render_stage_failed")]
    assert failed_rec["stage"] == "production_render"
    assert "error" in failed_rec



