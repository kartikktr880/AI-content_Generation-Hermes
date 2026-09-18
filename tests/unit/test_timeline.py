"""P4 timeline stage tests.

Covers: temporal spine assembly, visual/narration timing, upstream
cross-validation (production identity, scene linkage, presence), contract
separation, determinism, persistence/registry/reload, state lifecycle,
failure paths (no false success), observability, and full regression.
"""

import io
import json
from pathlib import Path

import pytest

from ayce.artifacts import ArtifactKind, ArtifactRegistry
from ayce.asset_resolution import AssetManifest, FileBackedAssetProvider, run_asset_resolution_stage
from ayce.config import Config
from ayce.ids import new_job_id, new_run_id
from ayce.logging import StructuredLogger
from ayce.narration_audio import (
    FileBackedNarrationProvider,
    NarrationManifest,
    run_narration_audio_stage,
)
from ayce.scene_contract import ProductionManifest
from ayce.script_to_scene import load_script_input, run_script_to_scene_stage
from ayce.state import RunState
from ayce.timeline import (
    STAGE_NAME,
    TIMELINE_FILENAME,
    TimelineElementKind,
    TimelineError,
    TimelineManifest,
    dump_timeline_manifest,
    load_timeline_manifest,
    run_timeline_stage,
)
from ayce.config import Config as _Config

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def make_run(run_id: str | None = None) -> RunState:
    return RunState(run_id=run_id or new_run_id(), job_id=new_job_id())


def make_registry(tmp_path: Path, run_id: str) -> ArtifactRegistry:
    return ArtifactRegistry(tmp_path / "run", run_id)


from ayce.timeline import (
    STAGE_NAME,
    TIMELINE_FILENAME,
    TimelineElementKind,
    TimelineError,
    TimelineManifest,
    dump_timeline_manifest,
    load_timeline_manifest,
    run_timeline_stage,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def make_run(run_id: str | None = None) -> RunState:
    return RunState(run_id=run_id or new_run_id(), job_id=new_job_id())


def make_registry(tmp_path: Path, run_id: str) -> ArtifactRegistry:
    return ArtifactRegistry(tmp_path / "run", run_id)


def full_chain(tmp_path: Path, name: str = "documentary.json", run_id: str | None = None):
    """Execute the real P1-B -> P2 -> P3 pipeline in a temp run directory."""
    script = load_script_input(FIXTURES / "script_to_scene" / name)
    run_id = run_id or new_run_id()
    registry = make_registry(tmp_path, run_id)
    run = make_run(run_id)
    scene_result = run_script_to_scene_stage(script, run, registry)
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
    return scene_result, asset_result, narration_result, registry, run_id

# ---- A/B/C/D/E. golden-path timeline: timing, ordering, references ------------


def test_documentary_timeline_assembly(tmp_path):
    scene_result, asset_result, narration_result, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)

    result = run_timeline_stage(
        scene_manifest=scene_result.manifest,
        asset_manifest=asset_result.manifest,
        narration_manifest=narration_result.manifest,
        run_state=run,
        artifact_registry=registry,
    )
    assert result.ok, result.error
    manifest = result.manifest
    assert isinstance(manifest, TimelineManifest)
    # canonical sequence preserved; deterministic temporal spine
    assert [s.sequence for s in manifest.scenes] == [1, 2, 3, 4, 5]
    assert [s.start_seconds for s in manifest.scenes] == [0.0, 9.6, 14.8, 20.4, 28.4]
    assert manifest.total_duration_seconds == 34.0
    # visual elements occupy their scene intervals; narration uses truthful audio durations
    scene_ids = [s.scene_id for s in manifest.scenes]
    assert scene_ids == ["scene-001", "scene-002", "scene-003", "scene-004", "scene-005"]
    by_scene = {s.scene_id: s for s in manifest.scenes}
    visual_1 = next(e for e in by_scene["scene-001"].elements if e.kind is TimelineElementKind.VISUAL)
    assert visual_1.start_seconds == 0.0
    assert visual_1.duration_seconds == by_scene["scene-001"].duration_seconds
    narration_1 = next(e for e in by_scene["scene-001"].elements if e.kind is TimelineElementKind.NARRATION)
    assert narration_1.start_seconds == 0.0
    assert narration_1.duration_seconds == 0.1  # truthful P3 audio duration
    assert narration_1.source == "audio/scene-001.wav"
    assert visual_1.source == "assets/scene-001.png"
    # scene-004 declared no asset requirement → narration-only scene on the spine
    # element sources reference the P2/P3 resolved records
    for timeline_scene in manifest.scenes:
        for element in timeline_scene.elements:
            if element.kind is TimelineElementKind.VISUAL:
                assert element.source.startswith("assets/")
            else:
                assert element.source.startswith("audio/")


