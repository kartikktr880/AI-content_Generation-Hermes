"""P4 — deterministic renderer-neutral timeline / composition stage.

A bounded, Hermes-compatible production capability:

    persisted Scene Manifest (P1-A)
        +
    persisted Asset Manifest (P2)
        +
    persisted Narration Manifest (P3)
        ↓ cross-validation (production identity, scene linkage)
        ↓ deterministic temporal assembly
    TimelineManifest (separate artifact — never merged upstream)
        → RunState lifecycle → ArtifactRegistry → reload + verify
        → TimelineResult

The Timeline Manifest expresses WHAT should appear and WHEN:

- scene intervals form a deterministic temporal spine (scene[n].start =
  sum of preceding scene durations, from the Scene Contract durations);
- each scene with a resolved visual gets a ``visual`` element occupying
  the full scene interval;
- each scene's narration starts at the scene start and lasts exactly the
  resolved audio's truthful duration.

Temporal policy (explicit, documented):

- Scene Contract duration = creative scene duration (the spine).
- Narration audio duration = actual media duration (truthful from P3).
- Narration longer than its scene → VALIDATION FAILURE (no safe
  overflow/stretch/trim semantics at MVP — sync problems must not be
  silently baked into the timeline).
- Narration shorter than its scene → allowed; the remaining scene time
  is unambiguously visual-only (narration.start = scene.start).
- Narration with unknown duration → VALIDATION FAILURE (a renderer
  cannot place audio of unknown length).

Renderer neutrality:

- No FFmpeg/Remotion/OTIO/OpenMontage commands, filters, nodes, DOM
  instructions, or provider-specific metadata. Only typed temporal
  composition referencing run-relative resolved-artifact paths.
- The timeline references RESOLVED artifacts; it never invokes providers.

MVP scope: ``visual`` + ``narration`` elements only. Captions, music,
SFX, graphics, text, transitions and overlays have no upstream contract
yet and are deliberately NOT faked — the schema is designed so future
element types are additive under schema minor versions.

Scenes without an ``AssetRequirement`` yield no visual element (P2
resolved none); their scene interval still exists on the temporal spine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from .artifacts import ArtifactKind, ArtifactRef, ArtifactRegistry
from .asset_resolution import (
    ASSET_MANIFEST_FILENAME,
    AssetManifest,
)
from .logging import StructuredLogger
from .narration_audio import (
    NARRATION_MANIFEST_FILENAME,
    NarrationManifest,
)
from .scene_contract import (
    MANIFEST_FILENAME as SCENE_MANIFEST_FILENAME,
    ProductionManifest,
)
from .state import RunState

__all__ = [
    "STAGE_NAME",
    "ARTIFACT_STAGE",
    "TIMELINE_FILENAME",
    "TIMELINE_SCHEMA_VERSION",
    "TimelineError",
    "TimelineElementKind",
    "TimelineElement",
    "TimelineScene",
    "TimelineManifest",
    "TimelineResult",
    "dump_timeline_manifest",
    "load_timeline_manifest",
    "persist_timeline_manifest",
    "run_timeline_stage",
]

#: RunState stage label for this capability.
STAGE_NAME = "timeline"
#: ArtifactRegistry stage label identifying the producing stage.
ARTIFACT_STAGE = "timeline"
#: Persisted timeline filename inside the run directory.
TIMELINE_FILENAME = "timeline.json"

#: Version stamped onto newly created timelines.
TIMELINE_SCHEMA_VERSION = "1.0"
#: The only timeline schema major version this build understands.
TIMELINE_SUPPORTED_MAJOR = 1

#: Tolerance for floating-point timing comparisons (seconds).
_TIMING_EPSILON = 1e-6

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class TimelineError(RuntimeError):
    """Raised when the timeline cannot be composed safely."""


# ---- renderer-neutral timeline models ---------------------------------------


class _TimelineModel(BaseModel):
    """Base for timeline models: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


