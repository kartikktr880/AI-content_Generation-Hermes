"""Stage 3 — Objective lineage tests.

Covers: old-state compatibility, RESEARCH/SCRIPT artifact registration,
research→script and script→run lineage, objective lineage, full forward
chain reconstruction from PERSISTED files, reverse lookup, fixture-run
backward compatibility, registration idempotency, and existing registry
behavior. Real pipeline runs (real FFmpeg) mirror test_pipeline conventions.
"""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from ayce.artifacts import ArtifactKind, ArtifactRegistry
from ayce.config import Config
from ayce.lineage import (
    LINEAGE_STAGE_RESEARCH,
    LINEAGE_STAGE_SCRIPT,
    RESEARCH_REFERENCE_FILENAME,
    SCRIPT_INPUT_FILENAME,
    ProductionLineage,
    find_runs,
    lineage_for_run,
    objective_id_for,
    register_lineage,
)
from ayce.pipeline import PIPELINE_STAGES, STATE_FILENAME, run_pipeline
from ayce.state import RunState, StateError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DOCUMENTARY = FIXTURES / "script_to_scene" / "documentary.json"
ASSETS_DIR = FIXTURES / "asset_provider"
NARRATION_DIR = FIXTURES / "narration_fixtures"

RESEARCH_ID = "res-20260920T090318Z-e8f295ac6ec8"
SCRIPT_ID = "brief-e8f295ac6ec8"
OBJECTIVE = "Identify outlier opening-hook formats in the AI productivity tools niche"


def make_config(tmp_path: Path) -> Config:
    """Real config with an isolated data dir (mirrors test_pipeline)."""
    return replace(Config.from_env(env={}), data_dir=tmp_path / "data")


def _write_research_artifact(tmp_path: Path) -> dict:
    """A real-shaped ResearchArtifact in the controlled research store."""
    artifact = {
        "schema_version": "1.0",
        "research_id": RESEARCH_ID,
        "objective": OBJECTIVE,
        "niche": "AI productivity tools",
        "query": "AI productivity tools",
        "collected_at": "2026-09-20T09:03:18.260Z",
        "status": "PARTIAL",
        "tiers": ["OBSERVED_FACT", "DERIVED_METRIC", "MODEL_INFERENCE", "CREATIVE_HYPOTHESIS"],
        "candidates": [{
            "video_id": "aaa123456789",
            "url": "https://www.youtube.com/watch?v=aaa123456789",
            "title": "Title aaa",
            "channel": "Chan",
            "published_at": "2026-01-01",
            "view_count": 1000,
            "hook_summary": "A recorded hook.",
            "hook_confidence": 0.7,
            "outlier_multiplier": None,
            "velocity_proxy": 5.0,
            "cluster_id": 0,
            "confidence": 0.8,
            "provenance": {"ingested_via": "ytsearch1:test",
                           "captions_status": "auto_generated"},
        }],
        "provenance": {"worker_version": "1.0",
                       "collected_at": "2026-09-20T09:03:18.260Z",
                       "request_digest": "ab" * 32},
    }
    store = tmp_path / "data" / "research"
    store.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(artifact, indent=2).encode("utf-8")
    (store / f"{RESEARCH_ID}.json").write_bytes(raw)
    return {"raw": raw, "sha256": hashlib.sha256(raw).hexdigest(),
            "objective_id": objective_id_for(OBJECTIVE)}


def _write_generated_script(tmp_path: Path) -> tuple[Path, bytes]:
    """A bridge-shaped ScriptInput: production_id = research_id."""
    script = {
        "production_id": RESEARCH_ID,
        "title": "Research brief: Identify outlier opening-hook formats",
        "scenes": [
            {"narration_text": "Here is an opening hook recorded by the research worker.",
             "visual_description": "High-contrast opening title card."},
            {"narration_text": "This video is built directly from research evidence.",
             "visual_description": "Clean text card naming the research objective."},
        ],
    }
    path = tmp_path / "script.json"
    raw = json.dumps(script, indent=2).encode("utf-8")
    path.write_bytes(raw)
    return path, raw


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---- Test 1: old state compatibility -----------------------------------------------


