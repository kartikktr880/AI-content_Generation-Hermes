"""P3 narration-audio stage tests.

Covers: provider health, deterministic narration resolution, real
integration with a P1-B-generated scene manifest, narration manifest
persistence/registry/reload, state lifecycle, failure paths (no false
success), determinism, provenance, contract separation, observability,
and full P0/P1-A/P1-B/P2 regression.
"""

import hashlib
import json
import io
from pathlib import Path

import pytest

from ayce.adapters import Adapter, AdapterHealth, AdapterRegistry
from ayce.artifacts import ArtifactKind, ArtifactRegistry
from ayce.config import Config
from ayce.ids import new_job_id, new_run_id
from ayce.logging import StructuredLogger
from ayce.narration_audio import (
    ARTIFACT_STAGE,
    NARRATION_MANIFEST_FILENAME,
    STAGE_NAME,
    AudioProvider,
    FileBackedNarrationProvider,
    NarrationManifest,
    NarrationAudioError,
    NarrationAudioProvenance,
    ResolvedNarrationAudio,
    dump_narration_manifest,
    load_narration_manifest,
    run_narration_audio_stage,
)
from ayce.script_to_scene import load_script_input, run_script_to_scene_stage
from ayce.state import RunState

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NARRATION_FIXTURES = FIXTURES / "narration_fixtures"


def make_run(run_id: str | None = None) -> RunState:
    return RunState(run_id=run_id or new_run_id(), job_id=new_job_id())


def make_registry(tmp_path: Path, run_id: str) -> ArtifactRegistry:
    return ArtifactRegistry(tmp_path / "run", run_id)


def make_provider(fixture_dir: Path | None = None) -> FileBackedNarrationProvider:
    return FileBackedNarrationProvider(
        Config.from_env(env={}), fixture_dir or NARRATION_FIXTURES
    )


def fixture_manifest(tmp_path: Path, name: str = "documentary.json", run_id: str | None = None):
    """Run the real P1-B stage to produce a genuine scene manifest + artifact."""
    script = load_script_input(FIXTURES / "script_to_scene" / name)
    run_id = run_id or new_run_id()
    registry = make_registry(tmp_path, run_id)
    result = run_script_to_scene_stage(script, make_run(run_id), registry)
    assert result.ok, result.error
    return result.manifest, registry, run_id


# ---- A. provider health -------------------------------------------------------


def test_provider_healthy_with_valid_fixture_dir():
    health = make_provider().health()
    assert isinstance(health, AdapterHealth)
    assert health.available is True
    assert health.name == "file-backed-narration-fixtures"


def test_provider_unhealthy_with_missing_dir(tmp_path):
    provider = FileBackedNarrationProvider(Config.from_env(env={}), tmp_path / "does-not-exist")
    health = provider.health()
    assert health.available is False
    assert "not found" in health.detail


# ---- B. single narration resolution -------------------------------------------


def test_single_narration_resolves_expected_fixture(tmp_path):
    manifest, _, _ = fixture_manifest(tmp_path)
    provider = make_provider()

    audio = provider.resolve_narration(
        manifest.scenes[0].narration, manifest.scenes[0].scene_id, run_dir=tmp_path / "run"
    )

    assert isinstance(audio, ResolvedNarrationAudio)
    assert audio.scene_id == "scene-001"
    copied = tmp_path / "run" / audio.path
    assert copied.is_file()  # a real local file was copied into the run dir
    assert copied.read_bytes() == (NARRATION_FIXTURES / "scene-001.wav").read_bytes()
    assert hashlib.sha256(copied.read_bytes()).hexdigest() == audio.sha256
    # truthful metadata read from the valid WAV header (0.1s digital silence)
    assert audio.duration_seconds == 0.1
    assert audio.format == "wav"


def test_unparseable_audio_yields_none_metadata(tmp_path):
    manifest, _, _ = fixture_manifest(tmp_path)
    fixture_dir = tmp_path / "not-audio"
    fixture_dir.mkdir()
    (fixture_dir / "scene-001.wav").write_text("not really a wav", encoding="utf-8")
    provider = make_provider(fixture_dir)

    asset = provider.resolve_narration(manifest.scenes[0].narration, "scene-001", run_dir=tmp_path / "run")

    assert asset.duration_seconds is None  # never invented
    assert asset.format is None


