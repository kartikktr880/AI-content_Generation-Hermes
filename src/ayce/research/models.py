"""Stage 2 — Research Artifact contract (four epistemic tiers).

The single validated contract produced by the local research worker and
returned (compactly) through the Director MCP tool
``execute_research_slice``.

Design rules:

- Pydantic V2, ``extra="forbid"`` (same strictness as the Scene Contract).
- Exactly FOUR epistemic tiers, never mixed:
  OBSERVED_FACT > DERIVED_METRIC > MODEL_INFERENCE > CREATIVE_HYPOTHESIS.
  ``tiers`` is stamped in canonical order and validated.
- Provenance is preserved at artifact AND candidate level (how evidence
  was obtained, caption availability, warnings). Nothing is fabricated:
  missing values are ``None`` plus a truthful reason string.
- :meth:`ResearchArtifact.compact` produces the bounded Hermes envelope
  (few exemplar outliers, truncated hooks/facts) — the full artifact is
  persisted locally and NEVER shipped to Hermes wholesale.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "SCHEMA_VERSION",
    "RESEARCH_ID_RE",
    "VIDEO_ID_RE",
    "EpistemicTier",
    "ResearchStatus",
    "CaptionsStatus",
    "CandidateProvenance",
    "ResearchCandidate",
    "ResearchProvenance",
    "ResearchArtifact",
]

SCHEMA_VERSION = "1.0"

RESEARCH_ID_RE = re.compile(r"^res-[A-Za-z0-9._-]{1,64}$")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

#: Canonical tier order — always all four, always this order.
CANONICAL_TIERS = ("OBSERVED_FACT", "DERIVED_METRIC", "MODEL_INFERENCE", "CREATIVE_HYPOTHESIS")


class EpistemicTier(str, Enum):
    """The four epistemic tiers of research evidence."""

    OBSERVED_FACT = "OBSERVED_FACT"
    DERIVED_METRIC = "DERIVED_METRIC"
    MODEL_INFERENCE = "MODEL_INFERENCE"
    CREATIVE_HYPOTHESIS = "CREATIVE_HYPOTHESIS"


class ResearchStatus(str, Enum):
    """Degraded-mode-aware outcome of one research slice."""

    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    RATE_LIMITED = "RATE_LIMITED"
    CAPTIONS_UNAVAILABLE = "CAPTIONS_UNAVAILABLE"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    ANALYSIS_FAILED = "ANALYSIS_FAILED"
    ARTIFACT_VALIDATION_FAILED = "ARTIFACT_VALIDATION_FAILED"


class CaptionsStatus(str, Enum):
    """Per-video caption availability (deterministic, never guessed)."""

    AVAILABLE = "available"
    AUTO_GENERATED = "auto_generated"
    CAPTIONS_UNAVAILABLE = "captions_unavailable"
    EXTRACTION_FAILED = "extraction_failed"
    NOT_ATTEMPTED = "not_attempted"


class CandidateProvenance(BaseModel):
    """How this candidate's evidence was obtained."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ingested_via: str
    captions_status: CaptionsStatus
    captions_lang: str | None = None
    captions_source: str | None = None  # "manual" | "auto"
    warnings: list[str] = Field(default_factory=list)


class ResearchCandidate(BaseModel):
    """One normalized video candidate with tiered evidence."""

    model_config = ConfigDict(extra="forbid")

    video_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")
    url: str
    title: str
    channel: str | None = None
    published_at: str | None = None  # ISO-8601 date, when known
    view_count: int | None = None

    hook_summary: str | None = None
    hook_confidence: float = Field(ge=0, le=1)
    outlier_multiplier: float | None = None
    outlier_reason: str | None = None
    velocity_proxy: float | None = None
    velocity_note: str = ""
    cluster_id: int | None = None
    description_excerpt: str | None = None

    observed_facts: list[str] = Field(default_factory=list)
    derived_metrics: list[str] = Field(default_factory=list)
    model_inferences: list[str] = Field(default_factory=list)
    creative_hypotheses: list[str] = Field(default_factory=list)

    confidence: float = Field(ge=0, le=1)
    provenance: CandidateProvenance


