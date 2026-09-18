"""P5.5 technical media QA tests.

Covers: the real six-stage chain (P1-B→P2→P3→P4→P5→P5.5) with the actual
P5 production MP4, the QA report contract, artifact registration/reload,
idempotency (incl. stale-report and changed-render re-execution), state
lifecycle (retry/terminal), and failure paths (missing artifact, corrupt
MP4, missing video/audio stream, duration mismatch, invalid manifests —
no false PASS anywhere).
"""

import hashlib
import io
import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from ayce.artifacts import ArtifactKind, ArtifactRegistry
from ayce.asset_resolution import FileBackedAssetProvider, run_asset_resolution_stage
from ayce.config import Config
from ayce.ids import new_job_id, new_run_id
from ayce.logging import StructuredLogger
from ayce.media_qa import (
    QA_REPORT_FILENAME,
    QACheckStatus,
    QAVerdict,
    QAReport,
    run_media_qa_stage,
)
from ayce.narration_audio import load_narration_manifest
from ayce.render import FFmpegRenderer, run_production_render_stage
from ayce.script_to_scene import load_script_input, run_script_to_scene_stage
from ayce.narration_audio import (
    FileBackedNarrationProvider,
    load_narration_manifest,
    run_narration_audio_stage,
)
from ayce.render import FFmpegRenderer, run_production_render_stage
from ayce.script_to_scene import load_script_input, run_script_to_scene_stage
from ayce.state import RunState
from ayce.timeline import run_timeline_stage

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def make_run(run_id: str | None = None) -> RunState:
    return RunState(run_id=run_id or new_run_id(), job_id=new_job_id())


def make_config() -> Config:
    return Config.from_env(env={})


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


def render_production(tmp_path, timeline, registry, run_id):
    """Run the real P5 production render; return (run_state, render_ref)."""
    run = make_run(run_id)
    result = run_production_render_stage(
        timeline, run, registry, renderer=FFmpegRenderer(make_config())
    )
    assert result.ok, result.error
    return run, result.artifact


def narration_manifest(tmp_path, run_id: str):
    return load_narration_manifest(tmp_path / "run" / "narration_manifest.json")


def render_ref_of(registry: ArtifactRegistry):
    refs = [
        a for a in registry.all()
        if a.kind is ArtifactKind.RENDERED_VIDEO and a.stage == "production_render"
    ]
    assert len(refs) == 1
    return refs[0]


def qa_refs(registry: ArtifactRegistry):
    return [a for a in registry.all() if a.kind is ArtifactKind.QA_REPORT]


def encode(*ffmpeg_args: str) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", *ffmpeg_args],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-300:]


# ---- happy path: real P1-B→P2→P3→P4→P5→P5.5 -------------------------------------


def test_media_qa_passes_on_real_production_render(tmp_path):
    """The real six-stage chain: the actual P5 MP4 is independently verified."""
    timeline, registry, run_id = full_chain(tmp_path)
    _, render_ref = render_production(tmp_path, timeline, registry, run_id)
    run = make_run(run_id)

    result = run_media_qa_stage(
        render_ref, timeline, narration_manifest(tmp_path, run_id),
        run, registry, make_config(),
    )

    assert result.ok, result.error
    assert result.verdict == "PASS"
    assert result.reused is False
    assert result.production_id == timeline.production_id
    assert run.stage("media_qa").status.value == "succeeded"

    report = result.report
    assert isinstance(report, QAReport)
    assert report.verdict is QAVerdict.PASS
    assert report.schema_version == "1.0"
    assert report.production_id == timeline.production_id
    assert report.render_artifact.artifact_id == render_ref.artifact_id
    assert report.render_artifact.path == render_ref.path
    # the report is bound to the render's actual content identity
    mp4 = tmp_path / "run" / render_ref.path
    assert report.render_sha256 == hashlib.sha256(mp4.read_bytes()).hexdigest()
    # summary tallies agree with the deterministic verdict rule
    assert report.summary.failed == 0
    assert report.summary.total_checks == len(report.checks)
    assert all(c.status is QACheckStatus.PASS for c in report.checks)

    # every QA category is represented
    categories = {c.category for c in report.checks}
    assert categories == {"file", "container", "video", "audio", "duration", "scene_coverage"}

    # duration check records the auditable P5 tolerance values
    duration_check = next(
        c for c in report.checks if c.check_id == "duration.matches_timeline"
    )
    tolerance = 0.5 + 0.02 * timeline.total_duration_seconds
    assert duration_check.expected == f"{timeline.total_duration_seconds}s ± {tolerance:.2f}s"

    # scene coverage evidence is structured per scene
    assert [e.scene_id for e in report.scene_coverage] == [
        s.scene_id for s in timeline.scenes
    ]
    for evidence in report.scene_coverage:
        assert evidence.status is QACheckStatus.PASS
        assert evidence.sampled_color is not None


