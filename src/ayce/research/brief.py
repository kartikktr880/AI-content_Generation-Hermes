"""Stage 2.5 — Research → Script Brief bridge (deterministic, additive).

The smallest custom boundary that turns the persisted research evidence
base into the HEAD of the existing Golden Path::

    ResearchArtifact (data/research/res-*.json)
        ↓  build_script_input()  — pure, deterministic, no LLM/network
    ScriptInput (the EXISTING P1-B contract)
        ↓  run_script_to_scene_stage (UNCHANGED existing stage)
    scene manifest → … existing 7-stage Golden Path …

This module is a formatter, not a writer. It calls no model, no API,
no Hermes; same artifact in → byte-identical ScriptInput JSON out.

Deterministic candidate selection (adapted to the real ResearchArtifact
model, which frequently has ``outlier_multiplier: None`` for every
candidate — "insufficient_baseline")::

    1. candidates with a KNOWN outlier_multiplier first, higher first
    2. candidates with a non-empty hook_summary next
    3. higher hook_confidence
    4. higher velocity_proxy        (missing → treated as 0.0)
    5. higher view_count            (missing → treated as 0)
    6. video_id ascending           (total-order tie-break)

Scene structure (fixed 4-scene evidence brief; no invented narrative)::

    scene-001 HOOK        the top-ranked candidate's recorded opening
                          hook, quoted verbatim (or its title when no
                          hook was captured)
    scene-002 SETUP       the artifact's own metadata: objective, niche,
                          query, status, research_id — traceability
    scene-003 DEVELOPMENT mechanical restatement of the top candidates'
                          observed fields (channel / view_count /
                          velocity_proxy / outlier_multiplier), each
                          value named as the field it comes from
    scene-004 PAYOFF      one evidence item as a takeaway: the first
                          creative_hypothesis, else the outlier_reason,
                          else the clustering inference, else a plain
                          artifact-reference sentence

Evidence policy (zero fabrication):

- Narration contains only (a) verbatim evidence strings from the
  artifact (clipped, never paraphrased into new claims) or (b)
  mechanical restatements that name the field they restate.
- Missing values are never invented: absent fields are simply omitted
  from the sentence; sparse artifacts degrade with recorded warnings,
  never with fabricated statistics, sources, or URLs.
- Durations are NOT set here: the existing P1-B policy
  (:func:`ayce.script_to_scene.estimate_scene_duration`, ~150 wpm with
  a 2 s floor) remains the single timing authority.

Errors are truthful: an artifact without usable candidates raises
:class:`BriefError` (``insufficient_evidence``) — no fake script is
produced. Malformed artifacts fail pydantic validation explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..script_to_scene import ScriptInput, SceneInput
from .models import ResearchArtifact, ResearchCandidate

__all__ = [
    "BRIEF_VERSION",
    "BriefError",
    "BriefResult",
    "rank_candidates",
    "build_script_input",
    "load_research_artifact",
]

#: Version stamped into :class:`BriefResult` for traceability.
BRIEF_VERSION = "1.0"

#: A brief needs at least one candidate with usable evidence.
MIN_CANDIDATES = 1

#: Verbatim-evidence clip limits (mirrors models.compact conventions).
_HOOK_CLIP = 220
_TEXT_CLIP = 120
_SENTENCE_CLIP = 240


class BriefError(ValueError):
    """Classified brief failure (never silently turned into fake output)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class BriefResult:
    """Outcome of one deterministic ResearchArtifact → ScriptInput bridge."""

    script_input: ScriptInput
    research_id: str
    research_status: str
    ranked_video_ids: tuple[str, ...]
    warnings: tuple[str, ...]
    brief_version: str = BRIEF_VERSION


def load_research_artifact(path: str | Path) -> ResearchArtifact:
    """Load and validate a persisted ResearchArtifact JSON file.

    Missing file → :class:`BriefError` (``artifact_not_found``); malformed
    or contract-violating JSON → pydantic ``ValidationError`` (explicit).
    """
    path = Path(path)
    if not path.is_file():
        raise BriefError("artifact_not_found", f"research artifact not found: {path}")
    return ResearchArtifact.model_validate_json(path.read_text(encoding="utf-8"))