def test_old_state_without_lineage_still_loads(tmp_path):
    legacy = {
        "run_id": "run-20260101T000000Z-legacy00001",
        "job_id": "job-20260101T000000Z-legacy00001",
        "created_at": "2026-01-01T00:00:00.000Z",
        "updated_at": "2026-01-01T00:00:00.000Z",
        "stages": {"script_to_scene": {"status": "succeeded", "attempts": 1,
                                       "updated_at": "2026-01-01T00:00:00.000Z",
                                       "last_error": None}},
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    state = RunState.load(path)
    assert state.lineage is None                      # absent → None (old runs)
    # re-saving legacy state keeps its historical shape (no lineage key)
    resaved = state.save(tmp_path / "resaved.json")
    assert "lineage" not in json.loads(resaved.read_text(encoding="utf-8"))


def test_state_with_lineage_round_trips(tmp_path):
    lineage = {"script_id": SCRIPT_ID, "script_sha256": "a" * 64,
               "research_id": RESEARCH_ID, "research_sha256": "b" * 64,
               "objective_id": "obj-" + "c" * 16}
    state = RunState(run_id="run-20260101T000000Z-lineage0001",
                     job_id="job-20260101T000000Z-lineage0001", lineage=lineage)
    path = state.save(tmp_path / "state.json")
    assert RunState.load(path).lineage == lineage


def test_corrupt_lineage_shape_is_rejected():
    with pytest.raises(StateError):
        RunState.from_dict({"run_id": "run-x", "job_id": "job-x",
                            "lineage": "not-an-object"})


# ---- lineage helper determinism ------------------------------------------------------


def test_objective_id_is_content_addressed_and_deterministic():
    first = objective_id_for(OBJECTIVE)
    assert first == objective_id_for(f"  {OBJECTIVE}  ")   # whitespace-normalized
    assert first.startswith("obj-") and len(first) == 4 + 16
    assert first != objective_id_for("a different objective")


def test_production_lineage_dict_projection_omits_nones():
    lineage = ProductionLineage(script_sha256="a" * 64, research_id=RESEARCH_ID)
    assert lineage.to_dict() == {"script_sha256": "a" * 64, "research_id": RESEARCH_ID}
    assert ProductionLineage.from_dict(lineage.to_dict()) == lineage
    assert ProductionLineage.from_dict(None) is None
    assert ProductionLineage.from_dict("garbage") is None


# ---- discover: fixture scripts get NO lineage (backward compatibility) ---------------


def test_discover_returns_none_for_fixture_script(tmp_path):
    from ayce.script_to_scene import load_script_input
    config = make_config(tmp_path)
    script_input = load_script_input(DOCUMENTARY)     # production_id = job-…
    assert _discover(config, script_input) is None


def _discover(config, script_input, script_bytes=None):
    from ayce.lineage import discover
    return discover(script_input, config=config, script_bytes=script_bytes)


# ---- Tests 2-8: one REAL research-derived pipeline run proves the chain --------------


@pytest.fixture()
def lineage_run(tmp_path):
    """One real Golden Path run over a research-derived generated script."""
    research = _write_research_artifact(tmp_path)
    script_path, script_bytes = _write_generated_script(tmp_path)
    config = make_config(tmp_path)
    result = run_pipeline(
        script_path,
        config=config,
        assets_dir=ASSETS_DIR,
        narration_dir=NARRATION_DIR,
    )
    return {"tmp_path": tmp_path, "config": config, "result": result,
            "research": research, "script_bytes": script_bytes}


def test_research_derived_run_succeeds_with_lineage(lineage_run):
    result = lineage_run["result"]
    assert result.ok, result.error
    assert [o.stage for o in result.stages] == list(PIPELINE_STAGES)  # 7 stages, unchanged
    assert result.qa_verdict == "PASS"
    assert result.lineage is not None
    assert result.lineage["research_id"] == RESEARCH_ID
    assert result.lineage["script_id"] == SCRIPT_ID


def test_script_artifact_registered_with_byte_exact_copy(lineage_run):
    result = lineage_run["result"]
    registry = ArtifactRegistry.load(result.run_dir, result.run_id)
    refs = registry.for_stage(LINEAGE_STAGE_SCRIPT)
    assert len(refs) == 1
    ref = refs[0]
    assert ref.kind is ArtifactKind.SCRIPT
    persisted = (result.run_dir / SCRIPT_INPUT_FILENAME).read_bytes()
    assert persisted == lineage_run["script_bytes"]          # byte-exact input copy
    assert ref.metadata["script_sha256"] == _sha(lineage_run["script_bytes"])
    assert ref.metadata["research_id"] == RESEARCH_ID
    assert ref.metadata["script_id"] == SCRIPT_ID


def test_research_artifact_registered_as_bounded_reference(lineage_run):
    result = lineage_run["result"]
    research = lineage_run["research"]
    registry = ArtifactRegistry.load(result.run_dir, result.run_id)
    refs = registry.for_stage(LINEAGE_STAGE_RESEARCH)
    assert len(refs) == 1
    ref = refs[0]
    assert ref.kind is ArtifactKind.RESEARCH
    reference = json.loads(
        (result.run_dir / RESEARCH_REFERENCE_FILENAME).read_text(encoding="utf-8"))
    assert reference["research_id"] == RESEARCH_ID
    assert reference["research_sha256"] == research["sha256"]  # checksum preserved
    assert reference["objective_id"] == research["objective_id"]
    assert reference["status"] == "PARTIAL"
    assert reference["candidate_count"] == 1
    # bounded reference, NOT a payload copy: no candidates/source lists inside
    assert "candidates" not in reference
    assert ref.metadata["reference_only"] is True


def test_full_forward_lineage_reconstructed_from_persisted_files_only(lineage_run):
    """OBJECTIVE → RESEARCH → SCRIPT → RUN → QA, read back from disk only."""
    result = lineage_run["result"]
    research = lineage_run["research"]
    # every hop from PERSISTED JSON (no in-memory objects)
    state = RunState.load(result.run_dir / STATE_FILENAME)
    lineage = state.lineage
    assert lineage["objective_id"] == research["objective_id"]       # objective
    assert lineage["objective_id"] == objective_id_for(OBJECTIVE)
    assert lineage["research_id"] == RESEARCH_ID                     # research
    assert lineage["research_sha256"] == research["sha256"]
    assert lineage["script_id"] == SCRIPT_ID                          # script
    assert lineage["script_sha256"] == _sha(lineage_run["script_bytes"])
    assert lineage_for_run(result.run_dir) == lineage                 # run
    # QA bound to the same run via the existing registry
    registry = ArtifactRegistry.load(result.run_dir, result.run_id)
    qa_ref = registry.require(result.qa_artifact_id)
    assert qa_ref.kind is ArtifactKind.QA_REPORT
    qa = json.loads((result.run_dir / "qa_report.json").read_text(encoding="utf-8"))
    assert qa["verdict"] == "PASS"


# ---- Test 8: reverse lookup -----------------------------------------------------------


def test_reverse_lookup_run_to_script_research_objective(lineage_run):
    result = lineage_run["result"]
    reverse = lineage_for_run(result.run_dir)
    assert reverse["script_id"] == SCRIPT_ID
    assert reverse["research_id"] == RESEARCH_ID
    assert reverse["objective_id"] == lineage_run["research"]["objective_id"]


def test_find_runs_by_research_script_and_objective(lineage_run):
    data_dir = lineage_run["tmp_path"] / "data"
    by_research = find_runs(data_dir, research_id=RESEARCH_ID)
    assert [m["run_id"] for m in by_research] == [lineage_run["result"].run_id]
    by_script = find_runs(data_dir, script_id=SCRIPT_ID)
    assert len(by_script) == 1
    by_objective = find_runs(data_dir, objective_id=lineage_run["research"]["objective_id"])
    assert len(by_objective) == 1
    # no false matches for unknown identities
    assert find_runs(data_dir, research_id="res-20990101T000000Z-nothing00001") == []
    assert find_runs(data_dir, objective_id="obj-" + "0" * 16) == []


# ---- Test 9: fixture runs stay byte-compatible (no lineage, no extra artifacts) ------


def test_fixture_run_has_no_lineage_and_unchanged_artifact_kinds(tmp_path):
    from ayce.script_to_scene import load_script_input
    config = make_config(tmp_path)
    # the production_id of the documentary fixture is a job id → no lineage
    script_input = load_script_input(DOCUMENTARY)
    assert script_input.production_id.startswith("job-")

    result = run_pipeline(
        DOCUMENTARY, config=config,
        assets_dir=ASSETS_DIR, narration_dir=NARRATION_DIR,
    )
    assert result.ok and result.qa_verdict == "PASS"
    assert result.lineage is None                      # no lineage for fixture runs
    state = RunState.load(result.run_dir / STATE_FILENAME)
    assert state.lineage is None
    assert "lineage" not in json.loads(
        (result.run_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    registry = ArtifactRegistry.load(result.run_dir, result.run_id)
    kinds = {ref.kind for ref in registry.all()}
    assert kinds == {                                   # EXACT pre-Stage-3 kind set
        ArtifactKind.SCENE_MANIFEST, ArtifactKind.ASSET_MANIFEST,
        ArtifactKind.AUDIO, ArtifactKind.TIMELINE, ArtifactKind.CAPTIONS,
        ArtifactKind.RENDERED_VIDEO, ArtifactKind.QA_REPORT,
    }
    assert not (result.run_dir / SCRIPT_INPUT_FILENAME).exists()
    assert not (result.run_dir / RESEARCH_REFERENCE_FILENAME).exists()


# ---- Test 10: lineage registration idempotency ----------------------------------------


def test_lineage_registration_is_idempotent(tmp_path):
    research = _write_research_artifact(tmp_path)
    script_path, script_bytes = _write_generated_script(tmp_path)
    config = make_config(tmp_path)
    run = RunState(run_id="run-20260101T000000Z-lineage0002",
                   job_id="job-20260101T000000Z-lineage0002")
    registry = ArtifactRegistry(tmp_path / "run", run.run_id)
    from ayce.lineage import discover
    from ayce.script_to_scene import load_script_input
    lineage = discover(load_script_input(script_path), config=config,
                       script_bytes=script_bytes)
    assert lineage is not None

    register_lineage(run, registry, lineage, script_bytes)
    register_lineage(run, registry, lineage, script_bytes)  # duplicate: no-op
    register_lineage(run, registry, lineage, script_bytes)

    assert len(registry.for_stage(LINEAGE_STAGE_SCRIPT)) == 1
    assert len(registry.for_stage(LINEAGE_STAGE_RESEARCH)) == 1
    reloaded = ArtifactRegistry.load(tmp_path / "run", run.run_id)
    assert len(reloaded.for_stage(LINEAGE_STAGE_SCRIPT)) == 1
    assert len(reloaded.for_stage(LINEAGE_STAGE_RESEARCH)) == 1
    # content digests unchanged
    assert _sha((tmp_path / "run" / SCRIPT_INPUT_FILENAME).read_bytes()) == _sha(script_bytes)
    assert json.loads(
        (tmp_path / "run" / RESEARCH_REFERENCE_FILENAME).read_text(encoding="utf-8")
    )["research_sha256"] == research["sha256"]


# ---- Test 11 support: degraded research resolution is truthful, never fabricated ------


def test_missing_research_artifact_degrades_to_none_hashes(tmp_path):
    script_path, script_bytes = _write_generated_script(tmp_path)  # store NOT written
    config = make_config(tmp_path)
    from ayce.script_to_scene import load_script_input
    lineage = _discover(config, load_script_input(script_path), script_bytes=script_bytes)
    assert lineage is not None                       # id-level references survive
    assert lineage.research_id == RESEARCH_ID
    assert lineage.script_id == SCRIPT_ID
    assert lineage.research_sha256 is None           # unresolvable → None, not invented
    assert lineage.objective_id is None
    assert lineage.research_reference is None

