"""Stage 4 — Sealed publish-package boundary tests.

Covers: seal success (full package shape), QA gating (FAIL / missing),
render hash integrity, artifact mutation after seal, package tampering,
lineage gating, idempotency + determinism, fixture-run refusal, the full
Golden Path acceptance chain (objective → research → script → run → QA →
package → verification), and the CLI. Real pipeline runs (real FFmpeg)
mirror test_lineage/test_pipeline conventions.
"""

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from ayce.artifacts import ArtifactKind, ArtifactRegistry
from ayce.config import Config
from ayce.lineage import objective_id_for
from ayce.pipeline import run_pipeline
from ayce.publish_package import (
    PACKAGE_FILENAME,
    PACKAGE_VERSION,
    PackageError,
    load_publish_package,
    resolve_run_dir,
    seal_publish_package,
    verify_publish_package,
)
from ayce.state import RunState

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DOCUMENTARY = FIXTURES / "script_to_scene" / "documentary.json"
ASSETS_DIR = FIXTURES / "asset_provider"
NARRATION_DIR = FIXTURES / "narration_fixtures"

RESEARCH_ID = "res-20260920T090318Z-e8f295ac6ec8"
SCRIPT_ID = "brief-e8f295ac6ec8"
OBJECTIVE = "Identify outlier opening-hook formats in the AI productivity tools niche"


def make_config(tmp_path: Path) -> Config:
    return replace(Config.from_env(env={}), data_dir=tmp_path / "data")


def _write_research_artifact(tmp_path: Path) -> str:
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
    (store / f"{RESEARCH_ID}.json").write_text(json.dumps(artifact), encoding="utf-8")
    return objective_id_for(OBJECTIVE)


def _write_generated_script(tmp_path: Path) -> Path:
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
    path.write_text(json.dumps(script), encoding="utf-8")
    return path


@pytest.fixture()
def golden_run(tmp_path):
    """One REAL research-derived Golden Path run (objective→…→QA PASS)."""
    objective_id = _write_research_artifact(tmp_path)
    script_path = _write_generated_script(tmp_path)
    config = make_config(tmp_path)
    result = run_pipeline(script_path, config=config,
                          assets_dir=ASSETS_DIR, narration_dir=NARRATION_DIR)
    assert result.ok and result.qa_verdict == "PASS", result.error
    return {"tmp_path": tmp_path, "config": config, "result": result,
            "objective_id": objective_id}


# ---- seal success: full self-describing package ---------------------------------------


def test_seal_success_package_is_complete_and_self_describing(golden_run):
    result = golden_run["result"]
    package = seal_publish_package(result.run_dir)

    assert package["package_version"] == PACKAGE_VERSION == 1
    assert package["package_id"].startswith("pkg-") and len(package["package_id"]) == 4 + 16
    assert package["run_id"] == result.run_id
    assert package["job_id"] == result.job_id
    assert package["production_id"] == RESEARCH_ID      # research-derived production
    # lineage (EXACT Stage 3 field names), complete
    assert package["lineage"]["objective_id"] == golden_run["objective_id"]
    assert package["lineage"]["research_id"] == RESEARCH_ID
    assert package["lineage"]["script_id"] == SCRIPT_ID
    # render identity + QA attestation, bound to each other
    assert package["render"]["kind"] == "rendered_video"
    assert len(package["render"]["sha256"]) == 64
    assert package["qa"]["verdict"] == "PASS"
    assert package["qa"]["render_sha256"] == package["render"]["sha256"]
    # artifact manifest: the whole run (7 production kinds + script + research)
    kinds = {e["kind"] for e in package["artifact_manifest"]}
    assert kinds == {"scene_manifest", "asset_manifest", "audio", "timeline",
                     "captions", "rendered_video", "qa_report", "script", "research"}
    for entry in package["artifact_manifest"]:
        assert len(entry["sha256"]) == 64 and entry["size"] > 0
    # NO payload copies: no media/research/script payloads inside the package
    package_text = json.dumps(package)
    assert "candidates" not in package_text
    assert "narration_text" not in package_text
    # seal present and persisted inside the originating run directory
    assert package["seal"]["algorithm"] == "sha256"
    assert len(package["seal"]["value"]) == 64
    assert load_publish_package(result.run_dir) == package


def test_verify_passes_full_content_verification(golden_run):
    result = golden_run["result"]
    package = seal_publish_package(result.run_dir)
    report = verify_publish_package(package, run_dir=result.run_dir)
    assert report["ok"] is True and report["errors"] == []
    assert report["verified_content"] is True
    assert report["package_id"] == package["package_id"]
    # seal-only (structural) verification without a run dir also passes
    assert verify_publish_package(package)["ok"] is True