# ---- renderer neutrality -------------------------------------------------------


def test_timeline_is_renderer_neutral_and_run_relative(tmp_path):
    scene_result, asset_result, narration_result, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)
    result = run_timeline_stage(
        scene_manifest=scene_result.manifest,
        asset_manifest=asset_result.manifest,
        narration_manifest=narration_result.manifest,
        run_state=run,
        artifact_registry=registry,
    )
    assert result.ok
    serialized = dump_timeline_manifest(result.manifest)
    # no renderer instructions, no provider data, no absolute paths
    lowered = serialized.lower()
    assert "ffmpeg" not in lowered
    assert "remotion" not in lowered
    assert "otio" not in lowered
    assert "filtergraph" not in lowered
    assert "\\" not in serialized  # posix run-relative paths only
    assert "C:" not in serialized  # no Windows drive paths
    assert "/home/" not in serialized
    for element in (e for s in result.manifest.scenes for e in s.elements):
        assert not Path(element.source).is_absolute()
        assert ".." not in Path(element.source).parts


def test_scene_without_requirement_is_narration_only_on_spine(tmp_path):
    """scene-004 has no AssetRequirement: no visual element, but its scene
    interval still exists on the temporal spine (documented P4 behavior)."""
    scene_result, asset_result, narration_result, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)
    result = run_timeline_stage(
        scene_manifest=scene_result.manifest,
        asset_manifest=asset_result.manifest,
        narration_manifest=narration_result.manifest,
        run_state=run,
        artifact_registry=registry,
    )
    assert result.ok
    scene_4 = next(s for s in result.manifest.scenes if s.scene_id == "scene-004")
    kinds = {e.kind for e in scene_4.elements}
    assert TimelineElementKind.VISUAL not in kinds
    assert TimelineElementKind.NARRATION in kinds
    narration_4 = scene_4.elements[0]
    assert narration_4.start_seconds == scene_4.start_seconds
    assert narration_4.duration_seconds == 0.4


# ---- persistence / registry / idempotency --------------------------------------


def test_timeline_persisted_registered_and_reloadable(tmp_path):
    scene_result, asset_result, narration_result, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)
    result = run_timeline_stage(
        scene_manifest=scene_result.manifest,
        asset_manifest=asset_result.manifest,
        narration_manifest=narration_result.manifest,
        run_state=run,
        artifact_registry=registry,
    )
    assert result.ok
    ref = result.artifact
    assert ref.kind is ArtifactKind.TIMELINE
    assert ref.path == TIMELINE_FILENAME
    assert ref.metadata == {
        "schema_version": "1.0",
        "scenes": 5,
        "total_duration_seconds": 34.0,
    }
    assert (tmp_path / "run" / TIMELINE_FILENAME).is_file()

    reloaded_registry = ArtifactRegistry.load(tmp_path / "run", run_id)
    ref2 = reloaded_registry.require(ref.artifact_id)
    assert ref2.kind is ArtifactKind.TIMELINE
    reloaded = load_timeline_manifest(Path(tmp_path / "run") / ref2.path)
    assert isinstance(reloaded, TimelineManifest)
    assert reloaded == result.manifest


