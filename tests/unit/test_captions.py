"""P6.5 scene-level captions stage tests.

Covers: truthful cue construction from verified upstream contracts
(verbatim text, exact narration-element intervals), standard SubRip
serialization, byte-deterministic idempotency, stage lifecycle, fail-fast
failure paths, and schema versioning. Real upstream chain (P1-B → P4),
real fixture data — no mocks for the primary path.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from ayce.artifacts import ArtifactKind, ArtifactRegistry
from ayce.asset_resolution import FileBackedAssetProvider, run_asset_resolution_stage
from ayce.captions import (
    CAPTIONS_FILENAME,
    CAPTIONS_MANIFEST_SCHEMA_VERSION,
    SRT_FILENAME,
    STAGE_NAME,
    CaptionsError,
    CaptionsManifest,
    build_captions,
    dump_captions_manifest,
    dump_srt,
    format_srt_timestamp,
    load_captions_manifest,
    run_captions_stage,
)
from ayce.config import Config
from ayce.ids import new_run_id
from ayce.narration_audio import FileBackedNarrationProvider, run_narration_audio_stage
from ayce.script_to_scene import load_script_input, run_script_to_scene_stage
from ayce.state import RunState
from ayce.timeline import TimelineElementKind, run_timeline_stage

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def make_registry(tmp_path: Path, run_id: str):
    return ArtifactRegistry(tmp_path / "run", run_id)


def upstream_chain(tmp_path: Path, run_id: str | None = None):
    """Execute the real P1-B → P2 → P3 → P4 chain; return the manifests + registry."""
    run_id = run_id or new_run_id()
    registry = make_registry(tmp_path, run_id)
    run = RunState(run_id=run_id, job_id=f"job-{run_id}")
    config = Config.from_env(env={})
    scene_result = run_script_to_scene_stage(
        load_script_input(FIXTURES / "script_to_scene" / "documentary.json"),
        run, registry,
    )
    assert scene_result.ok, scene_result.error
    asset_result = run_asset_resolution_stage(
        scene_result.manifest, run, registry,
        provider=FileBackedAssetProvider(config),
    )
    assert asset_result.ok, asset_result.error
    narration_result = run_narration_audio_stage(
        scene_result.manifest, run, registry,
        provider=FileBackedNarrationProvider(config),
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
    return scene_result.manifest, timeline_result.manifest, narration_result.manifest, registry, run


# ---- truthful construction (the core contract) -----------------------------------


def test_captions_happy_path_truthful_cues(tmp_path):
    scene_manifest, timeline, narration, registry, run = upstream_chain(tmp_path)

    result = run_captions_stage(scene_manifest, timeline, narration, run, registry)

    assert result.ok, result.error
    assert result.error is None
    assert result.production_id == scene_manifest.production_id
    assert run.stage(STAGE_NAME).status.value == "succeeded"

    manifest = result.manifest
    assert manifest.schema_version == CAPTIONS_MANIFEST_SCHEMA_VERSION
    assert len(manifest.cues) == len(scene_manifest.scenes)

    timeline_by_scene = {s.scene_id: s for s in timeline.scenes}
    for scene, cue in zip(scene_manifest.scenes, manifest.cues):
        # text is VERBATIM contract narration — never rewritten
        assert cue.text == scene.narration.text
        # timing is the EXACT timeline narration-element interval
        element = next(
            e for e in timeline_by_scene[scene.scene_id].elements
            if e.kind is TimelineElementKind.NARRATION
        )
        assert cue.scene_id == scene.scene_id
        assert cue.start_seconds == element.start_seconds
        assert cue.end_seconds == element.start_seconds + element.duration_seconds
        # the cue covers the SPOKEN audio, not the full scene interval
        scene_end = timeline_by_scene[scene.scene_id].start_seconds \
            + timeline_by_scene[scene.scene_id].duration_seconds
        assert cue.end_seconds <= scene_end

    # artifact registered under the reserved kind, reloaded through the registry
    assert result.artifact.kind is ArtifactKind.CAPTIONS
    assert result.artifact.path == CAPTIONS_FILENAME
    reloaded_registry = ArtifactRegistry.load(tmp_path / "run", run.run_id)
    assert reloaded_registry.require(result.artifact.artifact_id) == result.artifact

    # both files exist on disk and the manifest round-trips
    run_dir = tmp_path / "run"
    assert (run_dir / CAPTIONS_FILENAME).is_file()
    assert (run_dir / SRT_FILENAME).is_file()
    assert load_captions_manifest(run_dir / CAPTIONS_FILENAME) == manifest


def test_srt_serialization_is_standard_subrip(tmp_path):
    scene_manifest, timeline, narration, registry, run = upstream_chain(tmp_path)
    result = run_captions_stage(scene_manifest, timeline, narration, run, registry)
    assert result.ok

    srt_text = (tmp_path / "run" / SRT_FILENAME).read_text(encoding="utf-8")
    assert srt_text == dump_srt(result.manifest)

    # parse it back: sequential indices, arrow timing lines, cue text bodies
    blocks = [b for b in srt_text.split("\n\n") if b.strip()]
    assert len(blocks) == len(result.manifest.cues)
    for block, cue in zip(blocks, result.manifest.cues):
        lines = block.strip("\n").split("\n")
        assert lines[0] == str(cue.sequence)
        assert " --> " in lines[1]
        start, end = lines[1].split(" --> ")
        assert start == format_srt_timestamp(cue.start_seconds)
        assert end == format_srt_timestamp(cue.end_seconds)
        assert "," in start and ":" in start  # SubRip HH:MM:SS,mmm shape
        assert lines[2] == cue.text


def test_srt_timestamp_formatting_rules():
    assert format_srt_timestamp(0.0) == "00:00:00,000"
    assert format_srt_timestamp(1.0) == "00:00:01,000"
    assert format_srt_timestamp(3661.0) == "01:01:01,000"
    # documented HALF-UP millisecond rounding
    assert format_srt_timestamp(1.2345) == "00:00:01,235"
    assert format_srt_timestamp(1.2344) == "00:00:01,234"
    with pytest.raises(CaptionsError, match="negative"):
        format_srt_timestamp(-0.001)
    with pytest.raises(CaptionsError, match="limit"):
        format_srt_timestamp(100 * 3600)


# ---- byte-deterministic idempotency ----------------------------------------------


def test_captions_idempotent_rerun_reuses_artifact(tmp_path):
    scene_manifest, timeline, narration, registry, run = upstream_chain(tmp_path)

    first = run_captions_stage(scene_manifest, timeline, narration, run, registry)
    assert first.ok
    # the first RunState is terminal-succeeded; a rerun uses a fresh RunState
    rerun = RunState(run_id=run.run_id, job_id=run.job_id)
    second = run_captions_stage(scene_manifest, timeline, narration, rerun, registry)

    assert second.ok
    assert second.artifact.artifact_id == first.artifact.artifact_id
    assert second.manifest == first.manifest
    refs = [a for a in registry.all() if a.kind is ArtifactKind.CAPTIONS]
    assert len(refs) == 1  # no duplicate registration


def test_missing_srt_restored_on_idempotent_reuse(tmp_path):
    scene_manifest, timeline, narration, registry, run = upstream_chain(tmp_path)
    first = run_captions_stage(scene_manifest, timeline, narration, run, registry)
    assert first.ok
    (tmp_path / "run" / SRT_FILENAME).unlink()

    rerun = RunState(run_id=run.run_id, job_id=run.job_id)
    second = run_captions_stage(scene_manifest, timeline, narration, rerun, registry)

    assert second.ok and second.artifact.artifact_id == first.artifact.artifact_id
    assert (tmp_path / "run" / SRT_FILENAME).is_file()


# ---- fail-fast failure paths ------------------------------------------------------


def test_production_id_mismatch_fails(tmp_path):
    scene_manifest, timeline, narration, registry, run = upstream_chain(tmp_path)
    tampered = narration.model_copy(update={"production_id": "job-somewhere-else"})

    result = run_captions_stage(scene_manifest, timeline, tampered, run, registry)

    assert result.ok is False
    assert "production_id mismatch" in result.error
    assert run.stage(STAGE_NAME).status.value == "failed"
    kinds = [a.kind for a in registry.all()]
    assert ArtifactKind.CAPTIONS not in kinds
    assert not (tmp_path / "run" / CAPTIONS_FILENAME).exists()
    assert not (tmp_path / "run" / SRT_FILENAME).exists()


def test_missing_timeline_scene_fails(tmp_path):
    scene_manifest, timeline, narration, registry, run = upstream_chain(tmp_path)
    truncated = timeline.model_copy(update={"scenes": timeline.scenes[:-1]})

    result = run_captions_stage(scene_manifest, truncated, narration, run, registry)

    assert result.ok is False
    assert "missing timeline scene" in result.error
    assert run.stage(STAGE_NAME).status.value == "failed"
    assert ArtifactKind.CAPTIONS not in [a.kind for a in registry.all()]


def test_raw_dict_inputs_are_validated(tmp_path):
    scene_manifest, timeline, narration, registry, run = upstream_chain(tmp_path)
    result = run_captions_stage(
        scene_manifest.model_dump(mode="json"),
        timeline.model_dump(mode="json"),
        narration.model_dump(mode="json"),
        run, registry,
    )
    assert result.ok, result.error


# ---- schema versioning / determinism ----------------------------------------------


def test_captions_schema_version_pin():
    base = {
        "production_id": "job-1",
        "scene_manifest": "scene_manifest.json",
        "timeline": "timeline.json",
        "narration_manifest": "narration_manifest.json",
        "cues": [{
            "scene_id": "scene-001", "sequence": 1,
            "start_seconds": 0.0, "end_seconds": 1.0, "text": "hello",
        }],
    }
    with pytest.raises(ValidationError):
        CaptionsManifest.model_validate({**base, "schema_version": "9.0"})
    with pytest.raises(ValidationError):  # overlapping cues rejected
        CaptionsManifest.model_validate({**base, "cues": [
            {"scene_id": "scene-001", "sequence": 1,
             "start_seconds": 0.0, "end_seconds": 2.0, "text": "a"},
            {"scene_id": "scene-002", "sequence": 2,
             "start_seconds": 1.0, "end_seconds": 3.0, "text": "b"},
        ]})
    with pytest.raises(ValidationError):  # gap in sequence numbering rejected
        CaptionsManifest.model_validate({**base, "cues": [
            {"scene_id": "scene-001", "sequence": 1,
             "start_seconds": 0.0, "end_seconds": 1.0, "text": "a"},
            {"scene_id": "scene-002", "sequence": 3,
             "start_seconds": 1.0, "end_seconds": 2.0, "text": "b"},
        ]})


def test_dump_is_byte_deterministic(tmp_path):
    scene_manifest, timeline, narration, _, _ = upstream_chain(tmp_path)
    first = build_captions(scene_manifest, timeline, narration)
    second = build_captions(scene_manifest, timeline, narration)
    assert dump_captions_manifest(first) == dump_captions_manifest(second)
    assert dump_srt(first) == dump_srt(second)