def test_qa_report_is_persisted_and_registered(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    _, render_ref = render_production(tmp_path, timeline, registry, run_id)
    result = run_media_qa_stage(
        render_ref, timeline, narration_manifest(tmp_path, run_id),
        make_run(run_id), registry, make_config(),
    )
    assert result.ok

    ref = result.artifact
    assert ref.kind is ArtifactKind.QA_REPORT
    assert ref.stage == "media_qa"
    assert ref.path == QA_REPORT_FILENAME

    report_file = tmp_path / "run" / QA_REPORT_FILENAME
    assert report_file.is_file()
    reloaded = QAReport.model_validate_json(report_file.read_text(encoding="utf-8"))
    assert reloaded == result.report  # deterministic JSON round-trip

    # registry metadata carries the identity binding + verdict
    assert ref.metadata["production_id"] == timeline.production_id
    assert ref.metadata["verdict"] == "PASS"
    assert ref.metadata["render_artifact_id"] == render_ref.artifact_id
    assert ref.metadata["render_sha256"] == result.report.render_sha256
    assert ref.metadata["failed_checks"] == 0

    # artifact references, not filesystem paths
    reloaded_registry = ArtifactRegistry.load(tmp_path / "run", run_id)
    ref2 = reloaded_registry.require(ref.artifact_id)
    assert ref2.kind is ArtifactKind.QA_REPORT
    assert len(qa_refs(reloaded_registry)) == 1


def test_qa_report_schema_version_is_major_pinned():
    base = dict(
        production_id="prod-1",
        render_artifact={
            "artifact_id": "a-1", "stage": "production_render",
            "kind": "rendered_video", "path": "render/p.mp4",
            "sha256": "0" * 64,
        },
        render_sha256="0" * 64,
        timeline_manifest="timeline.json",
        narration_manifest="narration_manifest.json",
        verdict="PASS",
        checks=[{
            "check_id": "file.exists", "category": "file",
            "status": "PASS", "message": "ok",
        }],
        summary={"total_checks": 1, "passed": 1, "failed": 0},
    )
    report = QAReport.model_validate(base)
    assert report.verdict is QAVerdict.PASS
    with pytest.raises(ValidationError):
        QAReport.model_validate({**base, "schema_version": "9.0"})
    with pytest.raises(ValidationError):
        # verdict is strictly PASS/FAIL — no scoring, no percentages
        QAReport.model_validate({**base, "verdict": "85%"})


# ---- execution failures (the stage cannot do its job) ----------------------------


def test_unregistered_render_artifact_is_execution_failure(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    _, render_ref = render_production(tmp_path, timeline, registry, run_id)

    # a rendered_video ref that is NOT registered in this run's registry
    other = ArtifactRegistry(tmp_path / "other-run", run_id)
    foreign_ref = other.register(
        "production_render", ArtifactKind.RENDERED_VIDEO, render_ref.path, {}
    )

    run = make_run(run_id)
    result = run_media_qa_stage(
        foreign_ref, timeline, narration_manifest(tmp_path, run_id),
        run, registry, make_config(),
    )
    assert result.ok is False
    assert result.artifact is None
    assert result.report is None
    assert "not registered" in result.error
    assert run.stage("media_qa").status.value == "failed"
    assert qa_refs(registry) == []  # no false PASS, no artifact published


def test_render_artifact_of_wrong_kind_is_execution_failure(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    scene_ref = next(
        a for a in registry.all() if a.kind is ArtifactKind.SCENE_MANIFEST
    )
    run = make_run()
    result = run_media_qa_stage(
        scene_ref, timeline, narration_manifest(tmp_path, run_id),
        run, registry, make_config(),
    )
    assert result.ok is False
    assert run.stage("media_qa").status.value == "failed"
    assert qa_refs(registry) == []


def test_invalid_timeline_is_execution_failure_not_false_pass(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    _, render_ref = render_production(tmp_path, timeline, registry, run_id)
    run = make_run()
    result = run_media_qa_stage(
        render_ref, {"schema_version": "9.0"},
        narration_manifest(tmp_path, run_id),
        run, registry, make_config(),
    )
    assert result.ok is False
    assert run.stage("media_qa").status.value == "failed"
    assert qa_refs(registry) == []


def test_missing_ffprobe_is_execution_failure(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    _, render_ref = render_production(tmp_path, timeline, registry, run_id)
    run = make_run(run_id)
    bad_config = Config(
        ffmpeg_path=str(tmp_path / "no-ffmpeg.exe"),
        ffprobe_path=str(tmp_path / "no-ffprobe.exe"),
    )
    result = run_media_qa_stage(
        render_ref, timeline, narration_manifest(tmp_path, run_id),
        run, registry, bad_config,
    )
    assert result.ok is False
    assert "ffprobe" in result.error
    assert run.stage("media_qa").status.value == "failed"
    assert qa_refs(registry) == []


# ---- QA FAIL semantics: media inspected, gate failed -----------------------------


def test_missing_media_file_fails_qa_without_false_pass(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    _, render_ref = render_production(tmp_path, timeline, registry, run_id)
    (tmp_path / "run" / render_ref.path).unlink()

    result = run_media_qa_stage(
        render_ref, timeline, narration_manifest(tmp_path, run_id),
        make_run(run_id), registry, make_config(),
    )
    assert result.ok is True  # the stage executed and produced evidence
    assert result.verdict == "FAIL"  # ...and the evidence is a truthful FAIL
    failed_ids = {c.check_id for c in result.report.checks if c.status is QACheckStatus.FAIL}
    assert {"file.exists", "file.non_empty", "container.ffprobe_parses"} <= failed_ids
    assert result.report.summary.failed > 0
    assert qa_refs(registry)  # the FAIL report IS registered as evidence


def test_corrupt_mp4_fails_qa_without_false_pass(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    _, render_ref = render_production(tmp_path, timeline, registry, run_id)
    # corrupt the actual production output bytes
    (tmp_path / "run" / render_ref.path).write_bytes(b"corrupted bytes, not an mp4")

    result = run_media_qa_stage(
        render_ref, timeline, narration_manifest(tmp_path, run_id),
        make_run(run_id), registry, make_config(),
    )
    assert result.verdict == "FAIL"
    failed_ids = {c.check_id for c in result.report.checks if c.status is QACheckStatus.FAIL}
    assert "container.ffprobe_parses" in failed_ids
    assert "video.stream_present" in failed_ids
    # scene coverage degrades to a truthful FAIL, never a skipped PASS
    assert all(
        e.status is QACheckStatus.FAIL for e in result.report.scene_coverage
    )
    assert run_stage_failed_checks(result) > 0


def run_stage_failed_checks(result):
    return result.report.summary.failed


def test_missing_video_stream_fails_qa(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    _, render_ref = render_production(tmp_path, timeline, registry, run_id)
    media = tmp_path / "run" / render_ref.path
    crafted = tmp_path / "audio_only.mp4"
    encode(
           "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
           "-c:a", "aac", str(crafted))
    media.write_bytes(crafted.read_bytes())

    result = run_media_qa_stage(
        render_ref, timeline, narration_manifest(tmp_path, run_id),
        make_run(run_id), registry, make_config(),
    )
    assert result.verdict == "FAIL"
    checks = {c.check_id: c for c in result.report.checks}
    assert checks["video.stream_present"].status is QACheckStatus.FAIL
    assert checks["video.stream_present"].actual == "no video stream"
    assert checks["audio.stream_present"].status is QACheckStatus.PASS
    assert checks["container.ffprobe_parses"].status is QACheckStatus.PASS


def test_missing_audio_stream_fails_qa(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    _, render_ref = render_production(tmp_path, timeline, registry, run_id)
    media = tmp_path / "run" / render_ref.path
    crafted = tmp_path / "video_only.mp4"
    encode(
           "-f", "lavfi", "-i", "color=c=red:s=64x64:d=2",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           str(crafted))
    media.write_bytes(crafted.read_bytes())

    result = run_media_qa_stage(
        render_ref, timeline, narration_manifest(tmp_path, run_id),
        make_run(run_id), registry, make_config(),
    )
    assert result.verdict == "FAIL"
    checks = {c.check_id: c for c in result.report.checks}
    assert checks["audio.stream_present"].status is QACheckStatus.FAIL
    assert checks["audio.stream_present"].actual == "no audio stream"
    assert checks["video.stream_present"].status is QACheckStatus.PASS
    assert checks["duration.matches_timeline"].status is QACheckStatus.FAIL


def test_duration_mismatch_fails_qa_with_recorded_values(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    _, render_ref = render_production(tmp_path, timeline, registry, run_id)
    media = tmp_path / "run" / render_ref.path
    crafted = tmp_path / "short.mp4"
    # a valid MP4 (video + audio) far outside the timeline tolerance
    encode(
           "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=2",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", str(crafted))
    media.write_bytes(crafted.read_bytes())

    result = run_media_qa_stage(
        render_ref, timeline, narration_manifest(tmp_path, run_id),
        make_run(run_id), registry, make_config(),
    )
    assert result.verdict == "FAIL"
    check = next(
        c for c in result.report.checks if c.check_id == "duration.matches_timeline"
    )
    assert check.status is QACheckStatus.FAIL
    assert check.expected.startswith(f"{timeline.total_duration_seconds}s ± ")
    assert check.actual.startswith("2.0") or check.actual.startswith("2")


# ---- idempotency + staleness ------------------------------------------------------


def test_idempotent_rerun_reuses_qa_artifact(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    _, render_ref = render_production(tmp_path, timeline, registry, run_id)

    first = run_media_qa_stage(
        render_ref, timeline, narration_manifest(tmp_path, run_id),
        make_run(run_id), registry, make_config(),
    )
    assert first.ok and first.verdict == "PASS"

    second = run_media_qa_stage(
        render_ref, timeline, narration_manifest(tmp_path, run_id),
        make_run(run_id), registry, make_config(),
    )
    assert second.ok and second.verdict == "PASS"
    assert second.reused is True  # no re-execution needed
    assert second.artifact.artifact_id == first.artifact.artifact_id
    assert len(qa_refs(registry)) == 1  # no duplicate registrations


def test_corrupt_qa_report_is_not_reused(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    _, render_ref = render_production(tmp_path, timeline, registry, run_id)
    first = run_media_qa_stage(
        render_ref, timeline, narration_manifest(tmp_path, run_id),
        make_run(run_id), registry, make_config(),
    )
    assert first.ok

    # corrupt the persisted report file — existence alone is not validity
    (tmp_path / "run" / QA_REPORT_FILENAME).write_text("{not a report", encoding="utf-8")

    second = run_media_qa_stage(
        render_ref, timeline, narration_manifest(tmp_path, run_id),
        make_run(run_id), registry, make_config(),
    )
    assert second.ok
    assert second.reused is False  # stale report re-executed
    assert second.verdict == "PASS"
    assert second.artifact.artifact_id != first.artifact.artifact_id
    assert second.report == QAReport.model_validate_json(
        (tmp_path / "run" / QA_REPORT_FILENAME).read_text(encoding="utf-8")
    )
    assert len(qa_refs(registry)) == 2  # a new report identity was produced


def test_changed_render_identity_is_not_reused(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    _, render_ref = render_production(tmp_path, timeline, registry, run_id)
    first = run_media_qa_stage(
        render_ref, timeline, narration_manifest(tmp_path, run_id),
        make_run(run_id), registry, make_config(),
    )
    assert first.verdict == "PASS"

    # the underlying render changed → the old report must not be accepted
    media = tmp_path / "run" / render_ref.path
    crafted = tmp_path / "short.mp4"
    encode(
           "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=2",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", str(crafted))
    media.write_bytes(crafted.read_bytes())

    second = run_media_qa_stage(
        render_ref, timeline, narration_manifest(tmp_path, run_id),
        make_run(run_id), registry, make_config(),
    )
    assert second.ok
    assert second.reused is False
    assert second.verdict == "FAIL"  # fresh evidence against the new bytes
    new_sha = hashlib.sha256(media.read_bytes()).hexdigest()
    assert second.report.render_sha256 == new_sha
    assert first.report.render_sha256 != new_sha


# ---- state lifecycle --------------------------------------------------------------


def test_retry_after_execution_failure_is_possible(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    _, render_ref = render_production(tmp_path, timeline, registry, run_id)
    run = make_run(run_id)
    bad_config = Config(
        ffmpeg_path=str(tmp_path / "no-ffmpeg.exe"),
        ffprobe_path=str(tmp_path / "no-ffprobe.exe"),
    )

    first = run_media_qa_stage(
        render_ref, timeline, narration_manifest(tmp_path, run_id),
        run, registry, bad_config,
    )
    assert first.ok is False
    assert run.stage("media_qa").status.value == "failed"

    second = run_media_qa_stage(
        render_ref, timeline, narration_manifest(tmp_path, run_id),
        run, registry, make_config(),
    )
    assert second.ok is True and second.verdict == "PASS"
    assert run.stage("media_qa").status.value == "succeeded"
    assert run.stage("media_qa").attempts == 2


def test_rerun_after_success_is_rejected_by_state_machine(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    _, render_ref = render_production(tmp_path, timeline, registry, run_id)
    run = make_run(run_id)
    first = run_media_qa_stage(
        render_ref, timeline, narration_manifest(tmp_path, run_id),
        run, registry, make_config(),
    )
    assert first.ok
    assert run.stage("media_qa").status.value == "succeeded"

    second = run_media_qa_stage(
        render_ref, timeline, narration_manifest(tmp_path, run_id),
        run, registry, make_config(),  # SAME RunState — terminal rerun
    )
    assert second.ok is False
    assert "invalid transition" in second.error
    # terminal state is preserved
    assert run.stage("media_qa").status.value == "succeeded"
    assert len(qa_refs(registry)) == 1


# ---- observability ----------------------------------------------------------------


def test_stage_logs_structured_events(tmp_path):
    timeline, registry, run_id = full_chain(tmp_path)
    _, render_ref = render_production(tmp_path, timeline, registry, run_id)
    stream = io.StringIO()
    log = StructuredLogger(level="INFO", stream=stream)

    result = run_media_qa_stage(
        render_ref, timeline, narration_manifest(tmp_path, run_id),
        make_run(run_id), registry, make_config(), logger=log,
    )
    assert result.ok

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    events = [r["message"] for r in records]
    assert "media_qa_stage_started" in events
    assert "media_qa_probe_completed" in events
    assert "media_qa_report_persisted" in events
    assert "media_qa_stage_completed" in events
    started = records[events.index("media_qa_stage_started")]
    assert started["run_id"] == run_id
    assert started["stage"] == "media_qa"
    completed = records[events.index("media_qa_stage_completed")]
    assert completed["verdict"] == "PASS"

    # failure event with identity
    stream = io.StringIO()
    fail_run = make_run()
    bad_config = Config(
        ffmpeg_path=str(tmp_path / "no-ffmpeg.exe"),
        ffprobe_path=str(tmp_path / "no-ffprobe.exe"),
    )
    failed = run_media_qa_stage(
        render_ref, timeline, narration_manifest(tmp_path, run_id),
        fail_run, registry, bad_config,
        logger=StructuredLogger(level="INFO", stream=stream),
    )
    assert failed.ok is False
    fail_records = [json.loads(line) for line in stream.getvalue().splitlines()]
    fail_events = [r["message"] for r in fail_records]
    assert "media_qa_stage_failed" in fail_events
    failed_rec = fail_records[fail_events.index("media_qa_stage_failed")]
    assert failed_rec["stage"] == "media_qa"
    assert "error" in failed_rec