def test_idempotent_rerun_reuses_existing_artifact(tmp_path):
    scene_result, asset_result, narration_result, registry, run_id = full_chain(tmp_path)
    first = run_timeline_stage(
        scene_manifest=scene_result.manifest,
        asset_manifest=asset_result.manifest,
        narration_manifest=narration_result.manifest,
        run_state=make_run(run_id),
        artifact_registry=registry,
    )
    assert first.ok
    second = run_timeline_stage(
        scene_manifest=scene_result.manifest,
        asset_manifest=asset_result.manifest,
        narration_manifest=narration_result.manifest,
        run_state=make_run(run_id),
        artifact_registry=registry,
    )
    assert second.ok
    assert second.artifact.artifact_id == first.artifact.artifact_id
    timeline_refs = [a for a in registry.all() if a.kind is ArtifactKind.TIMELINE]
    assert len(timeline_refs) == 1

# ---- state lifecycle -------------------------------------------------------------


def test_state_lifecycle_success(tmp_path):
    scene_result, asset_result, narration_result, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)
    run_timeline_stage(
        scene_manifest=scene_result.manifest,
        asset_manifest=asset_result.manifest,
        narration_manifest=narration_result.manifest,
        run_state=run,
        artifact_registry=registry,
    )
    record = run.stage(STAGE_NAME)
    assert record.status.value == "succeeded"
    assert record.attempts == 1
    assert record.last_error is None


def test_rerun_after_success_is_rejected_by_state_machine(tmp_path):
    scene_result, asset_result, narration_result, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)
    first = run_timeline_stage(
        scene_manifest=scene_result.manifest,
        asset_manifest=asset_result.manifest,
        narration_manifest=narration_result.manifest,
        run_state=run,
        artifact_registry=registry,
    )
    assert first.ok
    second = run_timeline_stage(
        scene_manifest=scene_result.manifest,
        asset_manifest=asset_result.manifest,
        narration_manifest=narration_result.manifest,
        run_state=run,
        artifact_registry=registry,
    )
    assert second.ok is False
    assert run.stage(STAGE_NAME).status.value == "succeeded"  # unchanged


def test_retry_after_failure_is_possible(tmp_path):
    scene_result, asset_result, narration_result, registry, run_id = full_chain(tmp_path)
    # first attempt: production-id mismatch → failed
    run = make_run(run_id)
    mismatched_assets = asset_result.manifest.model_copy(update={"production_id": "job-other"})
    first = run_timeline_stage(
        scene_manifest=scene_result.manifest,
        asset_manifest=mismatched_assets,
        narration_manifest=narration_result.manifest,
        run_state=run,
        artifact_registry=registry,
    )
    assert first.ok is False
    assert run.stage(STAGE_NAME).status.value == "failed"
    # retry with consistent manifests: failed → running → succeeded
    second = run_timeline_stage(
        scene_manifest=scene_result.manifest,
        asset_manifest=asset_result.manifest,
        narration_manifest=narration_result.manifest,
        run_state=run,
        artifact_registry=registry,
    )
    assert second.ok is True
    assert run.stage(STAGE_NAME).status.value == "succeeded"
    assert run.stage(STAGE_NAME).attempts == 2

# ---- failure modes: upstream cross-validation ------------------------------------


def test_production_id_mismatch_fails(tmp_path):
    scene_result, asset_result, narration_result, registry, _ = full_chain(tmp_path)
    run = make_run()
    registry2 = make_registry(tmp_path / "r2", run.run_id)
    mismatched = narration_result.manifest.model_copy(update={"production_id": "job-other"})
    result = run_timeline_stage(
        scene_manifest=scene_result.manifest,
        asset_manifest=asset_result.manifest,
        narration_manifest=mismatched,
        run_state=run,
        artifact_registry=registry2,
    )
    assert result.ok is False
    assert "production_id" in result.error
    assert run.stage(STAGE_NAME).status.value == "failed"
    assert [a for a in registry2.all() if a.kind is ArtifactKind.TIMELINE] == []