def rank_candidates(candidates: list[ResearchCandidate]) -> list[ResearchCandidate]:
    """Deterministic evidence-strength ordering (documented in the module
    docstring). Returns a new list; the input is never mutated.

    The artifact's own compact() ranks only by outlier_multiplier; this
    ranking adds the signals that remain meaningful when every multiplier
    is ``None`` (the common real-world ``insufficient_baseline`` case):
    hook availability/confidence, then velocity proxy, then absolute
    view scale, then the ``video_id`` for a total order.
    """
    def _key(c: ResearchCandidate) -> tuple:
        has_hook = bool(c.hook_summary and c.hook_summary.strip())
        return (
            c.outlier_multiplier is None,                                   # known multipliers first
            -(c.outlier_multiplier if c.outlier_multiplier is not None else 0.0),
            not has_hook,                                                   # usable hooks next
            -c.hook_confidence,
            -(c.velocity_proxy if c.velocity_proxy is not None else 0.0),
            -(c.view_count if c.view_count is not None else 0),
            c.video_id,                                                     # stable total tie-break
        )

    models = [
        c if isinstance(c, ResearchCandidate) else ResearchCandidate.model_validate(c)
        for c in candidates
    ]
    return sorted(models, key=_key)


def _clip(text: str, limit: int) -> str:
    """Deterministic clip; prefers a sentence boundary inside the tail.

    Never adds content — only removes it. Falls back to a hard cut like
    ``ResearchArtifact.compact`` when no sentence boundary exists.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    window = cut[limit // 2:]
    best = max(window.rfind(p) for p in ".!?")
    if best != -1:
        return cut[: limit // 2 + best + 1]
    return cut.rstrip()


def _restated_views(view_count: int) -> str:
    """Mechanical restatement of a view_count field (no rounding lies)."""
    return f"{view_count:,} views"


def _candidate_sentence(c: ResearchCandidate) -> str:
    """One evidence sentence about a candidate, naming every field used.

    Only non-missing fields appear; nothing is invented for gaps.
    """
    parts = [f'"{c.title}"']
    if c.channel:
        parts.append(f"channel field reports {c.channel}")
    if c.view_count is not None:
        parts.append(_restated_views(c.view_count))
    if c.velocity_proxy is not None:
        parts.append(f"velocity proxy of {c.velocity_proxy:g} views per day")
    if c.outlier_multiplier is not None:
        parts.append(f"an outlier multiplier of {c.outlier_multiplier:g}")
    return "The top-ranked candidate, " + ", ".join(parts) + "."


def build_script_input(artifact: ResearchArtifact) -> BriefResult:
    """Deterministically convert a ResearchArtifact into a ScriptInput.

    Pure function: no I/O, no clock, no randomness, no network. The
    returned ScriptInput is validated by the EXISTING P1-B model and
    carries no duration_seconds (the existing downstream WPM policy
    stays the single timing authority).

    Raises:
        BriefError: ``insufficient_evidence`` when the artifact carries
            no candidates to build an evidence brief from.
    """
    if len(artifact.candidates) < MIN_CANDIDATES:
        raise BriefError(
            "insufficient_evidence",
            f"research artifact {artifact.research_id} has no candidates; "
            "an evidence-backed script brief cannot be generated without "
            "fabricating content",
        )

    ranked = rank_candidates(list(artifact.candidates))
    c1 = ranked[0]
    c2 = ranked[1] if len(ranked) > 1 else None
    warnings: list[str] = []

    if c1.outlier_multiplier is None:
        warnings.append(
            "outlier_multiplier unavailable (insufficient baseline); ranked "
            "by hook confidence, velocity proxy and view count instead"
        )
    if not (c1.hook_summary and c1.hook_summary.strip()):
        warnings.append(
            f"top candidate {c1.video_id} has no captured opening hook; "
            "HOOK scene uses the candidate title verbatim"
        )
    if artifact.status.value != "SUCCESS":
        warnings.append(
            f"research slice status is {artifact.status.value}; the brief "
            "inherits that degraded evidence state truthfully"
        )

    # ---- scene 1: HOOK ------------------------------------------------------
    hook = (c1.hook_summary or "").strip()
    if hook:
        s1_narration = (
            "Here is an opening hook recorded by the research worker, "
            f'quoted verbatim: "{_clip(hook, _HOOK_CLIP)}"'
        )
    else:
        s1_narration = f'The research opens with its top-ranked candidate: "{c1.title}".'
    s1_visual = f"High-contrast opening title card referencing: {_clip(c1.title, 90)}."

    # ---- scene 2: SETUP (artifact metadata; full traceability) --------------
    setup_bits = [
        "This video is built directly from research evidence, with no invented facts.",
        f"The research objective: {_clip(artifact.objective, _TEXT_CLIP)}.",
    ]
    if artifact.niche:
        setup_bits.append(f"Niche: {_clip(artifact.niche, 60)}.")
    if artifact.query:
        setup_bits.append(f"Query: {_clip(artifact.query, 60)}.")
    setup_bits.append(
        f"Evidence base: research artifact {artifact.research_id}, "
        f"status {artifact.status.value}."
    )
    s2_narration = " ".join(setup_bits)
    s2_visual = f"Clean text card naming the research objective for {artifact.research_id}."

    # ---- scene 3: DEVELOPMENT (mechanical restatement of observed fields) ---
    dev_bits = [_candidate_sentence(c1)]
    if c2 is not None:
        second = [f'A second ranked candidate is "{c2.title}"']
        if c2.view_count is not None:
            second.append(f"with {_restated_views(c2.view_count)}")
        dev_bits.append(", ".join(second) + ".")
    s3_narration = " ".join(dev_bits)
    s3_visual = "Side-by-side comparison graphic of the top-ranked research candidates."

    texts = {
        "s1_narration": s1_narration, "s1_visual": s1_visual,
        "s2_narration": s2_narration, "s2_visual": s2_visual,
        "s3_narration": s3_narration, "s3_visual": s3_visual,
    }
    return _finish_brief(artifact, ranked, c1, warnings, texts)


def _finish_brief(
    artifact: ResearchArtifact,
    ranked: list[ResearchCandidate],
    c1: ResearchCandidate,
    warnings: list[str],
    texts: dict,
) -> BriefResult:
    """PAYOFF scene + ScriptInput assembly (split only to keep functions small)."""
    # ---- scene 4: PAYOFF (one evidence item as the takeaway) ----------------
    if c1.creative_hypotheses:
        s4_narration = (
            "The research suggests one opening-hook angle worth testing, "
            'quoted from its hypothesis tier: '
            f'"{_clip(c1.creative_hypotheses[0], _SENTENCE_CLIP)}"'
        )
        s4_visual = "Closing card visualising the suggested hook angle."
    elif c1.outlier_reason:
        s4_narration = (
            "The research notes about the top candidate, verbatim: "
            f'"{_clip(c1.outlier_reason, _SENTENCE_CLIP)}"'
        )
        s4_visual = "Closing card summarising the outlier note."
    elif c1.model_inferences:
        s4_narration = (
            "Clustering evidence from the research worker: "
            f'"{_clip(c1.model_inferences[0], _SENTENCE_CLIP)}"'
        )
        s4_visual = "Closing card showing the topic-cluster grouping."
    else:
        s4_narration = (
            f"Every claim in this brief traces to research artifact "
            f"{artifact.research_id}."
        )
        s4_visual = f"Plain closing card citing research artifact {artifact.research_id}."

    scenes = (
        SceneInput(
            narration_text=texts["s1_narration"],
            visual_description=texts["s1_visual"],
            asset_requirement={
                "kind": "image",
                "description": f"Opening visual representing: {_clip(c1.title, 90)}",
                "requires_provenance": True,
            },
        ),
        SceneInput(
            narration_text=texts["s2_narration"],
            visual_description=texts["s2_visual"],
        ),
        SceneInput(
            narration_text=texts["s3_narration"],
            visual_description=texts["s3_visual"],
            asset_requirement={
                "kind": "image",
                "description": "Chart-style graphic comparing the top research candidates",
                "requires_provenance": True,
            },
        ),
        SceneInput(
            narration_text=s4_narration,
            visual_description=s4_visual,
        ),
    )

    # production_id = research_id: traceable, and it already satisfies the
    # P1-A identifier grammar (res-… matches ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$).
    script_input = ScriptInput(
        production_id=artifact.research_id,
        title=_clip(f"Research brief: {artifact.objective}", _TEXT_CLIP),
        scenes=scenes,
    )

    return BriefResult(
        script_input=script_input,
        research_id=artifact.research_id,
        research_status=artifact.status.value,
        ranked_video_ids=tuple(c.video_id for c in ranked),
        warnings=tuple(warnings),
    )
