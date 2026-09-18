"""P1-B Script → Scene Manifest stage tests.

Covers: golden path (state + artifact), determinism, duration/ID rules,
failure paths (no false success), idempotent rerun, structured logging
events, and full P0/P1-A regression.
"""

import io
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ayce.artifacts import ArtifactKind, ArtifactRegistry
from ayce.ids import new_job_id, new_run_id
from ayce.logging import StructuredLogger
from ayce.scene_contract import (
    AssetKind,
    ProductionManifest,
    dump_manifest,
    load_manifest,
    persist_manifest,
)
from ayce.script_to_scene import (
    ARTIFACT_STAGE,
    MIN_SCENE_DURATION_SECONDS,
    STAGE_NAME,
    WORDS_PER_SECOND,
    ScriptInput,
    SceneInput,
    build_manifest,
    estimate_scene_duration,
    load_script_input,
    run_script_to_scene_stage,
)
from ayce.state import RunState

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "script_to_scene"


def load_input(name: str) -> ScriptInput:
    return load_script_input(FIXTURES / name)


def make_run(run_id: str | None = None) -> RunState:
    return RunState(run_id=run_id or new_run_id(), job_id=new_job_id())


def make_registry(tmp_path: Path, run_id: str) -> ArtifactRegistry:
    return ArtifactRegistry(tmp_path / "run", run_id)


def scene(
    narration="A single narration line.",
    visual="A simple visual.",
    **overrides,
) -> SceneInput:
    data = {"narration_text": narration, "visual_description": visual}
    data.update(overrides)
    return SceneInput.model_validate(data)


# ---- A. happy path -----------------------------------------------------------


def test_documentary_golden_path(tmp_path):
    run = make_run()
    registry = make_registry(tmp_path, run.run_id)

    result = run_script_to_scene_stage(load_input("documentary.json"), run, registry)

    assert result.ok is True, result.error
    assert result.error is None
    assert result.production_id == "job-20260917T131500Z-1a2b3c4d5e6f"
    assert result.manifest is not None and result.artifact is not None
    # RunState reached succeeded via exactly one attempt
    record = run.stage(STAGE_NAME)
    assert record.status.value == "succeeded"
    assert record.attempts == 1
    assert record.last_error is None
    # artifact registered through the P0 registry with the P1-A kind
    assert result.artifact.kind is ArtifactKind.SCENE_MANIFEST
    assert result.artifact.path == "scene_manifest.json"
    manifest_file = tmp_path / "run" / "scene_manifest.json"
    assert manifest_file.is_file()
    # 3-5 scene MVP respected; requirements preserved as intent
    assert len(result.manifest.scenes) == 5
    assert [s.sequence for s in result.manifest.scenes] == [1, 2, 3, 4, 5]
    assert result.manifest.scenes[1].visual.requirement.kind is AssetKind.VIDEO


def test_reload_and_verify_persisted_manifest(tmp_path):
    run = make_run()
    registry = make_registry(tmp_path, run.run_id)
    result = run_script_to_scene_stage(load_input("minimal.json"), run, registry)
    assert result.ok

    reloaded = load_manifest(tmp_path / "run" / result.artifact.path)
    assert isinstance(reloaded, ProductionManifest)
    assert reloaded == result.manifest


# ---- B. determinism ----------------------------------------------------------


def test_same_input_produces_identical_manifest_bytes(tmp_path):
    script = load_input("documentary.json")
    manifests = []
    for _ in range(2):
        run = make_run()
        registry = make_registry(tmp_path / f"run-{len(manifests)}", run.run_id)
        result = run_script_to_scene_stage(script, run, registry)
        assert result.ok
        manifests.append(result.manifest)
    assert dump_manifest(manifests[0]) == dump_manifest(manifests[1])


def test_scene_ids_derived_deterministically():
    manifest = build_manifest(
        ScriptInput(
            production_id="job-x",
            title="t",
            scenes=(scene(), scene(), scene()),
        )
    )
    assert [s.scene_id for s in manifest.scenes] == ["scene-001", "scene-002", "scene-003"]


def test_explicit_scene_id_and_duration_preserved():
    manifest = build_manifest(
        ScriptInput(
            production_id="job-x",
            title="t",
            scenes=(
                scene(scene_id="custom-id", duration_seconds=7.5),
                scene(),
            ),
        )
    )
    assert manifest.scenes[0].scene_id == "custom-id"
    assert manifest.scenes[0].duration_seconds == 7.5
    assert manifest.scenes[1].scene_id == "scene-002"

# ---- duration rule -----------------------------------------------------------