def test_missing_asset_fails(tmp_path):
    scene_result, asset_result, narration_result, registry, _ = full_chain(tmp_path)
    run = make_run()
    registry2 = make_registry(tmp_path / "r2", run.run_id)
    # strip the scene-003 asset record → scene-003 requires it → fail
    stripped = AssetManifest(
        production_id=asset_result.manifest.production_id,
        provider=asset_result.manifest.provider,
        scene_manifest=asset_result.manifest.scene_manifest,
        resolved_assets=tuple(
            a for a in asset_result.manifest.resolved_assets if a.scene_id != "scene-003"
        ),
    )
    result = run_timeline_stage(
        scene_manifest=scene_result.manifest,
        asset_manifest=stripped,
        narration_manifest=narration_result.manifest,
        run_state=run,
        artifact_registry=registry2,
    )
    assert result.ok is False
    assert "scene-003" in result.error
    assert run.stage(STAGE_NAME).status.value == "failed"
    assert [a for a in registry2.all() if a.kind is ArtifactKind.TIMELINE] == []


def test_missing_narration_fails(tmp_path):
    scene_result, asset_result, narration_result, registry, _ = full_chain(tmp_path)
    run = make_run()
    registry2 = make_registry(tmp_path / "r2", run.run_id)
    stripped = NarrationManifest(
        production_id=narration_result.manifest.production_id,
        provider=narration_result.manifest.provider,
        scene_manifest=narration_result.manifest.scene_manifest,
        narration_audio=tuple(
            a for a in narration_result.manifest.narration_audio if a.scene_id != "scene-002"
        ),
    )
    result = run_timeline_stage(
        scene_manifest=scene_result.manifest,
        asset_manifest=asset_result.manifest,
        narration_manifest=stripped,
        run_state=run,
        artifact_registry=registry2,
    )
    assert result.ok is False
    assert "scene-002" in result.error
    assert run.stage(STAGE_NAME).status.value == "failed"


def test_invalid_upstream_manifest_fails_before_composition(tmp_path):
    scene_result, asset_result, narration_result, registry, _ = full_chain(tmp_path)
    run = make_run()
    result = run_timeline_stage(
        scene_manifest={"schema_version": "9.0"},
        asset_manifest=asset_result.manifest,
        narration_manifest=narration_result.manifest,
        run_state=run,
        artifact_registry=registry,
    )
    assert result.ok is False
    assert run.stage(STAGE_NAME).status.value == "failed"
    assert result.manifest is None

# ---- narration duration policy -----------------------------------------------------


def test_narration_longer_than_scene_fails(tmp_path):
    """Overflow policy: narration audio longer than its scene → explicit
    failure (no stretch/trim/overflow semantics at MVP)."""
    scene_result, asset_result, narration_result, registry, _ = full_chain(tmp_path)
    run = make_run()
    registry2 = make_registry(tmp_path / "r2", run.run_id)
    # rebuild a scene manifest with a scene duration shorter than the narration
    from ayce.script_to_scene import build_manifest, ScriptInput, SceneInput

    tiny = build_manifest(
        ScriptInput(
            production_id="job-x",
            title="tiny scene",
            scenes=(SceneInput(narration_text="hello", visual_description="v", duration_seconds=0.05),),
        )
    )
    # tiny manifest has scene-001; rebuild asset+narration for the same scenes
    tiny_assets = AssetManifest(
        production_id="job-x",
        provider="file-backed-fixtures",
        scene_manifest="scene_manifest.json",
        resolved_assets=(),
    )
    tiny_narration = NarrationManifest(
        production_id="job-x",
        provider="file-backed-narration-fixtures",
        scene_manifest="scene_manifest.json",
        narration_audio=(
            narration_result.manifest.narration_audio[0],  # 0.1s > 0.05s scene
        ),
    )
    result = run_timeline_stage(
        scene_manifest=tiny,
        asset_manifest=tiny_assets,
        narration_manifest=tiny_narration,
        run_state=run,
        artifact_registry=registry2,
    )
    assert result.ok is False
    assert "exceeds the scene duration" in result.error
    assert run.stage(STAGE_NAME).status.value == "failed"


def test_narration_shorter_than_scene_is_allowed(tmp_path):
    """Underflow policy: narration shorter than its scene is fine — the
    narration keeps its truthful duration and the rest of the scene is
    visual-only time."""
    scene_result, asset_result, narration_result, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)
    result = run_timeline_stage(
        scene_manifest=scene_result.manifest,
        asset_manifest=asset_result.manifest,
        narration_manifest=narration_result.manifest,
        run_state=run,
        artifact_registry=registry,
    )
    assert result.ok
    scene_1 = result.manifest.scenes[0]
    narration_element = next(
        e for e in scene_1.elements if e.kind is TimelineElementKind.NARRATION
    )
    assert narration_element.duration_seconds < scene_1.duration_seconds

