"""P6 — Golden Path pipeline runner (thin orchestration of verified stages).

Composes the already-verified P1-B → P5.5 stages into ONE production
executable path. This module contains NO stage logic of its own — every
step delegates to the existing, individually verified stage callables
(the same functions a future Hermes orchestrator would call):

    Script JSON
        ↓ run_script_to_scene_stage      → scene_manifest.json
        ↓ run_asset_resolution_stage     → asset_manifest.json
        ↓ run_narration_audio_stage      → narration_manifest.json
        ↓ run_timeline_stage             → timeline.json
        ↓ run_captions_stage             → captions.json + captions.srt (CAPTIONS)
        ↓ run_production_render_stage    → production MP4 (rendered_video)
        ↓ run_media_qa_stage             → qa_report.json (QA_REPORT)

Boundary rules (deliberately small):

- ONE shared ``RunState`` and ONE shared ``ArtifactRegistry`` per run —
  no second state system, no second registry, no database, no queue.
- Run identity: EVERY pipeline invocation creates a NEW run id and a NEW
  run directory (``<data_dir>/runs/<run_id>``). A previous run is never
  overwritten or deduplicated globally. Within a run, the existing
  per-stage artifact idempotency remains authoritative.
- Failure semantics: the first stage whose execution fails stops the
  pipeline. Completed stages stay ``succeeded`` and their artifacts stay
  persisted; later stages never execute and publish nothing. No rollback
  fakery, no deletion of valid prior artifacts.
- QA FAIL is NOT a pipeline failure: a deterministic technical QA FAIL
  is truthful evidence produced by a SUCCEEDED stage (``ok=True,
  verdict="FAIL"``). Only a QA EXECUTION ERROR (the QA stage cannot do
  its job) fails the pipeline. These two cases are never collapsed.
- Asset resolution and narration are provider-agnostic: the deterministic
  fixture providers remain the DEFAULT (``FileBackedAssetProvider`` /
  ``FileBackedNarrationProvider``), and the real production providers
  (Pexels for visuals, Kokoro-82M for narration) are selected ONLY when
  explicitly configured via ``AYCE_*`` — see ``select_asset_provider`` and
  ``select_narration_provider``. This pipeline is orchestration; provider
  choice is configuration, never a silent fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .asset_resolution import run_asset_resolution_stage
from .artifacts import ArtifactRegistry
from .captions import run_captions_stage
from .config import Config
from .ids import new_job_id, new_run_id
from .lineage import discover as discover_lineage
from .lineage import register_lineage
from .compositor import build_renderer
from .logging import StructuredLogger
from .media_qa import run_media_qa_stage
from .narration_audio import run_narration_audio_stage
from .render import Renderer, run_production_render_stage
from .script_to_scene import load_script_input, run_script_to_scene_stage
from .state import RunState
from .timeline import run_timeline_stage
from .tts_piper import select_narration_provider
from .visual_pexels import select_asset_provider

__all__ = [
    "PIPELINE_STAGES",
    "STATE_FILENAME",
    "StageOutcome",
    "PipelineResult",
    "run_pipeline",
]

#: The exact, ordered Golden Path stage sequence executed by the runner.
PIPELINE_STAGES: tuple[str, ...] = (
    "script_to_scene",
    "asset_resolution",
    "narration_audio",
    "timeline",
    "captions",
    "production_render",
    "media_qa",
)

#: Per-run state checkpoint file (existing RunState.save convention).
STATE_FILENAME = "state.json"


@dataclass(frozen=True)
class StageOutcome:
    """One executed stage's truthful outcome (executed stages only)."""

    stage: str
    ok: bool
    error: str | None = None


@dataclass(frozen=True)
class PipelineResult:
    """Outcome of one Golden Path pipeline execution.

    ``ok`` means: every executed stage succeeded AND the pipeline reached
    (and executed) the QA stage. A technical QA FAIL verdict does NOT
    make ``ok`` false — the verdict is reported truthfully in
    ``qa_verdict``. ``failed_stage`` names the first stage whose
    execution failed (None on success).
    """

    ok: bool
    run_id: str
    job_id: str
    run_dir: Path
    stages: tuple[StageOutcome, ...] = ()
    production_id: str | None = None
    qa_verdict: str | None = None
    rendered_artifact_id: str | None = None
    qa_artifact_id: str | None = None
    failed_stage: str | None = None
    error: str | None = None
    #: Stage 3 (additive): durable lineage references when the run is
    #: research-derived; None for fixture/manual runs.
    lineage: dict | None = None



