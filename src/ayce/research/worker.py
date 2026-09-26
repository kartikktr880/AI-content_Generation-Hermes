"""Stage 2 — Local research worker (the vertical slice orchestrator).

Pipeline (all local, outside Hermes's context):

    research objective
      → YouTube ingestion (yt-dlp subprocess, bounded)
      → normalization (VideoRecord)
      → hook extraction (bounded 45s window)
      → outlier / velocity analytics (deterministic)
      → local topic clustering (explicitly degrading)
      → validated ResearchArtifact (four epistemic tiers)
      → compact envelope (bounded) + full artifact persisted locally

The worker never exposes raw yt-dlp output, full transcripts, or
unbounded candidate lists to its caller. Errors are classified
(:class:`ResearchError` codes) — nothing is silently fabricated.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..config import Config
from . import analytics, clustering, ingestion, transcripts
from .models import (
    CANONICAL_TIERS,
    CaptionsStatus,
    CandidateProvenance,
    EpistemicTier,
    ResearchArtifact,
    ResearchCandidate,
    ResearchProvenance,
    ResearchStatus,
)

__all__ = [
    "WORKER_VERSION",
    "ResearchRequest",
    "ResearchError",
    "new_research_id",
    "run_research",
]

WORKER_VERSION = "1.0"

#: How much of the full artifact JSON is written to disk (atomic, local).
RESEARCH_DIRNAME = "research"


class ResearchRequest(BaseModel):
    """Validated MCP tool input."""

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=2000)
    niche: str | None = Field(default=None, max_length=300)
    query: str | None = Field(default=None, max_length=300)
    target_channels: list[str] = Field(default_factory=list, max_length=5)
    max_outliers: int = Field(default=5, ge=1, le=10)
    max_videos: int = Field(default=6, ge=1, le=12)

    @field_validator("objective", "niche", "query", mode="before")
    @classmethod
    def _strip_str(cls, v):
        if isinstance(v, str):
            return v.strip() or None
        return v

    @field_validator("target_channels", mode="before")
    @classmethod
    def _strip_list(cls, v):
        if isinstance(v, list):
            return [item.strip() for item in v if isinstance(item, str) and item.strip()]
        return v


class ResearchError(RuntimeError):
    """Classified research failure (never an unclassified crash)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def request_digest(payload: dict) -> str:
    """Deterministic digest of the canonical request payload."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def new_research_id(digest: str, *, stamp: str | None = None) -> str:
    """``res-<utcstamp>-<digest12>`` — the ids.py convention, with the
    random token replaced by the payload digest (content-derived)."""
    return f"res-{stamp or _utc_stamp()}-{digest[:12]}"


def _canonical_request(payload: dict) -> dict:
    """Normalized request dict used for the digest (defaults applied)."""
    request = ResearchRequest.model_validate(payload)
    return {
        "objective": request.objective,
        "niche": request.niche,
        "query": request.query,
        "target_channels": request.target_channels,
        "max_outliers": request.max_outliers,
        "max_videos": request.max_videos,
    }


def _build_candidate(video, hook, cluster_id: int | None, cluster_method: str,
                     median: float | None, now: datetime) -> ResearchCandidate:
    mult, mult_reason = analytics.outlier_multiplier(video.view_count, median)
    velocity, velocity_note = analytics.velocity_proxy(
        video.view_count, video.published_at, now=now
    )

    observed = [
        f"view_count: {video.view_count}" if video.view_count is not None
        else "view_count: unavailable",
        f"published_at: {video.published_at}" if video.published_at
        else "published_at: unavailable",
        f"channel: {video.channel}" if video.channel else "channel: unavailable",
    ]
    derived = [
        f"outlier_multiplier: {mult} ({mult_reason}; channel median baseline"
        + (f" = {median:g}" if median is not None else " unavailable") + ")",
        f"velocity_proxy_views_per_day: {velocity}" if velocity is not None
        else "velocity_proxy: unavailable",
    ]
    inferred = (
        [f"topic cluster {cluster_id} via {cluster_method}"]
        if cluster_id is not None else []
    )
    hook_ok = hook is not None and hook.status in (
        CaptionsStatus.AVAILABLE, CaptionsStatus.AUTO_GENERATED
    )
    hypotheses = (
        [f"opening-hook angle: {hook.hook_summary[:140]}"] if hook_ok and hook.hook_summary else []
    )

    confidence = 0.3
    if video.view_count is not None:
        confidence += 0.25
    if video.published_at:
        confidence += 0.2
    if hook_ok:
        confidence += 0.15
    if mult is not None:
        confidence += 0.1

    provenance = CandidateProvenance(
        ingested_via="yt-dlp",
        captions_status=hook.status if hook is not None else CaptionsStatus.NOT_ATTEMPTED,
        captions_lang=hook.lang if hook else None,
        captions_source=hook.captions_source if hook else None,
        warnings=(hook.warnings if hook else [])[:5],
    )
    return ResearchCandidate(
        video_id=video.video_id,
        url=video.url,
        title=video.title or f"(untitled {video.video_id})",
        channel=video.channel,
        published_at=video.published_at,
        view_count=video.view_count,
        hook_summary=hook.hook_summary if hook is not None else None,
        hook_confidence=hook.confidence if hook is not None else 0.0,
        outlier_multiplier=mult,
        outlier_reason=mult_reason,
        velocity_proxy=velocity,
        velocity_note=velocity_note,
        cluster_id=cluster_id,
        description_excerpt=video.description_excerpt,
        observed_facts=observed,
        derived_metrics=derived,
        model_inferences=inferred,
        creative_hypotheses=hypotheses,
        confidence=round(min(confidence, 1.0), 2),
        provenance=provenance,
    )


def run_research(payload: dict, config: Config) -> dict:
    """Execute one bounded research slice; return the compact envelope.

    Raises :class:`ResearchError` with a classified code on controlled
    failure; never leaks raw subprocess output or full transcripts.
    """
    try:
        request = ResearchRequest.model_validate(payload)
    except ValidationError as exc:
        raise ResearchError("invalid_request", f"invalid research request: {exc}") from exc

    canonical = _canonical_request(payload)
    digest = request_digest(canonical)
    research_id = new_research_id(digest)
    collected_at = _utcnow_iso()
    now = datetime.now(timezone.utc)

    ytdlp_path = ingestion.resolve_ytdlp(config)
    if ytdlp_path is None:
        raise ResearchError(
            "source_unavailable",
            "yt-dlp executable not found (set AYCE_YTDLP_PATH or install yt-dlp)",
        )
    ytdlp_ver = ingestion.ytdlp_version(ytdlp_path)

    warnings: list[str] = []
    outcome = ingestion.ingest(
        config,
        target_channels=request.target_channels,
        query=request.query,
        max_videos=request.max_videos,
    )
    warnings.extend(outcome.warnings)

    if not outcome.videos:
        if outcome.rate_limited:
            raise ResearchError(
                "rate_limited", "YouTube ingestion was rate-limited; no videos collected")
        raise ResearchError(
            "source_unavailable",
            "no videos could be collected for the research objective"
            + (f"; warnings: {'; '.join(outcome.warnings[-3:])}" if outcome.warnings else ""),
        )

    # Hook extraction: bounded, prioritized by view count (desc), then id.
    hook_budget = min(len(outcome.videos), max(3, request.max_outliers))
    hook_targets = sorted(
        outcome.videos,
        key=lambda v: (-(v.view_count if v.view_count is not None else -1), v.video_id),
    )[:hook_budget]
    hooks: dict[str, transcripts.HookResult] = {}
    for video in hook_targets:
        hooks[video.video_id] = transcripts.fetch_hook(
            ytdlp_path,
            video.url,
            video.video_id,
            has_manual_subtitles=video.has_manual_subtitles,
        )
        warnings.extend(f"[{video.video_id}] {w}" for w in hooks[video.video_id].warnings)

    # Analytics: per-channel median baseline over collected candidates.
    by_channel: dict[str, list[int | None]] = {}
    for video in outcome.videos:
        by_channel.setdefault(video.channel or "(unknown)", []).append(video.view_count)
    medians = {
        key: analytics.channel_median_views(views) for key, views in by_channel.items()
    }

    # Clustering (never fatal).
    titles = [v.title or v.video_id for v in outcome.videos]
    clustering_result = clustering.cluster_titles(titles)
    if clustering_result.capability_status not in ("ok", "fallback_lexical"):
        warnings.append(
            f"clustering degraded: {clustering_result.capability_status} ({clustering_result.detail})"
        )
    labels = clustering_result.labels

    candidates = [
        _build_candidate(
            video,
            hooks.get(video.video_id),
            labels[idx] if idx < len(labels) else None,
            clustering_result.method,
            medians.get(video.channel or "(unknown)"),
            now,
        )
        for idx, video in enumerate(outcome.videos)
    ]

    # Overall status: deterministic precedence.
    attempted = [hooks[v.video_id] for v in hook_targets]
    all_hooks_unavailable = bool(attempted) and all(
        h.status == CaptionsStatus.CAPTIONS_UNAVAILABLE for h in attempted
    )
    if outcome.rate_limited:
        status = ResearchStatus.RATE_LIMITED
    elif all_hooks_unavailable:
        status = ResearchStatus.CAPTIONS_UNAVAILABLE
    elif warnings or outcome.status != "SUCCESS":
        status = ResearchStatus.PARTIAL
    else:
        status = ResearchStatus.SUCCESS

    artifact = ResearchArtifact(
        research_id=research_id,
        objective=request.objective,
        niche=request.niche,
        query=request.query,
        collected_at=collected_at,
        status=status,
        tiers=[EpistemicTier(t) for t in CANONICAL_TIERS],
        candidates=candidates,
        provenance=ResearchProvenance(
            worker_version=WORKER_VERSION,
            collected_at=collected_at,
            ytdlp_path=ytdlp_path,
            ytdlp_version=ytdlp_ver,
            request_digest=digest,
            ingestion=outcome.counts,
            clustering={
                "method": clustering_result.method,
                "capability_status": clustering_result.capability_status,
                "n_clusters": clustering_result.n_clusters,
                "detail": clustering_result.detail,
            },
            warnings=warnings,
        ),
    )

    # Persist the FULL artifact locally (atomic write convention); the
    # envelope for Hermes is the compact projection only.
    artifact_path = _persist_artifact(config, research_id, artifact)
    return {
        "ok": True,
        "research_id": research_id,
        "status": status.value,
        "candidate_count": len(candidates),
        "artifact_path": str(artifact_path),
        "artifact": artifact.compact(max_outliers=request.max_outliers),
    }


def _persist_artifact(config: Config, research_id: str, artifact: ResearchArtifact) -> Path:
    directory = config.resolved_data_dir / RESEARCH_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{research_id}.json"
    tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
    tmp.write_text(artifact.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path