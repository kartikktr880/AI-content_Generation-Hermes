"""P6 Golden Path pipeline runner tests.

Covers: real end-to-end happy path (script → QA report, real FFmpeg,
no mocks), failure propagation (unhealthy renderer; media_qa never
executed), QA-FAIL ≠ pipeline-failure semantics, artifact completeness
through the real registry, and run isolation/idempotency policy.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ayce.artifacts import ArtifactKind, ArtifactRegistry
from ayce.captions import CAPTIONS_FILENAME, SRT_FILENAME
from ayce.config import Config
from ayce.media_qa import QA_REPORT_FILENAME
from ayce.pipeline import PIPELINE_STAGES, STATE_FILENAME, run_pipeline
from ayce.render import FFmpegRenderer
from ayce.state import RunState

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SCRIPT = FIXTURES / "script_to_scene" / "documentary.json"
ASSETS_DIR = FIXTURES / "asset_provider"
NARRATION_DIR = FIXTURES / "narration_fixtures"

EXPECTED_KINDS = {
    ArtifactKind.SCENE_MANIFEST,
    ArtifactKind.ASSET_MANIFEST,
    ArtifactKind.AUDIO,
    ArtifactKind.TIMELINE,
    ArtifactKind.CAPTIONS,
    ArtifactKind.RENDERED_VIDEO,
    ArtifactKind.QA_REPORT,
}


def make_config(tmp_path: Path) -> Config:
    """Real config with an isolated data dir; everything else default."""
    return replace(Config.from_env(env={}), data_dir=tmp_path / "data")


# ---- 1. happy path (real FFmpeg, no mocks) --------------------------------------


def test_pipeline_happy_path_end_to_end(tmp_path):
    config = make_config(tmp_path)

    result = run_pipeline(SCRIPT, config=config)

    assert result.ok, result.error
    assert result.failed_stage is None
    assert result.error is None
    assert [o.stage for o in result.stages] == list(PIPELINE_STAGES)
    assert all(o.ok for o in result.stages)
    assert result.qa_verdict == "PASS"
    assert result.production_id is not None
    assert result.rendered_artifact_id and result.qa_artifact_id

    # run directory location convention: <data_dir>/runs/<run_id>
    assert result.run_dir == config.resolved_data_dir / "runs" / result.run_id

    # state.json reloads; every stage succeeded in the durable checkpoint
    run = RunState.load(result.run_dir / STATE_FILENAME)
    for stage in PIPELINE_STAGES:
        assert run.stage(stage).status.value == "succeeded"

    # artifacts.json reloads through the real registry with all six kinds
    registry = ArtifactRegistry.load(result.run_dir, result.run_id)
    kinds = {ref.kind for ref in registry.all()}
    assert kinds == EXPECTED_KINDS

    # captions files exist in the run directory (captions stage output)
    assert (result.run_dir / CAPTIONS_FILENAME).is_file()
    assert (result.run_dir / SRT_FILENAME).is_file()
    # rendered MP4 exists on disk and is validated by the real renderer
    render_ref = next(
        ref for ref in registry.all() if ref.kind is ArtifactKind.RENDERED_VIDEO
    )
    media_path = result.run_dir / render_ref.path
    assert media_path.is_file()
    FFmpegRenderer(config).validate_output(media_path)  # raises RenderError if invalid

    # QA report reloads with a PASS verdict
    report = json.loads((result.run_dir / QA_REPORT_FILENAME).read_text(encoding="utf-8"))
    assert report["verdict"] == "PASS"
    qa_ref = registry.require(result.qa_artifact_id)
    assert qa_ref.kind is ArtifactKind.QA_REPORT
    assert qa_ref.path == QA_REPORT_FILENAME


# ---- 2. failure propagation ------------------------------------------------------


def test_pipeline_stops_at_failed_stage_and_media_qa_never_executes(tmp_path):
    config = make_config(tmp_path)
    unhealthy_renderer = FFmpegRenderer(Config(
        ffmpeg_path=str(tmp_path / "no-ffmpeg.exe"),
        ffprobe_path=str(tmp_path / "no-ffprobe.exe"),
    ))

    result = run_pipeline(SCRIPT, config=config, renderer=unhealthy_renderer)

    assert result.ok is False
    assert result.failed_stage == "production_render"
    assert result.error is not None

    executed = [o.stage for o in result.stages]
    assert executed == [
        "script_to_scene", "asset_resolution", "narration_audio",
        "timeline", "captions", "production_render",
    ]
    by_stage = {o.stage: o for o in result.stages}
    for stage in (
        "script_to_scene", "asset_resolution", "narration_audio",
        "timeline", "captions",
    ):
        assert by_stage[stage].ok is True
    assert by_stage["production_render"].ok is False

    # durable state: earlier stages succeeded, render failed, media_qa never ran
    run = RunState.load(result.run_dir / STATE_FILENAME)
    for stage in (
        "script_to_scene", "asset_resolution", "narration_audio",
        "timeline", "captions",
    ):
        assert run.stage(stage).status.value == "succeeded"
    assert run.stage("production_render").status.value == "failed"
    with pytest.raises(Exception):
        run.stage("media_qa")  # unknown stage → never executed

    # earlier artifacts remain persisted; no render/QA artifacts were faked
    registry = ArtifactRegistry.load(result.run_dir, result.run_id)
    kinds = {ref.kind for ref in registry.all()}
    assert kinds == {
        ArtifactKind.SCENE_MANIFEST,
        ArtifactKind.ASSET_MANIFEST,
        ArtifactKind.AUDIO,
        ArtifactKind.TIMELINE,
        ArtifactKind.CAPTIONS,
    }
    assert not (result.run_dir / QA_REPORT_FILENAME).exists()


def test_pipeline_stops_at_first_upstream_failure(tmp_path):
    config = make_config(tmp_path)
    missing_assets = tmp_path / "no-such-assets"

    result = run_pipeline(SCRIPT, config=config, assets_dir=missing_assets)

    assert result.ok is False
    assert result.failed_stage == "asset_resolution"
    assert [o.stage for o in result.stages] == ["script_to_scene", "asset_resolution"]
    # scene manifest artifact (earlier stage) remains persisted
    registry = ArtifactRegistry.load(result.run_dir, result.run_id)
    assert {ref.kind for ref in registry.all()} == {ArtifactKind.SCENE_MANIFEST}


# ---- 3. QA FAIL semantics (evidence ≠ pipeline failure) --------------------------


class _CorruptingRenderer(FFmpegRenderer):
    """Renders a valid production, then corrupts the bytes on disk AFTER
    the render stage's ffprobe validation — the same deterministic QA-FAIL
    construction proven by the P5.5 tests. The render stage still succeeds
    (the file exists and was validated at render time); the QA stage then
    truthfully inspects the corrupt bytes and reports verdict=FAIL."""

    def render_production(self, timeline_manifest, *, run_dir):
        output = super().render_production(timeline_manifest, run_dir=run_dir)
        (Path(run_dir) / output.path).write_bytes(b"corrupted bytes, not an mp4")
        return output


def test_pipeline_qa_fail_verdict_is_not_a_pipeline_failure(tmp_path):
    config = make_config(tmp_path)

    result = run_pipeline(SCRIPT, config=config, renderer=_CorruptingRenderer(config))

    # the QA stage EXECUTED and produced truthful evidence → pipeline succeeds
    assert result.ok, result.error
    assert result.qa_verdict == "FAIL"
    assert result.failed_stage is None
    assert [o.stage for o in result.stages] == list(PIPELINE_STAGES)
    assert all(o.ok for o in result.stages)

    run = RunState.load(result.run_dir / STATE_FILENAME)
    assert run.stage("media_qa").status.value == "succeeded"

    # the FAIL report is persisted and registered as real QA evidence
    registry = ArtifactRegistry.load(result.run_dir, result.run_id)
    kinds = {ref.kind for ref in registry.all()}
    assert kinds == EXPECTED_KINDS
    report = json.loads((result.run_dir / QA_REPORT_FILENAME).read_text(encoding="utf-8"))
    assert report["verdict"] == "FAIL"
    assert registry.require(result.qa_artifact_id).kind is ArtifactKind.QA_REPORT


# ---- 4. artifact completeness (registry-level, not filename-level) ---------------


def test_pipeline_artifact_completeness_and_reload(tmp_path):
    config = make_config(tmp_path)

    result = run_pipeline(SCRIPT, config=config)
    assert result.ok

    reloaded = ArtifactRegistry.load(result.run_dir, result.run_id)
    kinds = sorted(ref.kind.value for ref in reloaded.all())
    assert sorted(k.value for k in EXPECTED_KINDS) == kinds

    # every reference round-trips through require() and points at a real file
    for ref in reloaded.all():
        assert reloaded.require(ref.artifact_id) == ref
        assert (result.run_dir / ref.path).is_file()

    # per-kind artifact provenance matches the verified producing-stage labels
    from ayce.asset_resolution import ARTIFACT_STAGE as ASSET_STAGE
    from ayce.captions import ARTIFACT_STAGE as CAPTIONS_STAGE
    from ayce.media_qa import ARTIFACT_STAGE as QA_STAGE
    from ayce.narration_audio import ARTIFACT_STAGE as AUDIO_STAGE
    from ayce.render import PRODUCTION_ARTIFACT_STAGE as RENDER_STAGE
    from ayce.script_to_scene import ARTIFACT_STAGE as SCENE_STAGE
    from ayce.timeline import ARTIFACT_STAGE as TIMELINE_STAGE

    stage_kinds = {ref.kind: ref.stage for ref in reloaded.all()}
    assert stage_kinds[ArtifactKind.SCENE_MANIFEST] == SCENE_STAGE
    assert stage_kinds[ArtifactKind.ASSET_MANIFEST] == ASSET_STAGE
    assert stage_kinds[ArtifactKind.AUDIO] == AUDIO_STAGE
    assert stage_kinds[ArtifactKind.TIMELINE] == TIMELINE_STAGE
    assert stage_kinds[ArtifactKind.CAPTIONS] == CAPTIONS_STAGE
    assert stage_kinds[ArtifactKind.RENDERED_VIDEO] == RENDER_STAGE
    assert stage_kinds[ArtifactKind.QA_REPORT] == QA_STAGE


# ---- 5. run isolation / idempotency policy ---------------------------------------


def test_pipeline_each_invocation_creates_isolated_run(tmp_path):
    config = make_config(tmp_path)

    first = run_pipeline(SCRIPT, config=config)
    second = run_pipeline(SCRIPT, config=config)

    assert first.ok and second.ok
    # new run identity per invocation; run A is never overwritten by run B
    assert first.run_id != second.run_id
    assert first.run_dir != second.run_dir
    assert first.run_dir.is_dir() and second.run_dir.is_dir()

    # both runs remain independently valid and complete
    for r in (first, second):
        registry = ArtifactRegistry.load(r.run_dir, r.run_id)
        assert {ref.kind for ref in registry.all()} == EXPECTED_KINDS
        run = RunState.load(r.run_dir / STATE_FILENAME)
        for stage in PIPELINE_STAGES:
            assert run.stage(stage).status.value == "succeeded"

    # artifact identities are fully independent between the two runs
    first_ids = {ref.artifact_id for ref in ArtifactRegistry.load(first.run_dir, first.run_id).all()}
    second_ids = {ref.artifact_id for ref in ArtifactRegistry.load(second.run_dir, second.run_id).all()}
    assert first_ids.isdisjoint(second_ids)