def test_narration_text_is_not_required_for_lookup(tmp_path):
    """The fixture provider matches by scene_id; narration text is not used for matching."""
    manifest, _, _ = fixture_manifest(tmp_path)
    provider = make_provider()
    audio = provider.resolve_narration(manifest.scenes[2].narration, "scene-003", run_dir=tmp_path / "run")
    assert audio.path == "audio/scene-003.wav"


# ---- C. real integration: full stage over the P1-B scene manifest -------------


def test_documentary_narration_resolves_deterministically(tmp_path):
    manifest, registry, run_id = fixture_manifest(tmp_path)
    run = make_run(run_id)

    result = run_narration_audio_stage(manifest, run, registry, provider=make_provider())

    assert result.ok, result.error
    # every scene carries narration in the P1-A contract → all 5 resolved
    assert [a.scene_id for a in result.manifest.narration_audio] == [
        "scene-001", "scene-002", "scene-003", "scene-004", "scene-005",
    ]
    for audio in result.manifest.narration_audio:
        assert (tmp_path / "run" / audio.path).is_file()
        assert audio.duration_seconds is not None  # truthful WAV metadata
    # deterministic durations follow the deterministic fixtures
    assert [a.duration_seconds for a in result.manifest.narration_audio] == [0.1, 0.2, 0.3, 0.4, 0.5]


# ---- provenance ----------------------------------------------------------------


def test_provenance_records_fixture_origin(tmp_path):
    manifest, registry, run_id = fixture_manifest(tmp_path)
    run = make_run(run_id)
    result = run_narration_audio_stage(manifest, run, registry, make_provider())
    assert result.ok
    for audio in result.manifest.narration_audio:
        prov = audio.provenance
        assert isinstance(prov, NarrationAudioProvenance)
        assert prov.provider == "file-backed-narration-fixtures"
        assert prov.source == "local_fixture"
        assert prov.source_ref.endswith(".wav")
        # fixture provenance makes NO real-world licensing claim
        assert prov.license is None


# ---- F/G. narration manifest artifact + registry integration --------------------


def test_narration_manifest_persisted_registered_and_reloadable(tmp_path):
    manifest, registry, run_id = fixture_manifest(tmp_path)
    run = make_run(run_id)

    result = run_narration_audio_stage(manifest, run, registry, make_provider())

    assert result.ok
    ref = result.artifact
    assert ref.kind is ArtifactKind.AUDIO
    assert ref.path == NARRATION_MANIFEST_FILENAME
    assert ref.metadata == {
        "schema_version": "1.0",
        "narration_audio": 5,
        "provider": "file-backed-narration-fixtures",
    }
    assert (tmp_path / "run" / NARRATION_MANIFEST_FILENAME).is_file()

    # reload through both layers: registry + schema validation
    reloaded_registry = ArtifactRegistry.load(tmp_path / "run", run_id)
    ref2 = reloaded_registry.require(ref.artifact_id)
    assert ref2.kind is ArtifactKind.AUDIO
    reloaded = load_narration_manifest(Path(tmp_path / "run") / ref2.path)
    assert isinstance(reloaded, NarrationManifest)
    assert reloaded == result.manifest
    assert [a.scene_id for a in reloaded.narration_audio] == [
        "scene-001", "scene-002", "scene-003", "scene-004", "scene-005",
    ]


def test_narration_manifest_references_scene_manifest(tmp_path):
    manifest, registry, run_id = fixture_manifest(tmp_path)
    result = run_narration_audio_stage(manifest, make_run(run_id), registry, make_provider())
    assert result.ok
    assert result.manifest.scene_manifest == "scene_manifest.json"
    assert result.manifest.production_id == manifest.production_id
    assert result.manifest.provider == "file-backed-narration-fixtures"

# ---- H. state lifecycle ---------------------------------------------------------


def test_state_lifecycle_success(tmp_path):
    manifest, registry, run_id = fixture_manifest(tmp_path)
    run = make_run(run_id)
    run_narration_audio_stage(manifest, run, registry, make_provider())
    record = run.stage(STAGE_NAME)
    assert record.status.value == "succeeded"
    assert record.attempts == 1
    assert record.last_error is None


