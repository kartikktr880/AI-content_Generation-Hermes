"""Stage 2.5 — Research → Brief bridge tests.

Determinism, compatibility with the EXISTING ScriptInput model,
selection-rule ordering, sparse-artifact truthfulness, evidence
preservation (no fabricated numbers), conversion through the existing
script_to_scene stage, and the `--brief` CLI contract.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ayce.artifacts import ArtifactRegistry
from ayce.ids import new_job_id, new_run_id
from ayce.research.brief import (
    BriefError,
    build_script_input,
    load_research_artifact,
    rank_candidates,
)
from ayce.research.models import ResearchArtifact
from ayce.script_to_scene import (
    ScriptInput,
    build_manifest,
    estimate_scene_duration,
    run_script_to_scene_stage,
)
from ayce.state import RunState

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC = PROJECT_ROOT / "src"


# ---- helpers (mirror the real ResearchArtifact shape) ------------------------


def _candidate(video_id, *, mult=None, hook=None, hook_conf=0.0, views=None,
               velocity=None, cluster=None, channel="Chan", title=None,
               hypotheses=None):
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": title or f"Title {video_id}",
        "channel": channel,
        "published_at": "2026-01-01",
        "view_count": views,
        "hook_summary": hook,
        "hook_confidence": hook_conf,
        "outlier_multiplier": mult,
        "velocity_proxy": velocity,
        "cluster_id": cluster,
        "confidence": 0.8,
        "observed_facts": [f"view_count: {views}"] if views is not None else [],
        "creative_hypotheses": list(hypotheses or []),
        "provenance": {
            "ingested_via": "ytsearch2:test",
            "captions_status": "auto_generated",
        },
    }


def _artifact_dict(candidates, **overrides):
    payload = {
        "schema_version": "1.0",
        "research_id": "res-20260920T000000Z-test0000001",
        "objective": "Identify outlier opening-hook formats in the AI productivity niche",
        "niche": "AI productivity tools",
        "query": "AI productivity tools",
        "collected_at": "2026-09-20T00:00:00.000Z",
        "status": "SUCCESS",
        "tiers": ["OBSERVED_FACT", "DERIVED_METRIC", "MODEL_INFERENCE", "CREATIVE_HYPOTHESIS"],
        "candidates": candidates,
        "provenance": {
            "worker_version": "1.0",
            "collected_at": "2026-09-20T00:00:00.000Z",
            "request_digest": "ab" * 32,
        },
    }
    payload.update(overrides)
    return payload


def _artifact(candidates, **overrides):
    return ResearchArtifact.model_validate(_artifact_dict(candidates, **overrides))


def _narrations(script_input):
    return [s.narration_text for s in script_input.scenes]


# ---- Test 1: determinism ------------------------------------------------------


def test_determinism_byte_identical():
    payload = _artifact_dict([
        _candidate("aaa", hook="A hook line.", views=1000, velocity=1.5),
        _candidate("bbb", views=2000),
    ])
    a = build_script_input(ResearchArtifact.model_validate(payload))
    b = build_script_input(ResearchArtifact.model_validate(payload))
    assert a.script_input.model_dump_json() == b.script_input.model_dump_json()
    assert a.ranked_video_ids == b.ranked_video_ids
    assert a.warnings == b.warnings


# ---- Test 2: validates against the REAL existing ScriptInput model ------------


def test_output_validates_against_existing_script_input_model():
    result = build_script_input(_artifact([_candidate("aaa", hook="Hook.", views=100)]))
    raw = result.script_input.model_dump_json()
    reloaded = ScriptInput.model_validate_json(raw)
    assert reloaded == result.script_input
    # production identity stays traceable to the research artifact
    assert reloaded.production_id == "res-20260920T000000Z-test0000001"
    # the brief never sets durations — the existing WPM policy owns timing
    assert all(s.duration_seconds is None for s in reloaded.scenes)


# ---- Test 3: deterministic selection rules ------------------------------------


def test_outlier_multiplier_orders_first():
    ranked = rank_candidates([
        _candidate("ccc", mult=None, views=500000),
        _candidate("bbb", mult=1.2),
        _candidate("aaa", mult=4.0),
    ])
    assert [c.video_id for c in ranked] == ["aaa", "bbb", "ccc"]


def test_hook_then_confidence_then_velocity_then_id_tie_breaks():
    # same multiplier: a usable hook outranks bare hook_confidence
    ranked = rank_candidates([
        _candidate("zzz", mult=2.0, hook_conf=0.9),
        _candidate("mmm", mult=2.0, hook="has hook", hook_conf=0.1),
    ])
    assert ranked[0].video_id == "mmm"
    # same multiplier, both hooked: higher hook_confidence first
    ranked = rank_candidates([
        _candidate("low", mult=2.0, hook="a", hook_conf=0.2),
        _candidate("high", mult=2.0, hook="b", hook_conf=0.8),
    ])
    assert ranked[0].video_id == "high"
    # same multiplier + hook state: velocity proxy descends
    ranked = rank_candidates([
        _candidate("slow", mult=2.0, hook="h", hook_conf=0.5, velocity=1.0),
        _candidate("fast", mult=2.0, hook="h", hook_conf=0.5, velocity=9.0),
    ])
    assert ranked[0].video_id == "fast"
    # full tie: video_id ascending (total order, no input-order dependence)
    ranked = rank_candidates([_candidate("zzz"), _candidate("aaa")])
    assert [c.video_id for c in ranked] == ["aaa", "zzz"]


# ---- Test 4: sparse artifacts handled truthfully -------------------------------


def test_sparse_artifact_still_produces_valid_script():
    cands = [
        _candidate("vvv", mult=None, hook=None, views=None, velocity=None,
                   cluster=None, channel=None),
        _candidate("www", mult=None, hook=None, views=None, velocity=None,
                   cluster=None, channel=None),
    ]
    result = build_script_input(_artifact(cands, status="PARTIAL"))
    # degradation is recorded truthfully, never hidden
    assert any("outlier_multiplier" in w for w in result.warnings)
    assert any("PARTIAL" in w for w in result.warnings)
    # no fabricated values: missing evidence never becomes "None" text
    for text in _narrations(result.script_input):
        assert "None" not in text
    # still fully valid for the existing model and downstream contract
    ScriptInput.model_validate_json(result.script_input.model_dump_json())
    build_manifest(result.script_input)


def test_empty_candidates_fail_truthfully():
    with pytest.raises(BriefError) as excinfo:
        build_script_input(_artifact([]))
    assert excinfo.value.code == "insufficient_evidence"


def test_load_missing_artifact_fails_truthfully(tmp_path):
    with pytest.raises(BriefError) as excinfo:
        load_research_artifact(tmp_path / "missing.json")
    assert excinfo.value.code == "artifact_not_found"


# ---- Test 5: evidence preservation — every number traces to the artifact -------


def test_no_fabricated_numbers_in_narration():
    cands = [
        _candidate("aaa", title="The Only Tools Guide", channel="Jeff Su",
                   hook="Start with number 42 in mind.", hook_conf=0.7,
                   views=874065, velocity=3591.4, mult=3.5,
                   hypotheses=["opening-hook angle: show the 3 tools first."]),
        _candidate("bbb", views=9000, velocity=12.5),
    ]
    result = build_script_input(_artifact(cands))
    source_text = json.dumps(_artifact_dict(cands))
    for text in _narrations(result.script_input):
        normalized = text.replace(",", "")
        for token in re.findall(r"\d+(?:\.\d+)?", normalized):
            assert token in source_text, (
                f"fabricated number {token!r} in narration {text!r}"
            )
    # verbatim evidence really is used, not just absence of fabrication
    joined = "\n".join(_narrations(result.script_input))
    assert "The Only Tools Guide" in joined
    assert "Jeff Su" in joined
    assert "874,065 views" in joined
    assert "3591.4 views per day" in joined
    assert "show the 3 tools first." in joined


# ---- Test 6 / integration: generated brief enters the EXISTING stage -----------


def test_generated_brief_enters_existing_script_to_scene_stage(tmp_path):
    result = build_script_input(_artifact([
        _candidate("aaa", title="Hook Video", hook="Recorded hook.", hook_conf=0.7,
                   views=1000, velocity=5.0),
        _candidate("bbb", views=2000),
    ]))
    run = RunState(run_id=new_run_id(), job_id=new_job_id())
    registry = ArtifactRegistry(tmp_path / "run", run.run_id)
    stage = run_script_to_scene_stage(result.script_input, run, registry)
    assert stage.ok, stage.error
    assert len(stage.manifest.scenes) == 4
    # duration policy: brief sets none; existing estimate_scene_duration rules
    for scene in stage.manifest.scenes:
        assert scene.duration_seconds == estimate_scene_duration(scene.narration.text)
    assert stage.manifest.production_id == result.research_id


# ---- CLI: python -m ayce.research --brief (real subprocess, real transport) ----


def _run_brief_cli(payload_text, tmp_path):
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    return subprocess.run(
        [sys.executable, "-m", "ayce.research", "--brief", "--json"],
        input=payload_text, cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=60,
    )


def test_cli_brief_roundtrip(tmp_path):
    payload = _artifact_dict([_candidate("aaa", hook="Hook.", views=100)])
    proc = _run_brief_cli(json.dumps(payload), tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    script = ScriptInput.model_validate_json(proc.stdout)
    assert script.production_id == "res-20260920T000000Z-test0000001"


def test_cli_brief_insufficient_evidence_exit_code(tmp_path):
    proc = _run_brief_cli(json.dumps(_artifact_dict([])), tmp_path)
    assert proc.returncode == 9
    envelope = json.loads(proc.stdout)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "insufficient_evidence"


def test_cli_brief_malformed_stdin_exit_code(tmp_path):
    proc = _run_brief_cli("{not json", tmp_path)
    assert proc.returncode == 2
    envelope = json.loads(proc.stdout)
    assert envelope["error"]["code"] == "invalid_request"


def test_cli_brief_contract_violation_exit_code(tmp_path):
    payload = _artifact_dict([_candidate("aaa")])
    payload["bogus_unknown_field"] = 1
    proc = _run_brief_cli(json.dumps(payload), tmp_path)
    assert proc.returncode == 7
    envelope = json.loads(proc.stdout)
    assert envelope["error"]["code"] == "artifact_validation_failed"