def test_duration_rule_is_deterministic_and_documented():
    # ~150 wpm => words / 2.5, floored at MIN_SCENE_DURATION_SECONDS
    assert estimate_scene_duration("one two three four five") == 2.0  # 5/2.5 == 2.0
    long_text = " ".join(["word"] * 20)  # 20 words => 8.0 s
    assert estimate_scene_duration(long_text) == 8.0
    assert estimate_scene_duration("short") == MIN_SCENE_DURATION_SECONDS  # floor


def test_derived_durations_applied_when_input_omits_them():
    manifest = build_manifest(
        ScriptInput(production_id="job-x", title="t", scenes=(scene(),))
    )
    expected = estimate_scene_duration("A single narration line.")
    assert manifest.scenes[0].duration_seconds == expected


# ---- C. contract integration / invalid scenes --------------------------------


def test_sequence_mismatch_rejected_by_contract_fails_stage(tmp_path):
    run = make_run()
    registry = make_registry(tmp_path, run.run_id)
    bad = ScriptInput(
        production_id="job-x",
        title="t",
        scenes=(scene(sequence=1), scene(sequence=3)),
    )
    result = run_script_to_scene_stage(bad, run, registry)
    assert result.ok is False
    assert run.stage(STAGE_NAME).status.value == "failed"
    assert run.stage(STAGE_NAME).last_error is not None
    assert "sequence" in result.error


def test_invalid_fixture_fails_stage(tmp_path):
    # invalid.json loads structurally (input DTOs are thin); the P1-A
    # contract is the single validation authority and must reject it
    script = load_input("invalid.json")
    run = make_run()
    registry = make_registry(tmp_path, run.run_id)
    result = run_script_to_scene_stage(script, run, registry)
    assert result.ok is False
    assert result.manifest is None
    assert result.artifact is None
    record = run.stage(STAGE_NAME)
    assert record.status.value == "failed"
    assert record.last_error  # error is observable in the run state
    # no false-success artifact was registered
    assert registry.for_stage(ARTIFACT_STAGE) == []


def test_missing_narration_rejected():
    with pytest.raises(ValidationError):
        SceneInput.model_validate({"visual_description": "x"})


def test_whitespace_narration_rejected_by_contract_via_stage(tmp_path):
    run = make_run()
    registry = make_registry(tmp_path, run.run_id)
    result = run_script_to_scene_stage(
        ScriptInput(production_id="job-x", title="t", scenes=(scene(narration="   "),)),
        run,
        registry,
    )
    assert result.ok is False
    assert "non-whitespace" in result.error
    assert run.stage(STAGE_NAME).status.value == "failed"


def test_empty_visual_description_rejected_by_contract_via_stage(tmp_path):
    run = make_run()
    registry = make_registry(tmp_path, run.run_id)
    result = run_script_to_scene_stage(
        ScriptInput(production_id="job-x", title="t", scenes=(scene(visual=""),)),
        run,
        registry,
    )
    assert result.ok is False
    assert run.stage(STAGE_NAME).status.value == "failed"


def test_negative_duration_rejected_by_contract_via_stage(tmp_path):
    run = make_run()
    registry = make_registry(tmp_path, run.run_id)
    result = run_script_to_scene_stage(
        ScriptInput(production_id="job-x", title="t", scenes=(scene(duration_seconds=-1.0),)),
        run,
        registry,
    )
    assert result.ok is False
    assert run.stage(STAGE_NAME).status.value == "failed"


def test_invalid_production_id_rejected_by_contract_via_stage(tmp_path):
    run = make_run()
    registry = make_registry(tmp_path, run.run_id)
    result = run_script_to_scene_stage(
        ScriptInput(production_id="has space", title="t", scenes=(scene(),)),
        run,
        registry,
    )
    assert result.ok is False
    # production identity was bound from the input and remains observable on failure
    assert result.production_id == "has space"
    assert run.stage(STAGE_NAME).status.value == "failed"


def test_unknown_input_field_rejected():
    with pytest.raises(ValidationError, match="renderer"):
        scene(**{"renderer": "ffmpeg"})


def test_malformed_input_rejected():
    with pytest.raises(ValidationError):
        ScriptInput.model_validate({"production_id": "job-x"})


def test_invalid_input_json_file_fails_stage(tmp_path):
    # invalid.json is structurally loadable; the stage must fail on contract validation
    run = make_run()
    registry = make_registry(tmp_path, run.run_id)
    result = run_script_to_scene_stage(load_input("invalid.json"), run, registry)
    assert result.ok is False
    assert run.stage(STAGE_NAME).status.value == "failed"


# ---- D/G. state lifecycle + no false success ---------------------------------


