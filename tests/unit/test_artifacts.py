import pytest

from ayce.artifacts import ArtifactError, ArtifactKind, ArtifactRef, ArtifactRegistry


def test_register_and_lookup(tmp_path):
    reg = ArtifactRegistry(tmp_path / "run-1", run_id="run-1")
    ref = reg.register("script", "script", "script/script.md", {"title": "t"})
    assert ref.kind is ArtifactKind.SCRIPT
    assert ref.path == "script/script.md"
    assert reg.require(ref.artifact_id) is ref
    assert [a.artifact_id for a in reg.for_stage("script")] == [ref.artifact_id]


def test_unknown_kind_rejected(tmp_path):
    reg = ArtifactRegistry(tmp_path / "run-1", run_id="run-1")
    with pytest.raises(ArtifactError, match="unknown artifact kind"):
        reg.register("audio", "hologram", "audio/n/a.wav")


def test_absolute_path_rejected(tmp_path):
    reg = ArtifactRegistry(tmp_path / "run-1", run_id="run-1")
    with pytest.raises(ArtifactError, match="relative"):
        ArtifactRef(
            artifact_id="art-1",
            run_id="run-1",
            stage="audio",
            kind=ArtifactKind.AUDIO,
            path=str(tmp_path / "abs" / "narration.mp3"),
        )


def test_path_traversal_rejected(tmp_path):
    reg = ArtifactRegistry(tmp_path / "run-1", run_id="run-1")
    with pytest.raises(ArtifactError, match="unsafe artifact path"):
        reg.register("audio", ArtifactKind.AUDIO, "../escape/narration.mp3")


def test_missing_artifact_raises(tmp_path):
    reg = ArtifactRegistry(tmp_path / "run-1", run_id="run-1")
    with pytest.raises(ArtifactError, match="unknown artifact"):
        reg.require("art-does-not-exist")


def test_manifest_persists_and_reloads(tmp_path):
    run_dir = tmp_path / "run-1"
    reg = ArtifactRegistry(run_dir, run_id="run-1")
    ref = reg.register("qa", "qa_report", "qa/report.json", {"verdict": "pass"})

    reloaded = ArtifactRegistry.load(run_dir, run_id="run-1")
    assert reloaded.require(ref.artifact_id).metadata == {"verdict": "pass"}
    assert reloaded.get(ref.artifact_id).kind is ArtifactKind.QA_REPORT


def test_corrupt_manifest_raises(tmp_path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    (run_dir / "artifacts.json").write_text("[broken", encoding="utf-8")
    with pytest.raises(ArtifactError, match="corrupt"):
        ArtifactRegistry.load(run_dir, run_id="run-1")
