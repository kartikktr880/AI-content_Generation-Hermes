"""P1-B — deterministic Script → Scene Manifest stage adapter.

A bounded, Hermes-compatible production capability:

    structured script input (ScriptInput)
        → validate
        → deterministic transformation (build_manifest)
        → P1-A contract validation (ProductionManifest)
        → RunState lifecycle (running → succeeded/failed)
        → persist_manifest() + ArtifactRegistry
        → reload + verify
        → ScriptToSceneResult

Boundary rules:

- The stage is DETERMINISTIC: no LLMs, no randomness, no timestamps
  inside the manifest, no network. Identical input always produces
  identical manifest bytes.
- It is a *stage*, not an orchestrator. Future Hermes (master director)
  invokes it as one bounded capability via :func:`run_script_to_scene_stage`;
  nothing here plans, routes, retries policy, or coordinates workers.
- It reuses P0/P1-A abstractions exclusively: :class:`RunState`,
  :class:`ArtifactRegistry`, :func:`persist_manifest`, the structured
  logger. No second state machine, registry, or logging framework.
- It is NOT an ``Adapter`` subclass: the P0 Adapter convention is for
  provider-agnostic *external* capabilities; this stage is an internal,
  provider-free transformation.

Idempotency: rerunning the stage against the same run directory with
identical input reuses the existing scene_manifest artifact instead of
registering a duplicate (the P0 registry appends refs; the guard keeps
that honest). A *changed* manifest legitimately registers a new ref.
Rerunning a stage whose RunState already reached ``succeeded`` is
rejected by the state machine (succeeded is terminal) — orchestrators
create a fresh run instead. Retrying after ``failed`` is allowed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .artifacts import ArtifactKind, ArtifactRef, ArtifactRegistry
from .logging import StructuredLogger
from .scene_contract import (
    AssetRequirement,
    Narration,
    ProductionManifest,
    Scene,
    VisualIntent,
    dump_manifest,
    load_manifest,
    persist_manifest,
)
from .state import RunState

__all__ = [
    "STAGE_NAME",
    "ARTIFACT_STAGE",
    "WORDS_PER_SECOND",
    "MIN_SCENE_DURATION_SECONDS",
    "SceneInput",
    "ScriptInput",
    "ScriptToSceneResult",
    "ScriptToSceneError",
    "ScriptInputError",
    "estimate_scene_duration",
    "build_manifest",
    "load_script_input",
    "run_script_to_scene_stage",
]

#: RunState stage label for this capability.
STAGE_NAME = "script_to_scene"
#: ArtifactRegistry stage label identifying the producing stage.
ARTIFACT_STAGE = "scene_manifest"

# ---- deterministic MVP policies (documented, replaceable) ------------------

#: ~150 words per minute — deterministic narration duration approximation.
#: NOT production-grade timing; the real estimator replaces this later.
WORDS_PER_SECOND = 2.5
#: Short scenes still need at least this long (visual breathing room).
MIN_SCENE_DURATION_SECONDS = 2.0


class ScriptToSceneError(RuntimeError):
    """Raised when the stage cannot complete its work."""


class ScriptInputError(RuntimeError):
    """Raised when the structured script input is invalid."""


def estimate_scene_duration(narration_text: str) -> float:
    """Deterministic MVP duration rule (documented, replaceable).

    ``max(2.0 s, words / 2.5)`` — a pure function of the narration text
    (~150 wpm with a 2-second floor). This is only the temporal
    foundation for the first vertical slice, not real speech timing.
    """
    words = len(narration_text.split())
    return max(MIN_SCENE_DURATION_SECONDS, round(words / WORDS_PER_SECOND, 2))


def _default_scene_id(position: int) -> str:
    """Deterministic scene-ID strategy: ``scene-001``, ``scene-002``, ...

    Satisfies the P1-A identifier rules; no random component.
    """
    return f"scene-{position:03d}"


# ---- structured input contract ----------------------------------------------


class _InputModel(BaseModel):
    """Base for script-input DTOs: unknown fields are rejected.

    Input DTOs are STRUCTURAL only (required fields + types). All value
    semantics (positive durations, non-empty text, ID patterns) are
    owned by the P1-A Scene Contract: the stage passes raw values into
    the contract and lets it reject invalid manifests, so there is
    exactly one validation authority.
    """

    model_config = ConfigDict(extra="forbid")


class SceneInput(_InputModel):
    """One scene of the structured script input.

    A thin input DTO, not a second scene schema: every field maps
    directly onto the P1-A ``Scene`` contract. Identity, order, and
    duration are derived deterministically when omitted; narration and
    visual intent are required (structural). Value validation (e.g.
    positive duration, non-whitespace text) happens in the contract —
    invalid values flow through the stage and fail it via contract
    rejection.
    """

    scene_id: str | None = Field(default=None, description="Optional stable id; derived as scene-XXX when omitted.")
    sequence: StrictInt | None = Field(default=None, description="Optional position; derived from array order when omitted.")
    duration_seconds: float | None = Field(default=None, description="Optional duration; deterministic rule applies when omitted.")
    narration_text: str = Field(description="Narration text for this scene.")
    visual_description: str = Field(description="Creative visual intent for this scene.")
    asset_requirement: AssetRequirement | None = None


class ScriptInput(_InputModel):
    """The structured script input for one production."""

    production_id: str = Field(description="Stable production identity (typically a job id).")
    title: str = Field(description="Human-readable working title.")
    scenes: tuple[SceneInput, ...] = Field(min_length=1, description="Ordered script scenes (3-5 recommended for the MVP).")


def load_script_input(path: str | Path) -> ScriptInput:
    """Load and validate a structured script input from a JSON file."""
    return ScriptInput.model_validate_json(Path(path).read_text(encoding="utf-8"))

# ---- deterministic transformation -------------------------------------------


def build_manifest(script_input: ScriptInput) -> ProductionManifest:
    """Pure deterministic transformation: ScriptInput → ProductionManifest.

    No I/O, no randomness, no timestamps. Defaults applied when the
    input omits them:

    - scene_id  → ``scene-001``, ``scene-002``, ... by array position
    - sequence  → 1..N by array position (explicit values are preserved;
      mismatched values are rejected later by the P1-A contract)
    - duration  → :func:`estimate_scene_duration` from narration length

    Raises pydantic ``ValidationError`` if the constructed manifest
    violates the P1-A Scene Contract.
    """
    scenes: list[Scene] = []
    for position, scene_input in enumerate(script_input.scenes, start=1):
        scenes.append(
            Scene(
                scene_id=scene_input.scene_id or _default_scene_id(position),
                sequence=scene_input.sequence if scene_input.sequence is not None else position,
                duration_seconds=(
                    scene_input.duration_seconds
                    if scene_input.duration_seconds is not None
                    else estimate_scene_duration(scene_input.narration_text)
                ),
                narration=Narration(text=scene_input.narration_text),
                visual=VisualIntent(
                    description=scene_input.visual_description,
                    requirement=scene_input.asset_requirement,
                ),
            )
        )
    return ProductionManifest(
        production_id=script_input.production_id,
        title=script_input.title,
        scenes=tuple(scenes),
    )


# ---- stage execution (the Hermes-callable boundary) --------------------------


@dataclass(frozen=True)
class ScriptToSceneResult:
    """Outcome of one stage execution, for orchestrators (future Hermes).

    ``ok`` is trustworthy: it is ``True`` only after the manifest was
    constructed, validated by the contract, persisted, registered, and
    successfully reloaded and re-verified.
    """

    ok: bool
    stage: str
    run_id: str
    production_id: str | None
    manifest: ProductionManifest | None
    artifact: ArtifactRef | None
    error: str | None = None


def _find_existing_manifest(
    registry: ArtifactRegistry, run_dir: Path, manifest_json: str
) -> ArtifactRef | None:
    """Idempotency guard: return an already-registered, content-identical artifact."""
    for ref in registry.for_stage(ARTIFACT_STAGE):
        if ref.kind is not ArtifactKind.SCENE_MANIFEST:
            continue
        existing_file = Path(run_dir) / ref.path
        if existing_file.is_file() and existing_file.read_text(encoding="utf-8") == manifest_json:
            return ref
    return None


def run_script_to_scene_stage(
    script_input: ScriptInput | dict[str, Any] | str | Path,
    run_state: RunState,
    artifact_registry: ArtifactRegistry,
    *,
    logger: StructuredLogger | None = None,
) -> ScriptToSceneResult:
    """Execute the deterministic Script → Scene Manifest stage.

    This is the single callable a future orchestrator (Hermes) uses:

        result = run_script_to_scene_stage(
            script_input=...,
            run_state=run_state,
            artifact_registry=registry,
        )

    Lifecycle (existing P0 state machine):

        pending → running → succeeded          (happy path)
        pending → running → failed             (any stage-critical failure)
        failed  → running → ...                (retry is permitted)

    ``ok`` is never faked: input validation, contract validation,
    persistence, artifact registration, and reload verification must all
    succeed before ``succeeded`` is recorded; any exception marks the
    stage ``failed`` with the error recorded in the run state.
    """
    log = logger if logger is not None else StructuredLogger(level="INFO")
    log = log.bind(stage=STAGE_NAME, run_id=run_state.run_id, job_id=run_state.job_id)
    result = ScriptToSceneResult(
        ok=False,
        stage=STAGE_NAME,
        run_id=run_state.run_id,
        production_id=None,
        manifest=None,
        artifact=None,
        error=None,
    )

    def fail(exc: BaseException) -> ScriptToSceneResult:
        error = f"{type(exc).__name__}: {exc}"
        try:
            run_state.set_stage(STAGE_NAME, "failed", error=error)
        except Exception as mark_failure_error:
            # state machine corruption must not mask the original cause
            log.error("script_stage_failed", error=exc, state_error=str(mark_failure_error))
        else:
            log.error("script_stage_failed", error=exc)
        return replace(result, error=error)

    try:
        run_state.set_stage(STAGE_NAME, "running")
        log.info("script_stage_started")

        # 1) validate the structured script input
        if not isinstance(script_input, ScriptInput):
            script_input = ScriptInput.model_validate(script_input)
        result = replace(result, production_id=script_input.production_id)
        log = log.bind(production_id=script_input.production_id)

        # 2) deterministic transformation + P1-A contract validation
        log.info("scene_manifest_generation_started", scenes=len(script_input.scenes))
        manifest = build_manifest(script_input)
        log.info(
            "scene_manifest_generated",
            scenes=len(manifest.scenes),
            schema_version=manifest.schema_version,
        )

        # 3) persist through the existing P0/P1-A artifact mechanism (idempotent)
        manifest_json = dump_manifest(manifest)
        run_dir = artifact_registry.run_dir
        existing = _find_existing_manifest(artifact_registry, run_dir, manifest_json)
        if existing is not None:
            ref = existing
            log.info("scene_manifest_persisted", artifact_id=ref.artifact_id, reused=True)
        else:
            ref = persist_manifest(manifest, run_dir, artifact_registry, ARTIFACT_STAGE)
            log.info("scene_manifest_persisted", artifact_id=ref.artifact_id)

        # 4) reload + verify through the contract before claiming success
        reloaded = load_manifest(Path(run_dir) / ref.path)
        if reloaded != manifest:
            raise ScriptToSceneError("persisted scene manifest failed reload verification")

        # 5) success
        run_state.set_stage(STAGE_NAME, "succeeded")
        log.info("script_stage_succeeded", artifact_id=ref.artifact_id)
        return replace(result, ok=True, manifest=manifest, artifact=ref)
    except Exception as exc:  # noqa: BLE001 — the stage boundary must never fake success
        return fail(exc)



