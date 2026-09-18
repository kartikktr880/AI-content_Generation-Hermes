"""P2 asset resolution stage tests.

Covers: provider health, deterministic resolution, real integration with a
P1-B-generated scene manifest, asset manifest persistence/registry/reload,
state lifecycle, failure paths (no false success), determinism, provenance,
and full P0/P1-A/P1-B regression.
"""

import hashlib
import json
from pathlib import Path

import pytest

from ayce.adapters import AdapterHealth
from ayce.artifacts import ArtifactKind, ArtifactRegistry
from ayce.config import Config
from ayce.ids import new_job_id, new_run_id
from ayce.logging import StructuredLogger
from ayce.scene_contract import load_manifest
from ayce.script_to_scene import run_script_to_scene_stage, load_script_input
from ayce.asset_resolution import (
    ARTIFACT_STAGE,
    ASSET_MANIFEST_FILENAME,
    STAGE_NAME,
    AssetKind,
    AssetManifest,
    AssetProvenance,
    AssetProvider,
    AssetResolutionError,
    FileBackedAssetProvider,
    ResolvedAsset,
    dump_asset_manifest,
    load_asset_manifest,
    run_asset_resolution_stage,
)
from ayce.state import RunState

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PROVIDER_FIXTURES = FIXTURES / "asset_provider"


def make_run(run_id: str | None = None) -> RunState:
    return RunState(run_id=run_id or new_run_id(), job_id=new_job_id())


def make_registry(tmp_path: Path, run_id: str) -> ArtifactRegistry:
    return ArtifactRegistry(tmp_path / "run", run_id)