def _check_id(value: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValueError(f"invalid identifier {value!r}: no whitespace, 1-64 chars [A-Za-z0-9._-]")
    return value


def _check_positive_duration(value: float) -> float:
    if value <= 0:
        raise ValueError(f"duration must be positive, got {value}")
    return float(value)


def _check_start(value: float) -> float:
    if value < 0:
        raise ValueError(f"start must be >= 0, got {value}")
    return float(value)


def _validate_rel_path(value: str) -> str:
    """Validate a run-relative posix path (no absolute, no traversal)."""
    p = Path(value)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise ValueError(f"unsafe run-relative path {value!r}")
    return p.as_posix()


class TimelineElementKind(str, Enum):
    """Element types justified by current upstream artifacts (MVP).

    Future additive types (caption, music, sfx, graphic, text, transition,
    overlay) require upstream contracts first and are added under schema
    minor versions — never faked.
    """

    VISUAL = "visual"
    NARRATION = "narration"


class TimelineElement(_TimelineModel):
    """One renderer-neutral timed element of a scene."""

    element_id: str = Field(description="Deterministic id: '{scene_id}-{kind}'.")
    kind: TimelineElementKind
    start_seconds: float = Field(ge=0, description="Start offset from the timeline origin, in seconds.")
    duration_seconds: float = Field(gt=0, description="How long the element stays active, in seconds.")
    #: Run-relative path of the resolved artifact this element plays/shows.
    source: str

    _v_element_id = field_validator("element_id")(_check_id)
    _v_start = field_validator("start_seconds")(_check_start)
    _v_duration = field_validator("duration_seconds")(_check_positive_duration)
    _v_source = field_validator("source")(_validate_rel_path)

class TimelineScene(_TimelineModel):
    """One scene's interval on the temporal spine, with its timed elements."""

    scene_id: str
    sequence: StrictInt = Field(ge=1, description="Canonical Scene Contract ordering.")
    start_seconds: float = Field(ge=0, description="Scene start on the timeline, in seconds.")
    duration_seconds: float = Field(gt=0, description="Creative scene duration from the contract, in seconds.")
    elements: tuple[TimelineElement, ...] = Field(min_length=1, description="Timed elements of this scene.")

    _v_scene_id = field_validator("scene_id")(_check_id)

    @model_validator(mode="after")
    def _check_elements(self) -> "TimelineScene":
        scene_end = self.start_seconds + self.duration_seconds
        for element in self.elements:
            end = element.start_seconds + element.duration_seconds
            if end > scene_end + _TIMING_EPSILON:
                raise ValueError(
                    f"element {element.element_id!r} ends at {end} but its scene "
                    f"{self.scene_id!r} ends at {scene_end}"
                )
        ids = [e.element_id for e in self.elements]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate element ids in scene {self.scene_id!r}")
        return self


class TimelineManifest(_TimelineModel):
    """The persisted renderer-neutral composition for one production.

    Temporal composition only: WHAT appears and WHEN. Contains no
    renderer commands, provider metadata, or absolute paths.
    """

    schema_version: str = TIMELINE_SCHEMA_VERSION
    production_id: str
    #: Run-relative references to the upstream artifacts this was built from.
    scene_manifest: str
    asset_manifest: str
    narration_manifest: str
    total_duration_seconds: float = Field(gt=0, description="Sum of all scene durations.")
    scenes: tuple["TimelineScene", ...] = Field(min_length=1)

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9]+\.[0-9]+", value):
            raise ValueError(
                f"schema_version {value!r} must be 'MAJOR.MINOR' (e.g. {TIMELINE_SCHEMA_VERSION!r})"
            )
        major = int(value.split(".", 1)[0])
        if major != TIMELINE_SUPPORTED_MAJOR:
            raise ValueError(
                f"unsupported schema_version {value!r}; this build understands "
                f"major version {TIMELINE_SUPPORTED_MAJOR} only"
            )
        return value

    @field_validator("scene_manifest")
    @classmethod
    def _check_scene_manifest_ref(cls, value: str) -> str:
        if value != SCENE_MANIFEST_FILENAME:
            raise ValueError(
                f"scene_manifest must reference the run's scene manifest artifact "
                f"({SCENE_MANIFEST_FILENAME!r}), got {value!r}"
            )
        return value

    @field_validator("asset_manifest")
    @classmethod
    def _check_asset_manifest_ref(cls, value: str) -> str:
        if value != ASSET_MANIFEST_FILENAME:
            raise ValueError(
                f"asset_manifest must reference the run's asset manifest artifact "
                f"({ASSET_MANIFEST_FILENAME!r}), got {value!r}"
            )
        return value

    @field_validator("narration_manifest")
    @classmethod
    def _check_narration_manifest_ref(cls, value: str) -> str:
        if value != NARRATION_MANIFEST_FILENAME:
            raise ValueError(
                f"narration_manifest must reference the run's narration manifest "
                f"artifact ({NARRATION_MANIFEST_FILENAME!r}), got {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _check_scene_intervals(self) -> "TimelineManifest":
        sequences = [scene.sequence for scene in self.scenes]
        if sequences != list(range(1, len(self.scenes) + 1)):
            raise ValueError(
                f"timeline scenes must follow the canonical 1..{len(self.scenes)} "
                f"ordering, got {sequences}"
            )
        expected_start = 0.0
        for scene in self.scenes:
            if abs(scene.start_seconds - expected_start) > _TIMING_EPSILON:
                raise ValueError(
                    f"scene {scene.scene_id!r} starts at {scene.start_seconds}; "
                    f"expected {expected_start} (temporal spine must be contiguous)"
                )
            scene_end = scene.start_seconds + scene.duration_seconds
            for element in scene.elements:
                if element.start_seconds + _TIMING_EPSILON < scene.start_seconds:
                    raise ValueError(
                        f"element {element.element_id!r} starts before its scene"
                    )
                if element.start_seconds + element.duration_seconds > scene_end + _TIMING_EPSILON:
                    raise ValueError(
                        f"element {element.element_id!r} extends beyond its scene interval"
                    )
            expected_start += scene.duration_seconds
        if abs(self.total_duration_seconds - expected_start) > _TIMING_EPSILON:
            raise ValueError(
                f"total_duration_seconds {self.total_duration_seconds} != sum of "
                f"scene durations {expected_start}"
            )
        return self

# ---- deterministic assembly ---------------------------------------------------


def build_timeline(
    scene_manifest: ProductionManifest,
    asset_manifest: AssetManifest,
    narration_manifest: NarrationManifest,
) -> TimelineManifest:
    """Pure deterministic temporal assembly of the three upstream manifests.

    Cross-validation (fail-fast):
    - production identity must agree across all three manifests;
    - upstream records must reference known scenes;
    - every scene's narration must have a resolved record with a KNOWN
      duration (a renderer cannot place unknown-length audio);
    - narration longer than its scene is a validation failure (no safe
      overflow semantics at MVP);
    - a scene that declares an AssetRequirement must have its resolved
      asset record.

    Raises :class:`TimelineError` on any inconsistency — never invents
    placeholders.
    """
    # 1) production identity agreement
    for label, manifest in (
        ("asset manifest", asset_manifest),
        ("narration manifest", narration_manifest),
    ):
        if manifest.production_id != scene_manifest.production_id:
            raise TimelineError(
                f"production_id mismatch: scene manifest declares "
                f"{scene_manifest.production_id!r} but the {label} declares "
                f"{manifest.production_id!r}"
            )

    # 2) scene-id-consistent upstream lookups
    scene_ids = {scene.scene_id for scene in scene_manifest.scenes}
    assets_by_scene = {record.scene_id: record for record in asset_manifest.resolved_assets}
    narration_by_scene = {
        record.scene_id: record for record in narration_manifest.narration_audio
    }
    for lookup, label in ((assets_by_scene, "asset"), (narration_by_scene, "narration")):
        unknown = sorted(set(lookup) - scene_ids)
        if unknown:
            raise TimelineError(f"{label} manifest references unknown scenes: {unknown}")

    # 3) deterministic temporal spine + elements
    timeline_scenes: list[TimelineScene] = []
    elapsed = 0.0
    for scene in scene_manifest.scenes:
        narration_record = narration_by_scene.get(scene.scene_id)
        if narration_record is None:
            raise TimelineError(f"missing resolved narration audio for scene {scene.scene_id!r}")
        if narration_record.duration_seconds is None:
            raise TimelineError(
                f"narration audio for scene {scene.scene_id!r} has unknown duration — "
                f"cannot compose safely (metadata is never invented)"
            )
        if narration_record.duration_seconds > scene.duration_seconds + _TIMING_EPSILON:
            raise TimelineError(
                f"narration audio for scene {scene.scene_id!r} exceeds the scene duration "
                f"({narration_record.duration_seconds}s > {scene.duration_seconds}s); "
                f"no safe overflow/stretch/trim semantics exist at MVP — the timeline "
                f"is not published"
            )

        elements: list[TimelineElement] = []
        if scene.visual.requirement is not None:
            asset_record = assets_by_scene.get(scene.scene_id)
            if asset_record is None:
                raise TimelineError(
                    f"scene {scene.scene_id!r} declares an asset requirement but the "
                    f"asset manifest has no resolved asset for it"
                )
            elements.append(
                TimelineElement(
                    element_id=f"{scene.scene_id}-visual",
                    kind=TimelineElementKind.VISUAL,
                    start_seconds=round(elapsed, 6),
                    duration_seconds=scene.duration_seconds,
                    source=asset_record.path,
                )
            )
        elements.append(
            TimelineElement(
                element_id=f"{scene.scene_id}-narration",
                kind=TimelineElementKind.NARRATION,
                start_seconds=round(elapsed, 6),
                duration_seconds=narration_record.duration_seconds,
                source=narration_record.path,
            )
        )
        timeline_scenes.append(
            TimelineScene(
                scene_id=scene.scene_id,
                sequence=scene.sequence,
                start_seconds=round(elapsed, 6),
                duration_seconds=scene.duration_seconds,
                elements=tuple(elements),
            )
        )
        elapsed += scene.duration_seconds

    return TimelineManifest(
        production_id=scene_manifest.production_id,
        scene_manifest=SCENE_MANIFEST_FILENAME,
        asset_manifest=ASSET_MANIFEST_FILENAME,
        narration_manifest=NARRATION_MANIFEST_FILENAME,
        total_duration_seconds=round(elapsed, 6),
        scenes=tuple(timeline_scenes),
    )

# ---- serialization / artifact persistence -------------------------------------


def dump_timeline_manifest(manifest: TimelineManifest) -> str:
    """Deterministic JSON serialization (fixed field order, no timestamps)."""
    return manifest.model_dump_json(indent=2) + "\n"


def load_timeline_manifest(path: str | Path) -> TimelineManifest:
    """Load a timeline file: read → validate → deserialize (pydantic)."""
    return TimelineManifest.model_validate_json(Path(path).read_text(encoding="utf-8"))


def persist_timeline_manifest(
    manifest: TimelineManifest,
    run_dir: str | Path,
    registry: ArtifactRegistry,
) -> ArtifactRef:
    """Persist the timeline and register it (kind ``timeline``).

    Reuses the existing P0 artifact system; no new persistence layer.
    """
    target = Path(run_dir) / TIMELINE_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    return registry.register(
        ARTIFACT_STAGE,
        ArtifactKind.TIMELINE,
        TIMELINE_FILENAME,
        {
            "schema_version": manifest.schema_version,
            "scenes": len(manifest.scenes),
            "total_duration_seconds": manifest.total_duration_seconds,
        },
    )


def _find_existing_timeline(
    registry: ArtifactRegistry, run_dir: Path, manifest_json: str
) -> ArtifactRef | None:
    """Idempotency guard: return an already-registered, content-identical artifact."""
    for ref in registry.for_stage(ARTIFACT_STAGE):
        if ref.kind is not ArtifactKind.TIMELINE:
            continue
        existing_file = Path(run_dir) / ref.path
        if existing_file.is_file() and existing_file.read_text(encoding="utf-8") == manifest_json:
            return ref
    return None


# ---- stage execution (the Hermes-callable boundary) --------------------------


@dataclass(frozen=True)
class TimelineResult:
    """Outcome of one timeline-assembly execution, for future Hermes.

    ``ok`` is trustworthy: ``True`` only after all upstream manifests
    cross-validated, the timeline was built, persisted, registered, and
    successfully reloaded and re-verified.
    """

    ok: bool
    stage: str
    run_id: str
    production_id: str | None
    manifest: TimelineManifest | None
    artifact: ArtifactRef | None
    error: str | None = None


def run_timeline_stage(
    scene_manifest: ProductionManifest,
    asset_manifest: AssetManifest,
    narration_manifest: NarrationManifest,
    run_state: RunState,
    artifact_registry: ArtifactRegistry,
    *,
    logger: StructuredLogger | None = None,
) -> TimelineResult:
    """Execute the deterministic renderer-neutral timeline stage.

    This is the single callable a future orchestrator (Hermes) uses:

        result = run_timeline_stage(
            scene_manifest=scene_manifest,      # from the scene_manifest artifact
            asset_manifest=asset_manifest,      # from the asset_manifest artifact
            narration_manifest=narration_manifest,  # from the narration artifact
            run_state=run_state,
            artifact_registry=registry,
        )

    The stage consumes resolved artifacts only — it never invokes
    providers and never selects renderers. Renderer selection belongs to
    a later orchestration decision (future Hermes).

    Lifecycle (existing P0 state machine):

        pending → running → succeeded          (happy path)
        pending → running → failed             (any stage-critical failure)
        failed  → running → ...                (retry is permitted)

    Policy (explicit, deterministic):
    - Upstream manifests are cross-validated (production identity, scene
      linkage, narration/asset presence) BEFORE assembly.
    - Fail-fast: any missing/inconsistent/unsafe upstream data fails the
      stage and publishes NO timeline. No placeholders, no fake media.
    - The Scene/Asset/Narration manifests are never mutated.
    """
    log = logger if logger is not None else StructuredLogger(level="INFO")
    log = log.bind(stage=STAGE_NAME, run_id=run_state.run_id, job_id=run_state.job_id)
    result = TimelineResult(
        ok=False,
        stage=STAGE_NAME,
        run_id=run_state.run_id,
        production_id=None,
        manifest=None,
        artifact=None,
        error=None,
    )

    def fail(exc: BaseException) -> TimelineResult:
        error = f"{type(exc).__name__}: {exc}"
        try:
            run_state.set_stage(STAGE_NAME, "failed", error=error)
        except Exception as mark_failure_error:
            # state machine corruption must not mask the original cause
            log.error("timeline_stage_failed", error=exc, state_error=str(mark_failure_error))
        else:
            log.error("timeline_stage_failed", error=exc)
        return replace(result, error=error)

    try:
        run_state.set_stage(STAGE_NAME, "running")
        log.info("timeline_stage_started")

        # 1) validate upstream manifests (pydantic when raw dicts are passed)
        if not isinstance(scene_manifest, ProductionManifest):
            scene_manifest = ProductionManifest.model_validate(scene_manifest)
        if not isinstance(asset_manifest, AssetManifest):
            asset_manifest = AssetManifest.model_validate(asset_manifest)
        if not isinstance(narration_manifest, NarrationManifest):
            narration_manifest = NarrationManifest.model_validate(narration_manifest)
        result = replace(result, production_id=scene_manifest.production_id)
        log = log.bind(production_id=scene_manifest.production_id)

        # 2) cross-validate + deterministic temporal assembly
        log.info(
            "timeline_validation_started",
            scenes=len(scene_manifest.scenes),
            resolved_assets=len(asset_manifest.resolved_assets),
            narration_audio=len(narration_manifest.narration_audio),
        )
        manifest = build_timeline(scene_manifest, asset_manifest, narration_manifest)
        log.info(
            "timeline_built",
            scenes=len(manifest.scenes),
            total_duration_seconds=manifest.total_duration_seconds,
        )

        # 3) persist through the existing artifact mechanism (idempotent)
        manifest_json = dump_timeline_manifest(manifest)
        run_dir = artifact_registry.run_dir
        existing = _find_existing_timeline(artifact_registry, run_dir, manifest_json)
        if existing is not None:
            ref = existing
            log.info("timeline_manifest_persisted", artifact_id=ref.artifact_id, reused=True)
        else:
            ref = persist_timeline_manifest(manifest, run_dir, artifact_registry)
            log.info("timeline_manifest_persisted", artifact_id=ref.artifact_id)

        # 4) reload + verify before claiming success
        reloaded = load_timeline_manifest(Path(run_dir) / ref.path)
        if reloaded != manifest:
            raise TimelineError("persisted timeline failed reload verification")

        # 5) success
        run_state.set_stage(STAGE_NAME, "succeeded")
        log.info("timeline_stage_succeeded", artifact_id=ref.artifact_id)
        return replace(result, ok=True, manifest=manifest, artifact=ref)
    except Exception as exc:  # noqa: BLE001 — the stage boundary must never fake success
        return fail(exc)