# ---- contract separation + determinism -------------------------------------------


def test_upstream_manifests_are_not_mutated(tmp_path):
    scene_result, asset_result, narration_result, registry, run_id = full_chain(tmp_path)
    scenes_before = json.dumps(
        [s.model_dump() for s in scene_result.manifest.scenes], sort_keys=True
    )
    assets_before = json.dumps(
        [a.model_dump() for a in asset_result.manifest.resolved_assets], sort_keys=True
    )
    narration_before = json.dumps(
        [a.model_dump() for a in narration_result.manifest.narration_audio], sort_keys=True
    )
    run = make_run(run_id)
    result = run_timeline_stage(
        scene_manifest=scene_result.manifest,
        asset_manifest=asset_result.manifest,
        narration_manifest=narration_result.manifest,
        run_state=run,
        artifact_registry=registry,
    )
    assert result.ok
    # all three upstream manifests remain unchanged
    assert json.dumps([s.model_dump() for s in scene_result.manifest.scenes], sort_keys=True) == scenes_before
    assert json.dumps([a.model_dump() for a in asset_result.manifest.resolved_assets], sort_keys=True) == assets_before
    assert json.dumps([a.model_dump() for a in narration_result.manifest.narration_audio], sort_keys=True) == narration_before


def test_same_inputs_yield_byte_identical_timeline(tmp_path):
    timelines = []
    for index in range(2):
        scene_result, asset_result, narration_result, _, _ = full_chain(
            tmp_path / f"chain-{index}"
        )
        run = make_run()
        registry = make_registry(tmp_path / f"t-{index}", run.run_id)
        result = run_timeline_stage(
            scene_manifest=scene_result.manifest,
            asset_manifest=asset_result.manifest,
            narration_manifest=narration_result.manifest,
            run_state=run,
            artifact_registry=registry,
        )
        assert result.ok, result.error
        timelines.append(result.manifest)
    assert dump_timeline_manifest(timelines[0]) == dump_timeline_manifest(timelines[1])


# ---- observability ------------------------------------------------------------------


def test_stage_logs_structured_events(tmp_path):
    stream = io.StringIO()
    log = StructuredLogger(level="INFO", stream=stream)
    scene_result, asset_result, narration_result, registry, run_id = full_chain(tmp_path)
    run = make_run(run_id)

    result = run_timeline_stage(
        scene_manifest=scene_result.manifest,
        asset_manifest=asset_result.manifest,
        narration_manifest=narration_result.manifest,
        run_state=run,
        artifact_registry=registry,
        logger=log,
    )
    assert result.ok

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    events = [r["message"] for r in records]
    assert "timeline_stage_started" in events
    assert "timeline_validation_started" in events
    assert "timeline_built" in events
    assert "timeline_manifest_persisted" in events
    assert "timeline_stage_succeeded" in events
    started = records[events.index("timeline_stage_started")]
    assert started["run_id"] == run.run_id
    assert started["stage"] == STAGE_NAME
    built_rec = records[events.index("timeline_built")]
    assert "total_duration_seconds" in built_rec

    # failure event with identity
    stream = io.StringIO()
    fail_run = make_run()
    fail_registry = ArtifactRegistry(tmp_path / "fail-run", fail_run.run_id)
    failed = run_timeline_stage(
        scene_manifest={"schema_version": "9.0"},
        asset_manifest=asset_result.manifest,
        narration_manifest=narration_result.manifest,
        run_state=fail_run,
        artifact_registry=fail_registry,
        logger=StructuredLogger(level="INFO", stream=stream),
    )
    assert failed.ok is False
    fail_records = [json.loads(line) for line in stream.getvalue().splitlines()]
    fail_events = [r["message"] for r in fail_records]
    assert "timeline_stage_failed" in fail_events
    failed_rec = fail_records[fail_events.index("timeline_stage_failed")]
    assert failed_rec["stage"] == STAGE_NAME
    assert "error" in failed_rec