def test_seal_is_idempotent_and_deterministic(golden_run):
    result = golden_run["result"]
    first = seal_publish_package(result.run_dir)
    second = seal_publish_package(result.run_dir)   # re-seal: reuse, no conflict
    third = seal_publish_package(result.run_dir)
    assert first == second == third
    assert json.loads((result.run_dir / PACKAGE_FILENAME).read_text(
        encoding="utf-8")) == first
    # content-derived: a byte-identical COPY of the run yields the same
    # package_id and seal (no randomness anywhere)
    copy_dir = golden_run["tmp_path"] / "run-copy"
    shutil.copytree(result.run_dir, copy_dir)
    copied = seal_publish_package(copy_dir)
    assert copied["package_id"] == first["package_id"]
    assert copied["seal"] == first["seal"]


def test_seal_refuses_render_hash_mismatch(golden_run):
    result = golden_run["result"]
    registry = ArtifactRegistry.load(result.run_dir, result.run_id)
    render_ref = [r for r in registry.all() if r.kind is ArtifactKind.RENDERED_VIDEO][0]
    render_file = result.run_dir / render_ref.path
    render_file.write_bytes(render_file.read_bytes() + b"\x00")  # mutate media copy
    with pytest.raises(PackageError) as excinfo:
        seal_publish_package(result.run_dir)
    assert excinfo.value.code == "render_hash_mismatch"


def test_verify_detects_artifact_mutation_after_seal(golden_run):
    result = golden_run["result"]
    package = seal_publish_package(result.run_dir)
    assert verify_publish_package(package, run_dir=result.run_dir)["ok"] is True
    # tamper with a PACKAGED artifact (captions) in a TEMP copy — the
    # canonical run directory is never damaged
    copy_dir = golden_run["tmp_path"] / "tampered"
    shutil.copytree(result.run_dir, copy_dir)
    captions = copy_dir / "captions.json"
    captions.write_bytes(captions.read_bytes() + b"\n<!-- tampered -->\n")
    report = verify_publish_package(package, run_dir=copy_dir)
    assert report["ok"] is False
    assert "artifact_hash_mismatch" in {e["code"] for e in report["errors"]}


def test_verify_detects_missing_artifact(golden_run):
    result = golden_run["result"]
    package = seal_publish_package(result.run_dir)
    copy_dir = golden_run["tmp_path"] / "gutted"
    shutil.copytree(result.run_dir, copy_dir)
    (copy_dir / "captions.json").unlink()
    report = verify_publish_package(package, run_dir=copy_dir)
    assert report["ok"] is False
    assert {e["code"] for e in report["errors"]} == {"artifact_missing"}


def test_verify_detects_package_tampering(golden_run):
    result = golden_run["result"]
    package = seal_publish_package(result.run_dir)
    tampered = dict(package)
    tampered["production_id"] = "res-99990101T000000Z-tamper000001"  # ANY change
    report = verify_publish_package(tampered, run_dir=result.run_dir)
    assert report["ok"] is False
    assert {e["code"] for e in report["errors"]} >= {"package_seal_mismatch"}


def test_verify_detects_lineage_mismatch(golden_run):
    result = golden_run["result"]
    package = seal_publish_package(result.run_dir)
    # a VALIDLY re-sealed package with altered lineage (white-box: rebuilt
    # through the module's own canonical helpers) must FAIL content
    # verification against the run's persisted lineage
    from ayce.publish_package import _UNSEALED_FIELDS, _seal_core
    core = {k: v for k, v in package.items() if k not in _UNSEALED_FIELDS}
    core["lineage"] = {**core["lineage"],
                       "research_id": "res-99990101T000000Z-forged000001"}
    forged = dict(core)
    forged["package_id"] = "pkg-" + _seal_core(core)[:16]
    forged["seal"] = {"algorithm": "sha256", "value": _seal_core(forged)}
    report = verify_publish_package(forged, run_dir=result.run_dir)
    assert report["ok"] is False
    assert {e["code"] for e in report["errors"]} == {"lineage_invalid"}


# ---- lineage gate ------------------------------------------------------------------------


def test_seal_refuses_fixture_run_without_lineage(tmp_path):
    """A manual/fixture run (job- production id, no lineage) is not
    publishable: the package boundary demands full traceability."""
    config = make_config(tmp_path)
    result = run_pipeline(DOCUMENTARY, config=config,
                          assets_dir=ASSETS_DIR, narration_dir=NARRATION_DIR)
    assert result.ok and result.lineage is None
    with pytest.raises(PackageError) as excinfo:
        seal_publish_package(result.run_dir)
    assert excinfo.value.code == "lineage_invalid"


# ---- run resolution safety -----------------------------------------------------------------


