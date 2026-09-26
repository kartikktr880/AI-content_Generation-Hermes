"""P6.5 — Deterministic scene-level Captions stage.

First production consumer of the reserved ``ArtifactKind.CAPTIONS``.
Bounded, Hermes-compatible capability that joins two already-verified
upstream contracts into truthful, deterministic captions:

    Scene Contract (scene_manifest.json)  → exact narration TEXT
    Timeline Manifest (timeline.json)     → exact narration INTERVAL
        ↓ build_captions (pure, cross-validating, fail-fast)
    captions.json (CaptionsManifest, ArtifactKind.CAPTIONS)
    captions.srt  (standard SubRip serialization of the same cues)

Boundary rules (explicit):

- ONE cue per scene, in scene order. ``text`` is the scene contract's
  ``narration.text`` VERBATIM — never reflowed, split, or rewritten.
- The cue interval is the timeline's NARRATION element interval
  ``[start, start + duration]`` — the truthful spoken-audio window from
  P3, NOT the full scene interval. No timing is ever invented: there is
  NO phrase-level or word-level timing anywhere upstream, so none is
  produced here (a documented, truthful limitation).
- Fail-fast: any production-identity mismatch, missing timeline scene,
  or missing/ambiguous narration element fails the stage and publishes
  NO artifact. Upstream manifests are never mutated.
- Byte-deterministic idempotency (the scene-manifest pattern): identical
  inputs produce identical captions.json bytes; a content-identical,
  already-registered artifact is reused. A missing captions.srt beside a
  reused report is re-written (serialization of the same evidence).
- SRT timestamps are deterministic (round-half-up to milliseconds).
- This stage does NOT burn captions into the render — the render
  contract is untouched. Styling, positioning, translation, and word
  timing are future capabilities.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from .artifacts import ArtifactKind, ArtifactRef, ArtifactRegistry
from .logging import StructuredLogger
from .narration_audio import NARRATION_MANIFEST_FILENAME, NarrationManifest
from .scene_contract import MANIFEST_FILENAME as SCENE_MANIFEST_FILENAME
from .scene_contract import ProductionManifest
from .state import RunState
from .timeline import TIMELINE_FILENAME, TimelineElementKind, TimelineManifest

__all__ = [
    "STAGE_NAME",
    "ARTIFACT_STAGE",
    "CAPTIONS_FILENAME",
    "SRT_FILENAME",
    "CAPTIONS_MANIFEST_SCHEMA_VERSION",
    "CaptionsError",
    "CaptionCue",
    "CaptionsManifest",
    "CaptionsResult",
    "build_captions",
    "format_srt_timestamp",
    "dump_srt",
    "dump_captions_manifest",
    "load_captions_manifest",
    "run_captions_stage",
]

#: RunState stage label for this capability.
STAGE_NAME = "captions"
#: ArtifactRegistry stage label identifying the producing stage.
ARTIFACT_STAGE = "captions"
#: Persisted structured captions manifest inside the run directory.
CAPTIONS_FILENAME = "captions.json"
#: Persisted standard SubRip serialization of the same cues.
SRT_FILENAME = "captions.srt"
#: Version stamped onto newly created captions manifests.
CAPTIONS_MANIFEST_SCHEMA_VERSION = "1.0"
#: The only captions-manifest schema major version this build understands.
CAPTIONS_SUPPORTED_MAJOR = 1

#: Timing comparison tolerance (matches the timeline stage's epsilon).
_TIMING_EPSILON = 1e-6

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class CaptionsError(RuntimeError):
    """Raised when captions cannot be built truthfully (fail-fast)."""


class _CaptionsModel(BaseModel):
    """Base for captions models: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