def test_state_lifecycle_failure(tmp_path):
    manifest, registry, run_id = fixture_manifest(tmp_path)
    run = make_run(run_id)
    empty_dir = tmp_path / "empty-fixtures"
    empty_dir.mkdir()  # exists but contains no narration audio
    result = run_narration_audio_stage(manifest, run, registry, make_provider(empty_dir))
    record = run.stage(STAGE_NAME)
    assert record.status.value == "failed"
    assert record.attempts == 1
    assert record.last_error is not None


def test_retry_after_failure_is_possible(tmp_path):
    manifest, registry, run_id = fixture_manifest(tmp_path)
    run = make_run(run_id)
    # first attempt: provider dir missing entirely → unhealthy → failed
    bad_provider = FileBackedNarrationProvider(Config.from_env(env={}), tmp_path / "missing")
    first = run_narration_audio_stage(manifest, run, registry, provider=bad_provider)
    assert first.ok is False
    # retry with a healthy provider: failed → running → succeeded
    second = run_narration_audio_stage(manifest, run, registry, make_provider())
    assert second.ok is True
    assert run.stage(STAGE_NAME).status.value == "succeeded"
    assert run.stage(STAGE_NAME).attempts == 2


def test_rerun_after_success_is_rejected_by_state_machine(tmp_path):
    manifest, registry, run_id = fixture_manifest(tmp_path)
    run = make_run(run_id)
    first = run_narration_audio_stage(manifest, run, registry, make_provider())
    assert first.ok
    # succeeded is terminal: the same RunState cannot run the stage again
    second = run_narration_audio_stage(manifest, run, registry, make_provider())
    assert second.ok is False
    assert run.stage(STAGE_NAME).status.value == "succeeded"  # unchanged


# ---- I. no false success / failure modes ----------------------------------------


def test_missing_narration_fixture_fails_stage(tmp_path):
    # provider fixture dir contains only scene-001; scene-002's narration
    # cannot be resolved → the stage must fail explicitly and publish
    # no narration manifest
    manifest, registry, run_id = fixture_manifest(tmp_path)
    run = make_run(run_id)
    partial_dir = tmp_path / "partial-fixtures"
    partial_dir.mkdir()
    (partial_dir / "scene-001.wav").write_bytes((NARRATION_FIXTURES / "scene-001.wav").read_bytes())
    result = run_narration_audio_stage(manifest, run, registry, make_provider(tmp_path / "partial-fixtures"))
    assert result.ok is False
    assert "scene-002" in result.error
    assert result.manifest is None
    assert result.artifact is None
    assert run.stage(STAGE_NAME).status.value == "failed"
    # no false-success narration manifest was registered
    assert [a for a in registry.all() if a.kind is ArtifactKind.AUDIO] == []


def test_unhealthy_provider_fails_stage_before_resolution(tmp_path):
    manifest, registry, run_id = fixture_manifest(tmp_path)
    run = make_run(run_id)
    provider = FileBackedNarrationProvider(Config.from_env(env={}), tmp_path / "missing-dir")
    result = run_narration_audio_stage(manifest, run, registry, provider=provider)
    assert result.ok is False
    assert result.provider == "file-backed-narration-fixtures"
    assert "unhealthy" in result.error
    assert run.stage(STAGE_NAME).status.value == "failed"
    # nothing was resolved or copied
    assert not (tmp_path / "run" / "audio").exists()


def test_invalid_scene_manifest_rejected_before_resolution(tmp_path):
    run = make_run()
    registry = make_registry(tmp_path, run.run_id)
    result = run_narration_audio_stage(
        {"schema_version": "9.0"}, run, registry, provider=make_provider()
    )
    assert result.ok is False
    assert run.stage(STAGE_NAME).status.value == "failed"
    assert result.manifest is None


