"""Stage 2 Research Artifact contract tests — four tiers, provenance,
strict validation, bounded compact output."""

import json

import pytest
from pydantic import ValidationError

from ayce.research.models import (
    CANONICAL_TIERS,
    CaptionsStatus,
    CandidateProvenance,
    EpistemicTier,
    ResearchArtifact,
    ResearchCandidate,
    ResearchProvenance,
    ResearchStatus,
)


def _provenance():
    return ResearchProvenance(
        worker_version="1.0",
        collected_at="2026-09-20T00:00:00.000Z",
        ytdlp_path="yt-dlp",
        ytdlp_version="2026.09.14",
        request_digest="ab" * 32,
        ingestion={"collected": 3},
        clustering={"method": "lexical_cosine_dbscan", "capability_status": "fallback_lexical"},
        warnings=["w1"],
    )


def _candidate(video_id="vid0000000000000001", multiplier=7.5):
    return ResearchCandidate(
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        title=f"Great title {video_id}",
        channel="Some Channel",
        published_at="2026-08-01",
        view_count=75000,
        hook_summary="Stop doing this one thing…",
        hook_confidence=0.7,
        outlier_multiplier=multiplier,
        outlier_reason="ok",
        velocity_proxy=1285.7,
        velocity_note="temporal proxy",
        cluster_id=0,
        observed_facts=["view_count: 75000", "published_at: 2026-08-01"],
        derived_metrics=["outlier_multiplier: 7.5"],
        model_inferences=["topic cluster 0"],
        creative_hypotheses=["opening-hook angle: Stop doing this one thing"],
        confidence=0.95,
        provenance=CandidateProvenance(
            ingested_via="yt-dlp",
            captions_status=CaptionsStatus.AUTO_GENERATED,
            captions_lang="en",
            captions_source="auto",
        ),
    )


def _artifact(candidates=None, status=ResearchStatus.SUCCESS):
    return ResearchArtifact(
        research_id="res-20260920T000000Z-abcdef123456",
        objective="find outlier formats",
        niche="ai tools",
        query="ai tools",
        collected_at="2026-09-20T00:00:00.000Z",
        status=status,
        tiers=[EpistemicTier(t) for t in CANONICAL_TIERS],
        candidates=candidates if candidates is not None else [_candidate()],
        provenance=_provenance(),
    )


def test_valid_four_tier_artifact_validates():
    artifact = _artifact()
    assert [t.value for t in artifact.tiers] == list(CANONICAL_TIERS)
    assert artifact.candidates[0].provenance.captions_status is CaptionsStatus.AUTO_GENERATED


def test_invalid_schema_rejected():
    with pytest.raises(ValidationError):
        _artifact(status="TOTALLY_MADE_UP")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        _artifact(candidates=[_candidate()] + [{"bad": "shape"}])
    # extra fields are forbidden (strict contract)
    data = _artifact().model_dump()
    data["unexpected"] = True
    with pytest.raises(ValidationError):
        ResearchArtifact.model_validate(data)


def test_tier_order_and_completeness_enforced():
    data = _artifact().model_dump()
    data["tiers"] = ["OBSERVED_FACT", "MODEL_INFERENCE"]
    with pytest.raises(ValidationError):
        ResearchArtifact.model_validate(data)


def test_research_id_grammar_enforced():
    data = _artifact().model_dump()
    data["research_id"] = "not-a-research-id"
    with pytest.raises(ValidationError):
        ResearchArtifact.model_validate(data)


def test_provenance_retained_through_roundtrip():
    artifact = _artifact()
    dumped = json.loads(artifact.model_dump_json())
    reloaded = ResearchArtifact.model_validate(dumped)
    assert reloaded == artifact
    assert reloaded.provenance.request_digest == "ab" * 32
    assert reloaded.candidates[0].provenance.captions_lang == "en"


def test_compact_output_is_bounded():
    many = [_candidate(video_id=f"vid{i:019d}", multiplier=float(i)) for i in range(20)]
    artifact = _artifact(candidates=many)
    envelope = artifact.compact(max_outliers=5)
    assert envelope["candidate_count"] == 20
    assert len(envelope["outliers"]) == 5
    # ranked by multiplier desc: the highest multipliers first
    multipliers = [o["outlier_multiplier"] for o in envelope["outliers"]]
    assert multipliers == sorted(multipliers, reverse=True)
    serialized = json.dumps(envelope)
    # compact envelope must stay far below the 2,500-token Hermes target
    assert len(serialized) < 15000


def test_compact_truncates_long_text():
    long_candidate = _candidate().model_copy(update={"title": "x" * 5000, "hook_summary": "y" * 5000})
    envelope = _artifact(candidates=[long_candidate]).compact()
    assert len(envelope["outliers"][0]["title"]) <= 120
    assert len(envelope["outliers"][0]["hook_summary"]) <= 220