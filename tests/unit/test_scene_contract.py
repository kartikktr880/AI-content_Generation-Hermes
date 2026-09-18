"""P1-A canonical Scene Contract tests.

Covers: valid manifests (minimal + documentary), schema versioning,
deterministic serialization, round-trips, invalid inputs, the
intent/resolved-asset separation, and P0 artifact integration.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ayce.artifacts import ArtifactKind, ArtifactRegistry
from ayce.ids import new_run_id
from ayce.scene_contract import (
    SCHEMA_VERSION,
    AssetKind,
    AssetRequirement,
    Narration,
    ProductionManifest,
    Scene,
    VisualIntent,
    dump_manifest,
    load_manifest,
    persist_manifest,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "scene_contract"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_scene(**overrides) -> Scene:
    data = dict(
        scene_id="scene-1",
        sequence=1,
        duration_seconds=4.0,
        narration={"text": "A single scene."},
        visual={"description": "A calm establishing shot."},
    )
    data.update(overrides)
    return Scene.model_validate(data)


def make_manifest(scenes=None) -> ProductionManifest:
    return ProductionManifest(
        production_id="job-20260917T120000Z-000000000000",
        title="Test production",
        scenes=scenes if scenes is not None else (make_scene(),),
    )


# ---- valid cases -----------------------------------------------------------


def test_minimal_fixture_is_valid():
    manifest = ProductionManifest.model_validate(load_fixture("minimal.json"))
    assert manifest.schema_version == SCHEMA_VERSION
    assert len(manifest.scenes) == 1
    assert manifest.scenes[0].sequence == 1


def test_documentary_fixture_is_valid():
    manifest = ProductionManifest.model_validate(load_fixture("documentary.json"))
    assert len(manifest.scenes) == 5
    assert [s.sequence for s in manifest.scenes] == [1, 2, 3, 4, 5]
    assert len({s.scene_id for s in manifest.scenes}) == 5
    # some scenes carry asset requirements, one carries intent only
    kinds = {s.visual.requirement.kind for s in manifest.scenes if s.visual.requirement}
    assert kinds == {AssetKind.VIDEO, AssetKind.IMAGE}
    assert any(s.visual.requirement is None for s in manifest.scenes)


def test_new_manifest_is_version_stamped():
    assert make_manifest().schema_version == SCHEMA_VERSION == "1.0"


def test_multiple_ordered_scenes_valid():
    scenes = (
        make_scene(scene_id="scene-a", sequence=1),
        make_scene(scene_id="scene-b", sequence=2, duration_seconds=2.5),
        make_scene(scene_id="scene-c", sequence=3),
    )
    manifest = make_manifest(scenes)
    assert [s.scene_id for s in manifest.scenes] == ["scene-a", "scene-b", "scene-c"]


def test_serialization_is_deterministic():
    manifest = make_manifest()
    assert dump_manifest(manifest) == dump_manifest(manifest)
    # reload and re-serialize: identical bytes again
    reloaded = ProductionManifest.model_validate_json(dump_manifest(manifest))
    assert dump_manifest(reloaded) == dump_manifest(manifest)


def test_no_runtime_objects_in_serialized_form():
    payload = json.loads(dump_manifest(make_manifest()))
    text = json.dumps(payload)
    assert "datetime" not in text and "0x" not in text  # no python repr leakage


# ---- invalid cases ---------------------------------------------------------


def test_round_trip_preserves_semantics():
    for name in ("minimal.json", "documentary.json"):
        manifest = ProductionManifest.model_validate(load_fixture(name))
        assert ProductionManifest.model_validate_json(dump_manifest(manifest)) == manifest


def test_unsupported_major_schema_version_rejected():
    data = json.loads(dump_manifest(make_manifest()))
    data["schema_version"] = "2.0"
    with pytest.raises(ValidationError, match="unsupported schema_version"):
        ProductionManifest.model_validate(data)


def test_malformed_schema_version_rejected():
    for bad in ("one", "1", "1.0.0-beta", ""):
        data = json.loads(dump_manifest(make_manifest()))
        data["schema_version"] = bad
        with pytest.raises(ValidationError, match="schema_version"):
            ProductionManifest.model_validate(data)


def test_missing_required_field_rejected():
    data = load_fixture("minimal.json")
    del data["scenes"][0]["narration"]
    with pytest.raises(ValidationError):
        ProductionManifest.model_validate(data)


def test_missing_scene_id_rejected():
    data = load_fixture("minimal.json")
    del data["scenes"][0]["scene_id"]
    with pytest.raises(ValidationError):
        ProductionManifest.model_validate(data)


def test_duplicate_scene_id_rejected():
    scenes = (make_scene(scene_id="scene-a", sequence=1), make_scene(scene_id="scene-a", sequence=2))
    with pytest.raises(ValidationError, match="duplicate scene_id"):
        make_manifest(scenes)


def test_sequence_gap_rejected():
    with pytest.raises(ValidationError, match="exactly 1..2"):
        make_manifest(
            (
                make_scene(scene_id="scene-a", sequence=1),
                make_scene(scene_id="scene-b", sequence=3),
            )
        )


def test_sequence_must_start_at_one():
    with pytest.raises(ValidationError):
        make_manifest((make_scene(sequence=2),))


def test_out_of_order_scenes_rejected():
    with pytest.raises(ValidationError, match="ascending"):
        make_manifest(
            (
                make_scene(scene_id="scene-b", sequence=2),
                make_scene(scene_id="scene-a", sequence=1),
            )
        )


def test_non_integer_sequence_rejected():
    with pytest.raises(ValidationError):
        make_scene(sequence="1")


def test_invalid_duration_rejected():
    for bad in (0, -5.0):
        with pytest.raises(ValidationError):
            make_scene(duration_seconds=bad)


def test_empty_narration_rejected():
    with pytest.raises(ValidationError):
        make_scene(narration={"text": "   "})


def test_empty_visual_description_rejected():
    with pytest.raises(ValidationError):
        make_scene(visual={"description": ""})


def test_empty_scenes_list_rejected():
    with pytest.raises(ValidationError):
        make_manifest(scenes=())


def test_invalid_asset_kind_rejected():
    with pytest.raises(ValidationError):
        VisualIntent(
            description="x",
            requirement={"kind": "hologram", "description": "impossible"},
        )


def test_invalid_scene_id_rejected():
    for bad in ("has space", "", "x" * 65):
        with pytest.raises(ValidationError):
            make_scene(scene_id=bad)


def test_malformed_fixture_rejected():
    with pytest.raises(ValidationError):
        ProductionManifest.model_validate(load_fixture("invalid_malformed.json"))


# ---- intent / resolved-asset separation ------------------------------------


def test_intent_without_resolved_asset_is_valid():
    """A scene may express pure creative intent with no asset requirement."""
    scene = make_scene(visual={"description": "Show archival railway footage from colonial India."})
    assert scene.visual.requirement is None
    assert make_manifest((scene,))  # manifest with unresolved intent is valid


def test_resolved_asset_data_is_not_part_of_the_contract():
    """No model in the contract accepts a resolved filesystem path / URL."""
    resolved_fields = {"path", "file", "file_path", "filepath", "url", "uri", "source_url", "local_path"}
    for model in (ProductionManifest, Scene, VisualIntent, AssetRequirement, Narration):
        leaky = set(model.model_fields) & resolved_fields
        assert not leaky, f"resolved-asset field leaked into {model.__name__}"


def test_renderer_fields_cannot_leak_into_contract():
    data = json.loads(dump_manifest(make_manifest()))
    data["scenes"][0]["ffmpeg_filter"] = "scale=1280:720"
    with pytest.raises(ValidationError, match="ffmpeg_filter"):
        ProductionManifest.model_validate(data)


def test_provenance_is_a_requirement_not_a_record():
    req = AssetRequirement(kind=AssetKind.VIDEO, description="archival footage")
    assert req.requires_provenance is True
    # no resolved-provenance fields exist in the model
    assert set(AssetRequirement.model_fields) == {"kind", "description", "requires_provenance"}


# ---- P0 artifact integration ------------------------------------------------


def test_persist_and_reload_through_artifact_registry(tmp_path):
    manifest = ProductionManifest.model_validate(load_fixture("documentary.json"))
    run_id = new_run_id()
    registry = ArtifactRegistry(tmp_path / "run", run_id)

    ref = persist_manifest(manifest, tmp_path / "run", registry, "scene_manifest")

    # the manifest is a real P0 artifact of kind scene_manifest
    assert ref.kind is ArtifactKind.SCENE_MANIFEST
    assert ref.path == "scene_manifest.json"
    assert ref.metadata == {"schema_version": "1.0", "scenes": 5}

    # reload through both layers: registry + contract validation
    reloaded_registry = ArtifactRegistry.load(tmp_path / "run", run_id)
    ref2 = reloaded_registry.require(ref.artifact_id)
    assert ref2.kind is ArtifactKind.SCENE_MANIFEST
    reloaded_manifest = load_manifest(Path(tmp_path / "run") / ref2.path)
    assert reloaded_manifest == manifest