def _check_id(value: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValueError(f"invalid identifier {value!r}: no whitespace, 1-64 chars [A-Za-z0-9._-]")
    return value


def _check_text(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("text must be a non-empty, non-whitespace string")
    return value


class CaptionCue(_CaptionsModel):
    """One caption cue: the exact narration text over its exact interval."""

    scene_id: str = Field(description="Scene this cue captions (1:1 with the contract).")
    sequence: StrictInt = Field(ge=1, description="1-based cue position (scene order).")
    #: Narration-element start on the timeline, in seconds.
    start_seconds: float = Field(ge=0)
    #: Narration-element end on the timeline, in seconds (start + audio duration).
    end_seconds: float = Field(gt=0)
    #: Verbatim narration text from the Scene Contract.
    text: str

    _v_scene_id = field_validator("scene_id")(_check_id)
    _v_text = field_validator("text")(_check_text)

    @model_validator(mode="after")
    def _check_interval(self) -> "CaptionCue":
        if self.end_seconds <= self.start_seconds + _TIMING_EPSILON:
            raise ValueError(
                f"cue {self.scene_id!r}: end_seconds {self.end_seconds} must be greater "
                f"than start_seconds {self.start_seconds}"
            )
        return self


class CaptionsManifest(_CaptionsModel):
    """The persisted captions output for one production.

    Document-level artifact: schema version, production identity,
    run-relative references to the upstream artifacts, the standard SRT
    serialization filename, and the cues in scene order.
    """

    schema_version: str = CAPTIONS_MANIFEST_SCHEMA_VERSION
    production_id: str
    #: Run-relative references to the upstream artifacts captions were built from.
    scene_manifest: str
    timeline: str
    narration_manifest: str
    #: Run-relative path of the SRT serialization of these cues.
    srt: str = SRT_FILENAME
    cues: tuple[CaptionCue, ...] = Field(min_length=1)

    _v_production_id = field_validator("production_id")(_check_id)

    @field_validator("scene_manifest")
    @classmethod
    def _check_scene_ref(cls, value: str) -> str:
        if value != SCENE_MANIFEST_FILENAME:
            raise ValueError(
                f"scene_manifest must reference {SCENE_MANIFEST_FILENAME!r}, got {value!r}"
            )
        return value

    @field_validator("timeline")
    @classmethod
    def _check_timeline_ref(cls, value: str) -> str:
        if value != TIMELINE_FILENAME:
            raise ValueError(f"timeline must reference {TIMELINE_FILENAME!r}, got {value!r}")
        return value

    @field_validator("narration_manifest")
    @classmethod
    def _check_narration_ref(cls, value: str) -> str:
        if value != NARRATION_MANIFEST_FILENAME:
            raise ValueError(
                f"narration_manifest must reference {NARRATION_MANIFEST_FILENAME!r}, got {value!r}"
            )
        return value

    @field_validator("srt")
    @classmethod
    def _check_srt_ref(cls, value: str) -> str:
        if value != SRT_FILENAME:
            raise ValueError(f"srt must reference {SRT_FILENAME!r}, got {value!r}")
        return value

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9]+\.[0-9]+", value):
            raise ValueError(
                f"schema_version {value!r} must be 'MAJOR.MINOR' "
                f"(e.g. {CAPTIONS_MANIFEST_SCHEMA_VERSION!r})"
            )
        major = int(value.split(".", 1)[0])
        if major != CAPTIONS_SUPPORTED_MAJOR:
            raise ValueError(
                f"unsupported schema_version {value!r}; this build understands "
                f"major version {CAPTIONS_SUPPORTED_MAJOR} only"
            )
        return value

    @model_validator(mode="after")
    def _check_cue_order(self) -> "CaptionsManifest":
        sequences = [cue.sequence for cue in self.cues]
        if sequences != list(range(1, len(self.cues) + 1)):
            raise ValueError(
                f"cue sequences must be exactly 1..{len(self.cues)} in ascending order, "
                f"got {sequences}"
            )
        scene_ids = [cue.scene_id for cue in self.cues]
        duplicates = sorted({sid for sid in scene_ids if scene_ids.count(sid) > 1})
        if duplicates:
            raise ValueError(f"duplicate cues for scenes: {duplicates}")
        for previous, current in zip(self.cues, self.cues[1:]):
            if current.start_seconds < previous.end_seconds - _TIMING_EPSILON:
                raise ValueError(
                    f"cue {current.scene_id!r} starts at {current.start_seconds} before "
                    f"cue {previous.scene_id!r} ends at {previous.end_seconds}"
                )
        return self


# ---- deterministic assembly ----------------------------------------------------


def build_captions(
    scene_manifest: ProductionManifest,
    timeline_manifest: TimelineManifest,
    narration_manifest: NarrationManifest,
) -> CaptionsManifest:
    """Pure deterministic caption assembly from the three upstream manifests.

    Cross-validation (fail-fast):
    - production identity must agree across all three manifests;
    - every contract scene must have its timeline scene;
    - every timeline scene must carry exactly ONE narration element;
    - the narration element duration must agree with the narration
      manifest's recorded duration (when truthfully known).

    The cue text is the contract's narration text VERBATIM; the cue
    interval is the narration element's exact interval. No timing data
    is invented. Raises :class:`CaptionsError` on any inconsistency.
    """
    for label, pid in (
        ("timeline", timeline_manifest.production_id),
        ("narration manifest", narration_manifest.production_id),
    ):
        if pid != scene_manifest.production_id:
            raise CaptionsError(
                f"production_id mismatch: scene manifest declares "
                f"{scene_manifest.production_id!r} but the {label} declares {pid!r}"
            )

    timeline_by_scene = {scene.scene_id: scene for scene in timeline_manifest.scenes}
    narration_by_scene = {
        record.scene_id: record for record in narration_manifest.narration_audio
    }

    cues: list[CaptionCue] = []
    for scene in scene_manifest.scenes:  # contract order = cue order
        timeline_scene = timeline_by_scene.get(scene.scene_id)
        if timeline_scene is None:
            raise CaptionsError(
                f"missing timeline scene for contract scene {scene.scene_id!r}"
            )
        narration_elements = [
            element
            for element in timeline_scene.elements
            if element.kind is TimelineElementKind.NARRATION
        ]
        if len(narration_elements) != 1:
            raise CaptionsError(
                f"scene {scene.scene_id!r}: expected exactly one narration element in the "
                f"timeline, found {len(narration_elements)}"
            )
        element = narration_elements[0]

        narration_record = narration_by_scene.get(scene.scene_id)
        if (
            narration_record is not None
            and narration_record.duration_seconds is not None
            and abs(narration_record.duration_seconds - element.duration_seconds)
            > _TIMING_EPSILON
        ):
            raise CaptionsError(
                f"scene {scene.scene_id!r}: narration manifest duration "
                f"{narration_record.duration_seconds}s disagrees with the timeline "
                f"narration element duration {element.duration_seconds}s"
            )

        cues.append(
            CaptionCue(
                scene_id=scene.scene_id,
                sequence=scene.sequence,
                start_seconds=element.start_seconds,
                end_seconds=element.start_seconds + element.duration_seconds,
                text=scene.narration.text,
            )
        )

    return CaptionsManifest(
        production_id=scene_manifest.production_id,
        scene_manifest=SCENE_MANIFEST_FILENAME,
        timeline=TIMELINE_FILENAME,
        narration_manifest=NARRATION_MANIFEST_FILENAME,
        cues=tuple(cues),
    )


# ---- serialization (SubRip + manifest JSON) -------------------------------------


def format_srt_timestamp(seconds: float) -> str:
    """Format seconds as a deterministic SubRip timestamp ``HH:MM:SS,mmm``.

    Rounding rule (documented): round HALF-UP to the nearest millisecond.
    Negative input or timestamps beyond the SubRip limit (99:59:59,999)
    raise :class:`CaptionsError` — never silently wrapped timestamps.
    """
    total_ms = int(seconds * 1000 + 0.5)
    if seconds < 0:
        raise CaptionsError(f"negative SRT timestamp: {seconds}")
    if total_ms > 99 * 3600 * 1000 + 59 * 60 * 1000 + 59 * 1000 + 999:
        raise CaptionsError(f"SRT timestamp beyond the 99:59:59,999 limit: {seconds}")
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def dump_srt(manifest: CaptionsManifest) -> str:
    """Deterministic SubRip serialization of the manifest's cues."""
    blocks = []
    for cue in manifest.cues:
        blocks.append(
            f"{cue.sequence}\n"
            f"{format_srt_timestamp(cue.start_seconds)} --> "
            f"{format_srt_timestamp(cue.end_seconds)}\n"
            f"{cue.text}\n"
        )
    return "\n".join(blocks)


def dump_captions_manifest(manifest: CaptionsManifest) -> str:
    """Deterministic JSON serialization (fixed field order, no timestamps)."""
    return manifest.model_dump_json(indent=2) + "\n"


def load_captions_manifest(path: str | Path) -> CaptionsManifest:
    """Load a captions manifest file: read → validate → deserialize (pydantic)."""
    return CaptionsManifest.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _write_atomic(target: Path, content: str) -> None:
    """Atomic text write (tmp file + replace) — the repository convention."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(target)


def persist_captions(
    manifest: CaptionsManifest,
    run_dir: str | Path,
    registry: ArtifactRegistry,
) -> ArtifactRef:
    """Persist captions.json + captions.srt and register the manifest artifact.

    Reuses the existing P0 artifact system; the registered artifact is
    the structured captions manifest (kind ``captions``) whose metadata
    records the SRT serialization path.
    """
    run_dir = Path(run_dir)
    _write_atomic(run_dir / CAPTIONS_FILENAME, dump_captions_manifest(manifest))
    _write_atomic(run_dir / SRT_FILENAME, dump_srt(manifest))
    return registry.register(
        ARTIFACT_STAGE,
        ArtifactKind.CAPTIONS,
        CAPTIONS_FILENAME,
        {
            "schema_version": manifest.schema_version,
            "cues": len(manifest.cues),
            "srt": SRT_FILENAME,
            "production_id": manifest.production_id,
        },
    )


def _find_existing_captions(
    registry: ArtifactRegistry, run_dir: Path, manifest_json: str
) -> ArtifactRef | None:
    """Idempotency guard: return an already-registered, content-identical artifact."""
    for ref in registry.for_stage(ARTIFACT_STAGE):
        if ref.kind is not ArtifactKind.CAPTIONS:
            continue
        existing_file = Path(run_dir) / ref.path
        if existing_file.is_file() and existing_file.read_text(encoding="utf-8") == manifest_json:
            return ref
    return None


# ---- stage execution (the Hermes-callable boundary) -----------------------------


@dataclass(frozen=True)
class CaptionsResult:
    """Outcome of one captions execution, for future Hermes.

    ``ok`` is trustworthy: ``True`` only after every cue was built from
    verified upstream contracts, both files were persisted, the artifact
    was registered, and the manifest was successfully reloaded.
    """

    ok: bool
    stage: str
    run_id: str
    production_id: str | None
    manifest: CaptionsManifest | None
    artifact: ArtifactRef | None
    error: str | None = None


def run_captions_stage(
    scene_manifest: ProductionManifest,
    timeline_manifest: TimelineManifest,
    narration_manifest: NarrationManifest,
    run_state: RunState,
    artifact_registry: ArtifactRegistry,
    *,
    logger: StructuredLogger | None = None,
) -> CaptionsResult:
    """Execute the deterministic scene-level captions stage.

    This is the single callable a future orchestrator (Hermes) uses:

        result = run_captions_stage(
            scene_manifest=scene_manifest,     # from the scene_manifest artifact
            timeline_manifest=timeline,        # from the timeline artifact
            narration_manifest=narration,      # from the narration artifact
            run_state=run_state,
            artifact_registry=registry,
        )

    Lifecycle (existing P0 state machine):

        pending → running → succeeded          (happy path)
        pending → running → failed             (any stage-critical failure)
        failed  → running → ...                (retry is permitted)

    Policy (explicit, deterministic):
    - Cue text is the contract narration text VERBATIM; cue intervals
      are the timeline narration element intervals. No timing invented.
    - Fail-fast: any upstream inconsistency fails the stage and publishes
      NO captions artifact. The Scene/Timeline/Narration manifests are
      never mutated.
    - Byte-deterministic idempotency: identical inputs reuse the existing
      registered artifact; the SRT is re-written only if missing.
    """
    log = logger if logger is not None else StructuredLogger(level="INFO")
    log = log.bind(stage=STAGE_NAME, run_id=run_state.run_id, job_id=run_state.job_id)
    result = CaptionsResult(
        ok=False,
        stage=STAGE_NAME,
        run_id=run_state.run_id,
        production_id=None,
        manifest=None,
        artifact=None,
        error=None,
    )

    def fail(exc: BaseException) -> CaptionsResult:
        error = f"{type(exc).__name__}: {exc}"
        try:
            run_state.set_stage(STAGE_NAME, "failed", error=error)
        except Exception as mark_failure_error:
            # state machine corruption must not mask the original cause
            log.error("captions_stage_failed", error=exc, state_error=str(mark_failure_error))
        else:
            log.error("captions_stage_failed", error=exc)
        return replace(result, error=error)

    try:
        run_state.set_stage(STAGE_NAME, "running")
        log.info("captions_stage_started")

        # 1) validate upstream manifests (pydantic when raw dicts are passed)
        if not isinstance(scene_manifest, ProductionManifest):
            scene_manifest = ProductionManifest.model_validate(scene_manifest)
        if not isinstance(timeline_manifest, TimelineManifest):
            timeline_manifest = TimelineManifest.model_validate(timeline_manifest)
        if not isinstance(narration_manifest, NarrationManifest):
            narration_manifest = NarrationManifest.model_validate(narration_manifest)
        result = replace(result, production_id=scene_manifest.production_id)
        log = log.bind(production_id=scene_manifest.production_id)

        # 2) deterministic cross-validated assembly
        log.info(
            "captions_build_started",
            scenes=len(scene_manifest.scenes),
            timeline_scenes=len(timeline_manifest.scenes),
        )
        manifest = build_captions(scene_manifest, timeline_manifest, narration_manifest)
        log.info("captions_built", cues=len(manifest.cues))

        # 3) persist through the existing artifact mechanism (idempotent)
        run_dir = artifact_registry.run_dir
        manifest_json = dump_captions_manifest(manifest)
        existing = _find_existing_captions(artifact_registry, run_dir, manifest_json)
        if existing is not None:
            ref = existing
            # the SRT is a serialization of the SAME evidence — restore if missing
            if not (Path(run_dir) / SRT_FILENAME).is_file():
                _write_atomic(Path(run_dir) / SRT_FILENAME, dump_srt(manifest))
            log.info("captions_manifest_persisted", artifact_id=ref.artifact_id, reused=True)
        else:
            ref = persist_captions(manifest, run_dir, artifact_registry)
            log.info("captions_manifest_persisted", artifact_id=ref.artifact_id)

        # 4) reload + verify before claiming success
        reloaded = load_captions_manifest(Path(run_dir) / ref.path)
        if reloaded != manifest:
            raise CaptionsError("persisted captions failed reload verification")
        if not (Path(run_dir) / SRT_FILENAME).is_file():
            raise CaptionsError("captions.srt is missing after captions persistence")

        # 5) success
        run_state.set_stage(STAGE_NAME, "succeeded")
        log.info(
            "captions_stage_succeeded", artifact_id=ref.artifact_id, cues=len(manifest.cues)
        )
        return replace(result, ok=True, manifest=manifest, artifact=ref)
    except Exception as exc:  # noqa: BLE001 — the stage boundary must never fake success
        return fail(exc)