def run_pipeline(
    script_input: Any,
    *,
    config: Config,
    assets_dir: str | Path | None = None,
    narration_dir: str | Path | None = None,
    renderer: Renderer | None = None,
    run_dir: str | Path | None = None,
    logger: StructuredLogger | None = None,
) -> PipelineResult:
    """Execute the complete Golden Path: script → scenes → assets →
    narration → timeline → production render → technical media QA.

    This is the single callable a future orchestrator (Hermes) wraps:

        result = run_pipeline(
            "script.json",
            config=config,                       # required
            assets_dir=..., narration_dir=...,   # optional fixture dirs
            renderer=...,                        # optional (default FFmpegRenderer)
        )

    Arguments:
        script_input: path to a script JSON file (ScriptInput contract),
            a ScriptInput instance, or a validated dict. ``str``/``Path``
            inputs are treated as filesystem paths and loaded through the
            existing ``load_script_input`` convention.
        config: process configuration (AYCE_* convention); the run
            directory defaults to ``<resolved_data_dir>/runs/<run_id>``.
        assets_dir / narration_dir: optional fixture directories passed
            through to the existing file-backed providers (defaults to
            the providers' own DEFAULT_FIXTURE_DIR conventions).
        renderer: optional injected Renderer (defaults to the verified
            ``FFmpegRenderer``). Injection exists so orchestrators/tests
            can vary the renderer without touching stage implementations.
        run_dir: optional explicit run directory override (tests);
            defaults to ``<data_dir>/runs/<run_id>``.
        logger: optional StructuredLogger (defaults to a stderr logger
            at the configured level).

    Run isolation: every invocation creates a new run identity and a new
    run directory; an existing run is never overwritten. Within the run,
    each stage's own idempotency behavior remains authoritative.
    """
    log = logger if logger is not None else StructuredLogger(level=config.log_level)
    run_id = new_run_id()
    job_id = new_job_id()
    resolved_run_dir = (
        Path(run_dir) if run_dir is not None
        else config.resolved_data_dir / "runs" / run_id
    )
    log = log.bind(component="pipeline", run_id=run_id, job_id=job_id)

    result = PipelineResult(ok=False, run_id=run_id, job_id=job_id, run_dir=resolved_run_dir)

    # one shared state + one shared registry for the whole run
    run = RunState(run_id=run_id, job_id=job_id)
    registry = ArtifactRegistry(resolved_run_dir, run_id)
    state_path = resolved_run_dir / STATE_FILENAME
    executed: list[StageOutcome] = []

    def persist() -> None:
        run.save(state_path)

    def record(stage: str, ok: bool, error: str | None) -> None:
        executed.append(StageOutcome(stage, ok, error))
        persist()

    def stopped(stage: str, error: str) -> PipelineResult:
        log.error(
            "pipeline_failed",
            failed_stage=stage,
            error=error,
            completed_stages=[o.stage for o in executed if o.ok],
        )
        return replace(result, stages=tuple(executed), failed_stage=stage, error=error)

    log.info("pipeline_started", stages=list(PIPELINE_STAGES), run_dir=str(resolved_run_dir))

    # ---- lineage capture (Stage 3; additive, NEVER fails production) --------
    # The script input bytes are read BEFORE the P1-B stage loads the
    # contract. Only research-derived scripts (production_id = res-…, the
    # documented Stage 1/2 convention) get lineage; fixture/manual runs are
    # untouched (their artifact kind-sets stay byte-identical).
    script_bytes: bytes | None = None
    if isinstance(script_input, (str, Path)):
        script_path = Path(script_input)
        try:
            script_bytes = script_path.read_bytes()
        except OSError:
            script_bytes = None
        # filesystem path → load through the existing P1-B loader
        script_input = load_script_input(script_path)
    production_lineage = discover_lineage(
        script_input, config=config, script_bytes=script_bytes
    )
    if production_lineage is not None:
        run.lineage = production_lineage.to_dict()
        persist()
        register_lineage(run, registry, production_lineage, script_bytes)
        result = replace(result, lineage=production_lineage.to_dict())
        log.info("lineage_captured", **production_lineage.to_dict())

    # ---- stage 1: script → scene manifest (P1-B) ----------------------------
    scene_result = run_script_to_scene_stage(script_input, run, registry, logger=log)
    record("script_to_scene", scene_result.ok, scene_result.error)
    if not scene_result.ok:
        return stopped("script_to_scene", scene_result.error or "unknown error")
    result = replace(result, production_id=scene_result.production_id)
    scene_manifest = scene_result.manifest

    # ---- stage 2: asset resolution (P2; provider selected by configuration) -
    # Default: deterministic file-backed fixture provider (unchanged verified
    # behavior). The real Pexels provider is selected ONLY when
    # AYCE_PEXELS_API_KEY is configured (see select_asset_provider).
    asset_provider = select_asset_provider(config, assets_dir)
    asset_result = run_asset_resolution_stage(
        scene_manifest, run, registry, asset_provider, logger=log
    )
    record("asset_resolution", asset_result.ok, asset_result.error)
    if not asset_result.ok:
        return stopped("asset_resolution", asset_result.error or "unknown error")
    asset_manifest = asset_result.manifest

    # ---- stage 3: narration audio (P3; provider is OPT-IN via config) -------
    # Default: deterministic file-backed fixture provider (unchanged verified
    # behavior). Piper is selected ONLY when AYCE_PIPER_MODEL is configured.
    narration_provider = select_narration_provider(config, narration_dir=narration_dir)
    narration_result = run_narration_audio_stage(
        scene_manifest, run, registry, narration_provider, logger=log
    )
    record("narration_audio", narration_result.ok, narration_result.error)
    if not narration_result.ok:
        return stopped("narration_audio", narration_result.error or "unknown error")
    narration_manifest = narration_result.manifest

    # ---- stage 4: renderer-neutral timeline (P4) ----------------------------
    timeline_result = run_timeline_stage(
        scene_manifest=scene_manifest,
        asset_manifest=asset_manifest,
        narration_manifest=narration_manifest,
        run_state=run,
        artifact_registry=registry,
        logger=log,
    )
    record("timeline", timeline_result.ok, timeline_result.error)
    if not timeline_result.ok:
        return stopped("timeline", timeline_result.error or "unknown error")
    timeline_manifest = timeline_result.manifest

    # ---- stage 5: scene-level captions (P6.5) -------------------------------
    # Captions derive from verified contracts only: narration text comes
    # VERBATIM from the Scene Contract, cue intervals from the timeline's
    # narration elements. Fail-fast; no timing is invented.
    captions_result = run_captions_stage(
        scene_manifest,
        timeline_manifest,
        narration_manifest,
        run,
        registry,
        logger=log,
    )
    record("captions", captions_result.ok, captions_result.error)
    if not captions_result.ok:
        return stopped("captions", captions_result.error or "unknown error")

    # ---- stage 6: multi-scene production render (P5 / Stage 8) --------------
    # Renderer selection is configuration-driven (`AYCE_RENDERER`); the
    # verified `ffmpeg-smoke` renderer remains the default, so previously
    # verified behaviour is unchanged unless the compositor is explicitly
    # selected. An explicitly injected renderer always wins (tests/orchestrators).
    selected_renderer = renderer if renderer is not None else build_renderer(config)
    render_result = run_production_render_stage(
        timeline_manifest, run, registry, selected_renderer, logger=log
    )
    record("production_render", render_result.ok, render_result.error)
    if not render_result.ok:
        return stopped("production_render", render_result.error or "unknown error")
    result = replace(result, rendered_artifact_id=render_result.artifact.artifact_id)

    # ---- stage 7: technical media QA (P5.5) ---------------------------------
    # QA verdict FAIL is truthful evidence from a SUCCEEDED stage — the
    # pipeline completes. Only a QA EXECUTION ERROR (ok=False) fails it.
    qa_result = run_media_qa_stage(
        render_result.artifact,
        timeline_manifest,
        narration_manifest,
        run,
        registry,
        config,
        logger=log,
    )
    record("media_qa", qa_result.ok, qa_result.error)
    if not qa_result.ok:
        return stopped("media_qa", qa_result.error or "unknown error")
    result = replace(
        result,
        qa_verdict=qa_result.verdict,
        qa_artifact_id=qa_result.artifact.artifact_id if qa_result.artifact else None,
    )

    log.info(
        "pipeline_succeeded",
        stages=[o.stage for o in executed],
        qa_verdict=qa_result.verdict,
        production_id=result.production_id,
    )
    return replace(result, ok=True, stages=tuple(executed))