class ResearchProvenance(BaseModel):
    """Artifact-level provenance: how the whole slice was produced."""

    model_config = ConfigDict(extra="forbid")

    worker_version: str
    collected_at: str
    ytdlp_path: str | None = None
    ytdlp_version: str | None = None
    request_digest: str
    ingestion: dict[str, int] = Field(default_factory=dict)
    clustering: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class ResearchArtifact(BaseModel):
    """The validated Research Artifact (schema version 1.0)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    research_id: str
    objective: str
    niche: str | None = None
    query: str | None = None
    collected_at: str
    status: ResearchStatus
    tiers: list[EpistemicTier]
    candidates: list[ResearchCandidate] = Field(default_factory=list, max_length=50)
    provenance: ResearchProvenance

    @field_validator("research_id")
    @classmethod
    def _check_research_id(cls, v: str) -> str:
        if not RESEARCH_ID_RE.match(v):
            raise ValueError(f"research_id must match ^res-[A-Za-z0-9._-]{{1,64}}$: {v!r}")
        return v

    @field_validator("tiers")
    @classmethod
    def _check_tiers(cls, v: list[EpistemicTier]) -> list[EpistemicTier]:
        names = tuple(t.value for t in v)
        if names != CANONICAL_TIERS:
            raise ValueError(
                "tiers must be exactly the four epistemic tiers in canonical "
                f"order {CANONICAL_TIERS}; got {names}"
            )
        return v

    def compact(
        self,
        *,
        max_outliers: int = 5,
        title_chars: int = 120,
        hook_chars: int = 220,
        fact_chars: int = 140,
        facts_per_tier: int = 3,
    ) -> dict[str, Any]:
        """Bounded Hermes envelope: few exemplar outliers, truncated text.

        The full artifact NEVER crosses the MCP boundary — only this
        compact projection does. Ranking: outlier_multiplier descending
        (unknown multipliers last, then video_id for determinism).
        """
        def _rank(c: ResearchCandidate) -> tuple:
            mult = c.outlier_multiplier
            return (mult is None, -(mult if mult is not None else 0.0), c.video_id)

        ranked = sorted(self.candidates, key=_rank)[: max(0, max_outliers)]

        def _clip(text: str, limit: int) -> str:
            return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

        def _tier(items: list[str], limit: int) -> list[str]:
            return [_clip(i, fact_chars) for i in items[:limit]]

        outliers = [
            {
                "video_id": c.video_id,
                "url": c.url,
                "title": _clip(c.title, title_chars),
                "channel": c.channel,
                "published_at": c.published_at,
                "view_count": c.view_count,
                "outlier_multiplier": c.outlier_multiplier,
                "velocity_proxy": c.velocity_proxy,
                "hook_summary": _clip(c.hook_summary, hook_chars) if c.hook_summary else None,
                "cluster_id": c.cluster_id,
                "confidence": c.confidence,
                "observed_facts": _tier(c.observed_facts, facts_per_tier),
                "derived_metrics": _tier(c.derived_metrics, facts_per_tier),
                "model_inferences": _tier(c.model_inferences, facts_per_tier),
                "creative_hypotheses": _tier(c.creative_hypotheses, facts_per_tier),
                "captions_status": c.provenance.captions_status.value,
            }
            for c in ranked
        ]
        return {
            "schema_version": self.schema_version,
            "research_id": self.research_id,
            "objective": _clip(self.objective, 300),
            "niche": self.niche,
            "query": self.query,
            "collected_at": self.collected_at,
            "status": self.status.value,
            "tiers": [t.value for t in self.tiers],
            "candidate_count": len(self.candidates),
            "outliers": outliers,
            "provenance": {
                "worker_version": self.provenance.worker_version,
                "ytdlp_version": self.provenance.ytdlp_version,
                "clustering": self.provenance.clustering,
                "warnings": [_clip(w, 200) for w in self.provenance.warnings[:5]],
            },
        }