def test_resolve_run_dir_rejects_paths_and_unknown_runs(tmp_path):
    config = make_config(tmp_path)
    with pytest.raises(PackageError) as traversal:
        resolve_run_dir(config, "../data")
    assert traversal.value.code == "run_not_found"
    with pytest.raises(PackageError) as unknown:
        resolve_run_dir(config, "run-20990101T000000Z-missing00001")
    assert unknown.value.code == "run_not_found"


# ---- QA gate (not bypassable) ----------------------------------------------------------


def test_seal_refuses_qa_fail(golden_run):
    result = golden_run["result"]
    qa_path = result.run_dir / "qa_report.json"
    qa = json.loads(qa_path.read_text(encoding="utf-8"))
    qa["verdict"] = "FAIL"                              # simulated QA failure
    qa_path.write_text(json.dumps(qa), encoding="utf-8")
    with pytest.raises(PackageError) as excinfo:
        seal_publish_package(result.run_dir)
    assert excinfo.value.code == "qa_failed"
    assert not (result.run_dir / PACKAGE_FILENAME).exists()


def test_seal_refuses_missing_qa(golden_run):
    result = golden_run["result"]
    (result.run_dir / "qa_report.json").unlink()
    with pytest.raises(PackageError) as excinfo:
        seal_publish_package(result.run_dir)
    assert excinfo.value.code == "qa_missing"


def test_seal_refuses_incomplete_run(golden_run):
    result = golden_run["result"]
    state_path = result.run_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    del state["stages"]["media_qa"]                     # simulate a partial run
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(PackageError) as excinfo:
        seal_publish_package(result.run_dir)
    assert excinfo.value.code == "run_not_successful"


# ---- hash / artifact integrity (render duplicate covered above) -------------------------


# ---- full Golden Path acceptance (Section 22) -----------------------------------------------


def test_full_golden_path_objective_to_verified_package(golden_run):
    """OBJECTIVE -> RESEARCH -> SCRIPT -> RUN -> 7/7 -> QA PASS -> PACKAGE ->
    VERIFY, reconstructed ENTIRELY from persisted files (the canonical
    Golden Path v0 acceptance)."""
    result = golden_run["result"]
    package = seal_publish_package(result.run_dir)
    report = verify_publish_package(package, run_dir=result.run_dir)
    assert report["ok"] is True and report["verified_content"] is True
    # the chain, read back from disk only
    state = RunState.load(result.run_dir / "state.json")
    assert state.lineage["objective_id"] == objective_id_for(OBJECTIVE)
    assert state.lineage["research_id"] == RESEARCH_ID
    assert state.lineage["script_id"] == SCRIPT_ID
    assert package["run_id"] == result.run_id
    assert package["lineage"] == state.lineage
    assert package["qa"]["verdict"] == "PASS"
    assert package["render"]["sha256"] == package["qa"]["render_sha256"]


# ---- CLI (seal + verify through the real CLI handler) ----------------------------------------


def test_cli_package_seal_and_verify(golden_run, capsys, monkeypatch):
    from ayce import cli
    run_id = golden_run["result"].run_id
    monkeypatch.setenv("AYCE_DATA_DIR", str(golden_run["tmp_path"] / "data"))

    args = cli._build_parser().parse_args(["package", run_id, "--json"])
    assert cli._cmd_package(args) == 0
    sealed = json.loads(capsys.readouterr().out)
    assert sealed["ok"] is True and sealed["verified_content"] is True

    args = cli._build_parser().parse_args(["package", run_id, "--verify", "--json"])
    assert cli._cmd_package(args) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["ok"] is True
    assert verified["package_id"] == sealed["package_id"]

    # QA gating through the CLI too (truthful failure, exit 1)
    qa_path = golden_run["result"].run_dir / "qa_report.json"
    qa = json.loads(qa_path.read_text(encoding="utf-8"))
    qa["verdict"] = "FAIL"
    qa_path.write_text(json.dumps(qa), encoding="utf-8")
    # re-seal attempt after a simulated QA failure: refused, nothing mutated
    args = cli._build_parser().parse_args(["package", run_id, "--json"])
    assert cli._cmd_package(args) == 1
    refusal = json.loads(capsys.readouterr().out)
    assert refusal["error"]["code"] == "qa_failed"
    package = load_publish_package(golden_run["result"].run_dir)
    assert package["qa"]["verdict"] == "PASS"   # gate never mutated the seal
    # and the verifier now truthfully detects that the QA evidence file
    # changed after sealing (the mutation cannot hide from the seal)
    after = verify_publish_package(package, run_dir=golden_run["result"].run_dir)
    assert after["ok"] is False
    assert "artifact_hash_mismatch" in {e["code"] for e in after["errors"]}
