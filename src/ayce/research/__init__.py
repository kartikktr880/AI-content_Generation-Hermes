"""Stage 2 — Research & Content Intelligence (first vertical slice).

Local, deterministic research worker: YouTube ingestion via the existing
quota-free yt-dlp CLI path, transcript/opening-hook extraction, outlier
+ velocity analytics, local topic clustering, and a validated
four-tier Research Artifact.

Boundary rules:
- yt-dlp is an EXTERNAL CLI tool (Piper/FFmpeg convention): subprocess
  with explicit argv, never imported, never an AYCE dependency.
- Hermes (via the Director MCP) receives ONLY the compact envelope —
  never raw yt-dlp output, full transcripts, or unbounded candidates.
- The full artifact is persisted locally under ``<data_dir>/research/``.
"""

from .brief import (
    BRIEF_VERSION,
    BriefError,
    BriefResult,
    build_script_input,
    load_research_artifact,
    rank_candidates,
)
from .models import (
    CANONICAL_TIERS,
    CaptionsStatus,
    EpistemicTier,
    ResearchArtifact,
    ResearchStatus,
    SCHEMA_VERSION,
)
from .worker import ResearchError, ResearchRequest, new_research_id, request_digest, run_research

__all__ = [
    "BRIEF_VERSION",
    "BriefError",
    "BriefResult",
    "CANONICAL_TIERS",
    "CaptionsStatus",
    "EpistemicTier",
    "ResearchArtifact",
    "ResearchError",
    "ResearchRequest",
    "ResearchStatus",
    "SCHEMA_VERSION",
    "build_script_input",
    "load_research_artifact",
    "new_research_id",
    "rank_candidates",
    "request_digest",
    "run_research",
]