def test_state_lifecycle_success(tmp_path):
    run = make_run()
    registry = make_registry(tmp_path, run.run_id)
    run_script_to_scene_stage(load_input("minimal.json"), run, registry)
    record = run.stage(STAGE_NAME)
    assert record.status.value == "succeeded"
    assert record.attempts == 1


def test_state_lifecycle_failure(tmp_path):
    run = make_run()
    registry = make_registry(tmp_path, run.run_id)
    run_script_to_scene_stage(load_input("invalid.json"), run, registry)
    record = run.stage(STAGE_NAME)
    assert record.status.value == "failed"
    assert record.attempts == 1
    assert record.last_error is not None


def test_retry_after_failure_is_possible(tmp_path):
    run = make_run()
    registry = make_registry(tmp_path, run.run_id)
    first = run_script_to_scene_stage(load_input("invalid.json"), run, registry)
    assert first.ok is False
    # retry with valid input: failed → running → succeeded is allowed
    second = run_script_to_scene_stage(load_input("minimal.json"), run, registry)
    assert second.ok is True
    assert run.stage(STAGE_NAME).status.value == "succeeded"
    assert run.stage(STAGE_NAME).attempts == 2


def test_no_false_success_when_persistence_fails(tmp_path):
    # make the run directory un-creatable: a file blocks the parent path
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    run = make_run()
    registry = ArtifactRegistry(blocker / "run", run.run_id)

    result = run_script_to_scene_stage(load_input("minimal.json"), run, registry)

    assert result.ok is False
    assert result.manifest is None  # manifest built but not confirmed
    assert result.artifact is None
    record = run.stage(STAGE_NAME)
    assert record.status.value == "failed"
    assert record.last_error is not None


def test_rerun_after_success_is_rejected_by_state_machine(tmp_path):
    run = make_run()
    registry = make_registry(tmp_path, run.run_id)
    first = run_script_to_scene_stage(load_input("minimal.json"), run, registry)
    assert first.ok
    # succeeded is terminal: the same RunState cannot run the stage again
    second = run_script_to_scene_stage(load_input("minimal.json"), run, registry)
    assert second.ok is False
    assert run.stage(STAGE_NAME).status.value == "succeeded"  # unchanged


# ---- E. idempotent rerun ------------------------------------------------------


def test_idempotent_rerun_reuses_existing_artifact(tmp_path):
    run_id = new_run_id()
    registry = make_registry(tmp_path, run_id)
    script = load_input("documentary.json")

    first = run_script_to_scene_stage(script, make_run(run_id), registry)
    assert first.ok
    # fresh RunState, same run directory/registry: identical manifest content
    second = run_script_to_scene_stage(script, make_run(run_id), registry)
    assert second.ok

    assert second.artifact.artifact_id == first.artifact.artifact_id
    scene_manifest_refs = [
        a for a in registry.all() if a.kind is ArtifactKind.SCENE_MANIFEST
    ]
    assert len(scene_manifest_refs) == 1  # no duplicate artifacts


# ---- observability ------------------------------------------------------------


def test_stage_logs_structured_events(tmp_path):
    stream = io.StringIO()
    log = StructuredLogger(level="INFO", stream=stream)
    run = make_run()
    registry = make_registry(tmp_path, run.run_id)

    ok_result = run_script_to_scene_stage(load_input("minimal.json"), run, registry, logger=log)
    assert ok_result.ok
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    events = [r["message"] for r in records]
    assert "script_stage_started" in events
    assert "scene_manifest_generation_started" in events
    assert "scene_manifest_generated" in events
    assert "scene_manifest_persisted" in events
    assert "script_stage_succeeded" in events
    # identity fields are bound into every record
    started = records[events.index("script_stage_started")]
    assert started["run_id"] == run.run_id
    assert started["stage"] == STAGE_NAME
    assert started["job_id"] == run.job_id
    persisted = records[events.index("scene_manifest_persisted")]
    assert persisted["artifact_id"] == ok_result.artifact.artifact_id

    # failure emits the failure event with bound identity
    stream = io.StringIO()
    fail_run = make_run()
    fail_registry = make_registry(tmp_path / "fail", fail_run.run_id)
    failed = run_script_to_scene_stage(load_input("invalid.json"), fail_run, fail_registry, logger=StructuredLogger(level="INFO", stream=stream))
    assert failed.ok is False
    fail_records = [json.loads(line) for line in stream.getvalue().splitlines()]
    fail_events = [r["message"] for r in fail_records]
    assert "script_stage_failed" in fail_events
    failed_rec = fail_records[fail_events.index("script_stage_failed")]
    assert "error" in failed_rec
    assert failed_rec["stage"] == STAGE_NAME