def make_provider(fixture_dir: Path | None = None) -> FileBackedAssetProvider:
    return FileBackedAssetProvider(
        Config.from_env(env={}), fixture_dir or PROVIDER_FIXTURES
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
    assert health.name == "file-backed-fixtures"


def test_provider_unhealthy_with_missing_dir(tmp_path):
    provider = FileBackedAssetProvider(Config.from_env(env={}), tmp_path / "does-not-exist")
    health = provider.health()
    assert health.available is False
    assert "not found" in health.detail


def test_provider_health_reflects_created_then_removed_dir(tmp_path):
    provider = make_provider(tmp_path / "late-fixtures")
    assert provider.health().available is False  # missing → unhealthy
    (tmp_path / "late").mkdir()
    provider = FileBackedAssetProvider(Config.from_env(env={}), tmp_path / "late")
    assert provider.health().available is True


# ---- B. single asset resolution ----------------------------------------------


def test_single_requirement_resolves_expected_fixture(tmp_path):
    manifest, registry, _ = fixture_manifest(tmp_path)
    provider = make_provider()
    requirement = manifest.scenes[1].visual.requirement
    run = make_run()

    asset = provider.resolve(requirement, manifest.scenes[1].scene_id, run_dir=tmp_path / "run")

    assert isinstance(asset, ResolvedAsset)
    assert asset.scene_id == "scene-002"
    assert asset.kind is AssetKind.VIDEO
    copied = tmp_path / "run" / asset.path
    assert copied.is_file()  # a real local file was copied into the run dir
    assert hashlib.sha256(copied.read_bytes()).hexdigest() == asset.sha256
    assert asset.provenance.source_ref == "scene-002.mp4"


def test_resolved_copy_matches_source_bytes(tmp_path):
    manifest, _, _ = fixture_manifest(tmp_path)
    run = make_run()
    registry = make_registry(tmp_path / "b", run.run_id)
    provider = make_provider()
    requirement = manifest.scenes[0].visual.requirement

    asset = provider.resolve(requirement, manifest.scenes[0].scene_id, run_dir=tmp_path / "run" / "x")
    copied = tmp_path / "run" / "x" / asset.path
    source = PROVIDER_FIXTURES / "scene-001.png"
    assert copied.read_bytes() == source.read_bytes()
    assert asset.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()


# ---- C. multiple asset resolution (through the real stage) --------------------


def test_scene_manifest_requirements_resolve_deterministically(tmp_path):
    manifest, registry, run_id = fixture_manifest(tmp_path)
    run = make_run(run_id)

    result = run_asset_resolution_stage(manifest, run, registry, provider=make_provider())

    assert result.ok, result.error
    # 4 of 5 documentary scenes carry requirements; scene-004 has none
    assert [a.scene_id for a in result.manifest.resolved_assets] == [
        "scene-001", "scene-002", "scene-003", "scene-005",
    ]
    kinds = {a.scene_id: a.kind for a in result.manifest.resolved_assets}
    assert kinds["scene-001"] is AssetKind.IMAGE
    assert kinds["scene-002"] is AssetKind.VIDEO
    # copied files actually exist in the run directory
    for asset in result.manifest.resolved_assets:
        assert (tmp_path / "run" / asset.path).is_file()


# ---- provenance ----------------------------------------------------------------


def test_provenance_records_fixture_origin(tmp_path):
    manifest, registry, run_id = fixture_manifest(tmp_path)
    run = make_run(run_id)
    result = run_asset_resolution_stage(manifest, run, registry, make_provider())
    assert result.ok
    for asset in result.manifest.resolved_assets:
        prov = asset.provenance
        assert isinstance(prov, AssetProvenance)
        assert prov.provider == "file-backed-fixtures"
        assert prov.source == "local_fixture"
        assert prov.source_ref.endswith((".png", ".mp4"))
        # fixture provenance makes NO real-world licensing claim
        assert prov.license is None


# ---- observability ------------------------------------------------------------------


def test_stage_logs_structured_events(tmp_path):
    import io

    stream = io.StringIO()
    log = StructuredLogger(level="INFO", stream=stream)
    manifest, registry, run_id = fixture_manifest(tmp_path)
    run = make_run(run_id)

    result = run_asset_resolution_stage(manifest, run, registry, make_provider(), logger=log)
    assert result.ok

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    events = [r["message"] for r in records]
    assert "asset_resolution_started" in events
    assert "asset_requirement_resolution_started" in events
    assert "asset_resolved" in events
    assert "asset_manifest_persisted" in events
    assert "asset_resolution_succeeded" in events
    started = records[events.index("asset_resolution_started")]
    assert started["run_id"] == run.run_id
    assert started["stage"] == STAGE_NAME
    assert started["provider"] == "file-backed-fixtures"
    resolved_rec = records[events.index("asset_resolved")]
    assert resolved_rec["scene_id"] == "scene-001"

    # failure event with identity
    stream = io.StringIO()
    fail_run = make_run()
    fail_registry = ArtifactRegistry(tmp_path / "fail-run", fail_run.run_id)
    empty_dir = tmp_path / "fail-empty"
    empty_dir.mkdir()
    failed = run_asset_resolution_stage(
        manifest,
        fail_run,
        fail_registry,
        provider=make_provider(empty_dir),
        logger=StructuredLogger(level="INFO", stream=stream),
    )
    assert failed.ok is False
    fail_records = [json.loads(line) for line in stream.getvalue().splitlines()]
    fail_events = [r["message"] for r in fail_records]
    assert "asset_resolution_stage_failed" in fail_events
    failed_rec = fail_records[fail_events.index("asset_resolution_stage_failed")]
    assert failed_rec["stage"] == STAGE_NAME
    assert "error" in failed_rec


# ---- adapter convention compatibility -----------------------------------------------


def test_provider_fits_the_p0_adapter_convention():
    from ayce.adapters import Adapter, AdapterRegistry

    provider = make_provider()
    assert isinstance(provider, Adapter)
    registry = AdapterRegistry()
    registry.register(FileBackedAssetProvider)
    assert "file-backed-fixtures" in registry.names()
    created = registry.create("file-backed-fixtures", Config.from_env(env={}))
    assert isinstance(created, FileBackedAssetProvider)


# ---- F/G. asset manifest artifact + registry integration ----------------------


def test_asset_manifest_persisted_registered_and_reloadable(tmp_path):
    manifest, registry, run_id = fixture_manifest(tmp_path)
    run = make_run(run_id)

    result = run_asset_resolution_stage(manifest, run, registry, make_provider())

    assert result.ok
    ref = result.artifact
    assert ref.kind is ArtifactKind.ASSET_MANIFEST
    assert ref.path == ASSET_MANIFEST_FILENAME
    assert ref.metadata == {
        "schema_version": "1.0",
        "resolved_assets": 4,
        "provider": "file-backed-fixtures",
    }
    assert (tmp_path / "run" / ASSET_MANIFEST_FILENAME).is_file()

    # reload through both layers: registry + schema validation
    reloaded_registry = ArtifactRegistry.load(tmp_path / "run", run_id)
    ref2 = reloaded_registry.require(ref.artifact_id)
    assert ref2.kind is ArtifactKind.ASSET_MANIFEST
    reloaded = load_asset_manifest(Path(tmp_path / "run") / ref2.path)
    assert isinstance(reloaded, AssetManifest)
    assert reloaded == result.manifest
    # resolved assets remain discoverable via the scene linkage
    by_scene = {a.scene_id: a for a in reloaded.resolved_assets}
    assert set(by_scene) == {"scene-001", "scene-002", "scene-003", "scene-005"}
    assert by_scene["scene-002"].path == "assets/scene-002.mp4"


def test_asset_manifest_references_scene_manifest(tmp_path):
    manifest, registry, run_id = fixture_manifest(tmp_path)
    result = run_asset_resolution_stage(manifest, make_run(run_id), registry, make_provider())
    assert result.ok
    assert result.manifest.scene_manifest == "scene_manifest.json"
    assert result.manifest.production_id == manifest.production_id
    assert result.manifest.provider == "file-backed-fixtures"


# ---- H. state lifecycle ---------------------------------------------------------


def test_state_lifecycle_success(tmp_path):
    manifest, registry, run_id = fixture_manifest(tmp_path)
    run = make_run(run_id)
    run_asset_resolution_stage(manifest, run, registry, make_provider())
    record = run.stage(STAGE_NAME)
    assert record.status.value == "succeeded"
    assert record.attempts == 1
    assert record.last_error is None


def test_state_lifecycle_failure(tmp_path):
    manifest, registry, run_id = fixture_manifest(tmp_path)
    run = make_run(run_id)
    empty_dir = tmp_path / "empty-fixtures"
    empty_dir.mkdir()  # exists but contains no matching assets
    result = run_asset_resolution_stage(manifest, run, registry, make_provider(empty_dir))
    record = run.stage(STAGE_NAME)
    assert record.status.value == "failed"
    assert record.attempts == 1
    assert record.last_error is not None


def test_retry_after_failure_is_possible(tmp_path):
    manifest, registry, run_id = fixture_manifest(tmp_path)
    run = make_run(run_id)
    # first attempt: provider dir missing entirely → unhealthy → failed
    bad_provider = FileBackedAssetProvider(Config.from_env(env={}), tmp_path / "missing")
    first = run_asset_resolution_stage(manifest, run, registry, provider=bad_provider)
    assert first.ok is False
    # retry with a healthy provider: failed → running → succeeded
    second = run_asset_resolution_stage(manifest, run, registry, make_provider())
    assert second.ok is True
    assert run.stage(STAGE_NAME).status.value == "succeeded"
    assert run.stage(STAGE_NAME).attempts == 2


def test_rerun_after_success_is_rejected_by_state_machine(tmp_path):
    manifest, registry, run_id = fixture_manifest(tmp_path)
    run = make_run(run_id)
    first = run_asset_resolution_stage(manifest, run, registry, make_provider())
    assert first.ok
    # succeeded is terminal: the same RunState cannot run the stage again
    second = run_asset_resolution_stage(manifest, run, registry, make_provider())
    assert second.ok is False
    assert run.stage(STAGE_NAME).status.value == "succeeded"  # unchanged


# ---- I. no false success / failure modes ----------------------------------------


def test_missing_fixture_asset_fails_stage(tmp_path):
    # provider fixture dir contains only scene-001; the documentary
    # manifest's NEXT requirement (scene-002) cannot be resolved → the
    # stage must fail explicitly and publish no asset manifest
    manifest, registry, run_id = fixture_manifest(tmp_path)
    run = make_run(run_id)
    partial_dir = tmp_path / "partial-fixtures"
    partial_dir.mkdir()
    (partial_dir / "scene-001.png").write_text("only scene-001 exists", encoding="utf-8")
    result = run_asset_resolution_stage(manifest, run, registry, make_provider(tmp_path / "partial-fixtures"))
    assert result.ok is False
    assert "scene-002" in result.error
    assert result.manifest is None
    assert result.artifact is None
    assert run.stage(STAGE_NAME).status.value == "failed"
    # no false-success asset manifest was registered
    assert [a for a in registry.all() if a.kind is ArtifactKind.ASSET_MANIFEST] == []


def test_unhealthy_provider_fails_stage_before_resolution(tmp_path):
    manifest, registry, run_id = fixture_manifest(tmp_path)
    run = make_run(run_id)
    provider = FileBackedAssetProvider(Config.from_env(env={}), tmp_path / "missing-dir")
    result = run_asset_resolution_stage(manifest, run, registry, provider=provider)
    assert result.ok is False
    assert result.provider == "file-backed-fixtures"
    assert "unhealthy" in result.error
    assert run.stage(STAGE_NAME).status.value == "failed"
    # nothing was resolved or copied
    assert not (tmp_path / "run" / "assets").exists()


def test_invalid_scene_manifest_rejected_before_resolution(tmp_path):
    run = make_run()
    registry = make_registry(tmp_path, run.run_id)
    result = run_asset_resolution_stage(
        {"schema_version": "9.0"}, run, registry, provider=make_provider()
    )
    assert result.ok is False
    assert run.stage(STAGE_NAME).status.value == "failed"
    assert result.manifest is None


def test_persistence_failure_does_not_fake_success(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    manifest, _, run_id = fixture_manifest(tmp_path)
    run = make_run()
    registry = ArtifactRegistry(blocker / "run", run.run_id)
    result = run_asset_resolution_stage(manifest, run, registry, make_provider())
    assert result.ok is False
    assert result.manifest is None
    assert result.artifact is None
    assert run.stage(STAGE_NAME).status.value == "failed"


# ---- J. determinism + contract separation ---------------------------------------


def test_same_input_yields_identical_asset_manifest_bytes(tmp_path):
    script = load_script_input(FIXTURES / "script_to_scene" / "documentary.json")
    manifests = []
    for index in range(2):
        run = make_run()
        registry = ArtifactRegistry(tmp_path / f"run-{index}", run.run_id)
        scene_result = run_script_to_scene_stage(script, run, registry)
        assert scene_result.ok
        result = run_asset_resolution_stage(
            scene_result.manifest, run, registry, provider=make_provider()
        )
        assert result.ok, result.error
        manifests.append(result.manifest)
    assert dump_asset_manifest(manifests[0]) == dump_asset_manifest(manifests[1])


def test_resolved_assets_stay_outside_the_scene_contract(tmp_path):
    manifest, registry, run_id = fixture_manifest(tmp_path)
    scene_state_before = json.dumps(
        [s.model_dump() for s in manifest.scenes], sort_keys=True
    )
    run = make_run(run_id)
    result = run_asset_resolution_stage(manifest, run, registry, make_provider())
    assert result.ok
    # the scene contract was not mutated by asset resolution
    assert json.dumps([s.model_dump() for s in manifest.scenes], sort_keys=True) == scene_state_before
    # no resolved-asset fields leaked into the contract models
    for model in (type(manifest), type(manifest.scenes[0]), type(manifest.scenes[0].visual)):
        leaked = {"path", "url", "sha256", "provenance"} & set(model.model_fields)
        assert not leaked, f"resolved-asset field leaked into {model.__name__}"