def test_persistence_failure_does_not_fake_success(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    manifest, _, _ = fixture_manifest(tmp_path)
    run = make_run()
    registry = ArtifactRegistry(blocker / "run", run.run_id)
    result = run_narration_audio_stage(manifest, run, registry, make_provider())
    assert result.ok is False
    assert result.manifest is None
    assert result.artifact is None
    assert run.stage(STAGE_NAME).status.value == "failed"


# ---- J. determinism + contract separation ---------------------------------------


def test_same_input_yields_identical_narration_manifest_bytes(tmp_path):
    script = load_script_input(FIXTURES / "script_to_scene" / "documentary.json")
    manifests = []
    for index in range(2):
        run = make_run()
        registry = ArtifactRegistry(tmp_path / f"run-{index}", run.run_id)
        scene_result = run_script_to_scene_stage(script, run, registry)
        assert scene_result.ok
        result = run_narration_audio_stage(
            scene_result.manifest, run, registry, provider=make_provider()
        )
        assert result.ok, result.error
        manifests.append(result.manifest)
    assert dump_narration_manifest(manifests[0]) == dump_narration_manifest(manifests[1])


def test_resolved_audio_stays_outside_the_scene_contract(tmp_path):
    manifest, registry, run_id = fixture_manifest(tmp_path)
    scene_state_before = json.dumps(
        [s.model_dump() for s in manifest.scenes], sort_keys=True
    )
    run = make_run(run_id)
    result = run_narration_audio_stage(manifest, run, registry, make_provider())
    assert result.ok
    # the scene contract was not mutated by narration resolution
    assert json.dumps([s.model_dump() for s in manifest.scenes], sort_keys=True) == scene_state_before
    # no resolved-audio fields leaked into the contract models.
    # NOTE: Scene.duration_seconds is P1-A creative intent (intended scene
    # timing) and predates P3 — it is NOT audio metadata. The fields below
    # exist only on resolved-audio records.
    for model in (type(manifest), type(manifest.scenes[0]), type(manifest.scenes[0].narration)):
        leaked = {"path", "sha256", "provenance", "format", "audio_path"} & set(model.model_fields)
        assert not leaked, f"audio field leaked into {model.__name__}"


# ---- observability ------------------------------------------------------------------


def test_stage_logs_structured_events(tmp_path):
    stream = io.StringIO()
    log = StructuredLogger(level="INFO", stream=stream)
    manifest, registry, run_id = fixture_manifest(tmp_path)
    run = make_run(run_id)

    result = run_narration_audio_stage(manifest, run, registry, make_provider(), logger=log)
    assert result.ok

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    events = [r["message"] for r in records]
    assert "narration_audio_started" in events
    assert "narration_resolution_started" in events
    assert "narration_audio_resolved" in events
    assert "narration_manifest_persisted" in events
    assert "narration_audio_succeeded" in events
    started = records[events.index("narration_audio_started")]
    assert started["run_id"] == run.run_id
    assert started["stage"] == STAGE_NAME
    assert started["provider"] == "file-backed-narration-fixtures"
    resolved_rec = records[events.index("narration_audio_resolved")]
    assert resolved_rec["scene_id"] == "scene-001"

    # failure event with identity
    stream = io.StringIO()
    fail_run = make_run()
    fail_registry = ArtifactRegistry(tmp_path / "fail-run", fail_run.run_id)
    empty_dir = tmp_path / "fail-empty"
    empty_dir.mkdir()
    failed = run_narration_audio_stage(
        manifest,
        fail_run,
        fail_registry,
        provider=make_provider(empty_dir),
        logger=StructuredLogger(level="INFO", stream=stream),
    )
    assert failed.ok is False
    fail_records = [json.loads(line) for line in stream.getvalue().splitlines()]
    fail_events = [r["message"] for r in fail_records]
    assert "narration_audio_stage_failed" in fail_events
    failed_rec = fail_records[fail_events.index("narration_audio_stage_failed")]
    assert failed_rec["stage"] == STAGE_NAME
    assert "error" in failed_rec


# ---- adapter convention compatibility -----------------------------------------------


def test_provider_fits_the_p0_adapter_convention():
    provider = make_provider()
    assert isinstance(provider, Adapter)
    registry = AdapterRegistry()
    registry.register(FileBackedNarrationProvider)
    assert "file-backed-narration-fixtures" in registry.names()
    created = registry.create("file-backed-narration-fixtures", Config.from_env(env={}))
    assert isinstance(created, FileBackedNarrationProvider)
    # the created provider is immediately usable against the repo fixtures
    assert created.health().available is True





