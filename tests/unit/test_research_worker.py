"""Stage 2 worker tests — status mapping, controlled errors, envelope
bounds (network and yt-dlp fully faked at the module seams)."""

import json

import pytest

from ayce.config import Config
from ayce.research import worker
from ayce.research.ingestion import IngestionOutcome, VideoRecord
from ayce.research.models import CaptionsStatus
from ayce.research.transcripts import HookResult
from ayce.research.worker import ResearchError, new_research_id, run_research


def _video(video_id, views, title="A title", channel="Chan"):
    return VideoRecord(
        video_id=video_id, url=f"https://www.youtube.com/watch?v={video_id}",
        title=f"{title} {video_id}", channel=channel, published_at="2026-08-01",
        view_count=views,
    )


def _patch_worker(monkeypatch, outcome: IngestionOutcome, hooks: dict | None = None):
    monkeypatch.setattr(worker.ingestion, "resolve_ytdlp", lambda config: "yt-dlp.exe")
    monkeypatch.setattr(worker.ingestion, "ytdlp_version", lambda path: "2026.01.01")
    monkeypatch.setattr(worker.ingestion, "ingest", lambda config, **kw: outcome)

    def fake_fetch_hook(ytdlp, url, video_id, has_manual_subtitles=None):
        return (hooks or {}).get(
            video_id,
            HookResult(hook_summary="A hook line.", confidence=0.7,
                       status=CaptionsStatus.AUTO_GENERATED, lang="en",
                       captions_source="auto"),
        )

    monkeypatch.setattr(worker.transcripts, "fetch_hook", fake_fetch_hook)


def _request(**overrides):
    payload = {"objective": "find outlier formats", "query": "test", "max_outliers": 3}
    payload.update(overrides)
    return payload


def test_success_artifact_and_envelope(monkeypatch, tmp_path):
    outcome = IngestionOutcome(
        videos=[_video("vid0000000000000001", 90000), _video("vid0000000000000002", 10000),
                _video("vid0000000000000003", 8000)],
        status="SUCCESS", counts={"collected": 3},
    )
    _patch_worker(monkeypatch, outcome)
    envelope = run_research(_request(), Config(data_dir=tmp_path))
    assert envelope["ok"] is True
    assert envelope["status"] == "SUCCESS"
    assert envelope["candidate_count"] == 3
    assert len(envelope["artifact"]["outliers"]) == 3
    assert envelope["artifact"]["tiers"] == [
        "OBSERVED_FACT", "DERIVED_METRIC", "MODEL_INFERENCE", "CREATIVE_HYPOTHESIS"]
    assert envelope["artifact"]["provenance"]["ytdlp_version"] == "2026.01.01"
    persisted = json.loads(
        (tmp_path / "research" / f"{envelope['research_id']}.json").read_text("utf-8"))
    assert persisted["research_id"] == envelope["research_id"]
    assert len(persisted["candidates"]) == 3
    # envelope stays well under the 2,500-token Hermes target
    assert len(json.dumps(envelope)) < 15000


def test_captions_unavailable_status_when_all_hooks_lack_captions(monkeypatch, tmp_path):
    outcome = IngestionOutcome(
        videos=[_video("vid0000000000000001", 90000), _video("vid0000000000000002", 10000),
                _video("vid0000000000000003", 8000)],
        status="SUCCESS", counts={"collected": 3},
    )
    no_caps = HookResult(hook_summary="Captions unavailable", confidence=0.2,
                         status=CaptionsStatus.CAPTIONS_UNAVAILABLE)
    _patch_worker(monkeypatch, outcome, hooks={v.video_id: no_caps for v in outcome.videos})
    envelope = run_research(_request(), Config(data_dir=tmp_path))
    assert envelope["status"] == "CAPTIONS_UNAVAILABLE"
    outlier = envelope["artifact"]["outliers"][0]
    assert outlier["hook_summary"] == "Captions unavailable"
    assert outlier["captions_status"] == "captions_unavailable"


def test_partial_status_on_ingestion_warnings(monkeypatch, tmp_path):
    outcome = IngestionOutcome(
        videos=[_video("vid0000000000000001", 90000), _video("vid0000000000000002", 10000),
                _video("vid0000000000000003", 8000)],
        status="PARTIAL", warnings=["metadata fetch failed for one video"],
        counts={"collected": 3},
    )
    _patch_worker(monkeypatch, outcome)
    envelope = run_research(_request(), Config(data_dir=tmp_path))
    assert envelope["status"] == "PARTIAL"


def test_source_unavailable_is_controlled(monkeypatch, tmp_path):
    outcome = IngestionOutcome(status="SOURCE_UNAVAILABLE", counts={})
    _patch_worker(monkeypatch, outcome)
    with pytest.raises(ResearchError) as excinfo:
        run_research(_request(), Config(data_dir=tmp_path))
    assert excinfo.value.code == "source_unavailable"


def test_rate_limited_is_controlled(monkeypatch, tmp_path):
    outcome = IngestionOutcome(status="RATE_LIMITED", rate_limited=True, counts={})
    _patch_worker(monkeypatch, outcome)
    with pytest.raises(ResearchError) as excinfo:
        run_research(_request(), Config(data_dir=tmp_path))
    assert excinfo.value.code == "rate_limited"


def test_invalid_request_rejected(monkeypatch, tmp_path):
    with pytest.raises(ResearchError) as excinfo:
        run_research({"objective": ""}, Config(data_dir=tmp_path))
    assert excinfo.value.code == "invalid_request"
    with pytest.raises(ResearchError):
        run_research({"objective": "x", "max_outliers": 99}, Config(data_dir=tmp_path))


def test_research_id_derivation_deterministic():
    digest = "ab" * 32
    assert new_research_id(digest, stamp="20260920T000000Z") == "res-20260920T000000Z-abababababab"
    assert new_research_id(digest) == new_research_id(digest)


def test_channel_baseline_drives_multiplier(monkeypatch, tmp_path):
    outcome = IngestionOutcome(
        videos=[_video("vid0000000000000001", 75000), _video("vid0000000000000002", 10000),
                _video("vid0000000000000003", 8000)],
        status="SUCCESS", counts={"collected": 3},
    )
    _patch_worker(monkeypatch, outcome)
    envelope = run_research(_request(), Config(data_dir=tmp_path))
    top = envelope["artifact"]["outliers"][0]
    # median(75000, 10000, 8000) = 10000 → 75000/10000 = 7.5
    assert top["outlier_multiplier"] == 7.5
    persisted = json.loads(
        (tmp_path / "research" / f"{envelope['research_id']}.json").read_text("utf-8"))
    candidate = next(c for c in persisted["candidates"]
                     if c["video_id"] == top["video_id"])
    assert "temporal proxy" in candidate["velocity_note"]