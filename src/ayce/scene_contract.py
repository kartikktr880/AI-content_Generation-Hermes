"""Canonical Scene Contract — the creative-production intermediate representation.

This module defines the stable, machine-readable contract between creative
planning (story/script, scene intelligence) and downstream production
systems (asset resolution, audio, captions, timeline, renderers).

Architectural boundary (P1-A):

- The contract describes CREATIVE INTENT only ("show archival railway
  footage from colonial India"), never resolved physical assets
  (``/assets/video/railway_001.mp4``). Resolved assets, provenance
  records, and renderer instructions belong to future subsystems and
  adapters; they must not enter this model.
- The contract is provider- and renderer-neutral: no FFmpeg, no
  OpenTimelineIO, no TTS/image/video provider concepts. Downstream
  systems consume the contract through adapters.
- Serialization is deterministic JSON so the manifest can be persisted
  as a scene-manifest artifact via the existing P0
  :mod:`ayce.artifacts` registry, reloaded, re-validated, and compared.

Schema versioning: ``schema_version`` is ``MAJOR.MINOR``. New manifests
are stamped with :data:`SCHEMA_VERSION`. Loading accepts the same major
version (additive, backward-compatible minor changes) and explicitly
rejects any other major version — old productions never silently change
meaning. There is deliberately no migration framework at P1-A.
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from .artifacts import ArtifactKind, ArtifactRegistry

#: Version stamped onto every newly created manifest.
SCHEMA_VERSION = "1.0"
#: The only schema major version this code can interpret.
SUPPORTED_MAJOR = 1

MANIFEST_FILENAME = "scene_manifest.json"

_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


def _check_text(value: str) -> str:
    """Strip surrounding whitespace and reject empty text fields."""
    stripped = value.strip()
    if not stripped:
        raise ValueError("must contain non-whitespace text")
    return stripped


def _check_id(value: str) -> str:
    """Validate a stable identifier (no whitespace, <= 64 chars)."""
    if not re.match(_ID_RE, value):
        raise ValueError(
            f"{value!r} is not a valid identifier; expected 1-64 characters of "
            f"letters, digits, '.', '_' or '-' with no whitespace"
        )
    return value


class _ContractModel(BaseModel):
    """Base for all contract models: unknown fields are a contract violation.

    ``extra="forbid"`` keeps renderer-specific or provider-specific fields
    from silently leaking into the canonical creative contract.
    """

    model_config = ConfigDict(extra="forbid")


class AssetKind(str, Enum):
    """Kind of visual asset a resolver must obtain (MVP: visual assets only)."""

    IMAGE = "image"
    VIDEO = "video"


class AssetRequirement(_ContractModel):
    """What a future asset resolver must obtain — NOT the asset itself.

    This is a *requirement*, deliberately separate from any resolved
    asset or provenance record. It contains no filesystem paths, URLs,
    provider names, or binaries. Actual resolved provenance belongs to
    the future asset-resolution subsystem.
    """

    kind: AssetKind
    description: str = Field(description="Resolver brief: what the asset must show.")
    #: Golden Path visuals must be verifiable/usable; this is the *demand*
    #: for documented provenance, not the provenance record itself.
    requires_provenance: bool = True

    _v_description = field_validator("description")(_check_text)


class Narration(_ContractModel):
    """Basic audio intent: the narration text for one scene."""

    text: str = Field(description="Narration text to be spoken over this scene.")

    _v_text = field_validator("text")(_check_text)


class VisualIntent(_ContractModel):
    """Creative visual intent for one scene.

    A scene may express intent ("show archival railway footage from
    colonial India") without any resolved asset: ``requirement`` is
    optional and, when present, is still an unresolved
    :class:`AssetRequirement`.
    """

    description: str = Field(description="Creative description of what the scene shows.")
    requirement: AssetRequirement | None = None

    _v_description = field_validator("description")(_check_text)


class Scene(_ContractModel):
    """One ordered scene of the production (creative intent only)."""

    scene_id: str = Field(description="Stable scene identity, unique within the manifest.")
    sequence: StrictInt = Field(ge=1, description="1-based position in the production.")
    duration_seconds: float = Field(gt=0, description="Intended scene duration; must be positive.")
    narration: Narration
    visual: VisualIntent

    _v_scene_id = field_validator("scene_id")(_check_id)


class ProductionManifest(_ContractModel):
    """The canonical Scene Manifest for one production.

    Document-level contract: schema version, production identity, and an
    ordered list of scenes. Validation guarantees a deterministic,
    self-consistent structure (see the model validator below).
    """

    schema_version: str = SCHEMA_VERSION
    production_id: str = Field(
        description=(
            "Stable production identity; typically an ayce.ids.new_job_id() "
            "(a job is the logical unit of work, stable across retries)."
        )
    )
    title: str = Field(max_length=200, description="Human-readable working title.")
    scenes: tuple[Scene, ...] = Field(min_length=1, description="Ordered scenes (exactly 1..N).")

    _v_production_id = field_validator("production_id")(_check_id)
    _v_title = field_validator("title")(_check_text)

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9]+\.[0-9]+", value):
            raise ValueError(
                f"schema_version {value!r} must be 'MAJOR.MINOR' (e.g. {SCHEMA_VERSION!r})"
            )
        major = int(value.split(".", 1)[0])
        if major != SUPPORTED_MAJOR:
            raise ValueError(
                f"unsupported schema_version {value!r}; this build understands "
                f"major version {SUPPORTED_MAJOR} only"
            )
        return value

    @model_validator(mode="after")
    def _check_scene_ordering(self) -> "ProductionManifest":
        sequences = [scene.sequence for scene in self.scenes]
        if sequences != list(range(1, len(self.scenes) + 1)):
            raise ValueError(
                f"scene sequences must be exactly 1..{len(self.scenes)} in ascending "
                f"order, got {sequences}"
            )
        scene_ids = [scene.scene_id for scene in self.scenes]
        duplicates = sorted({sid for sid in scene_ids if scene_ids.count(sid) > 1})
        if duplicates:
            raise ValueError(f"duplicate scene_id values are not allowed: {duplicates}")
        return self


# ---- serialization / artifact persistence ---------------------------------


def dump_manifest(manifest: ProductionManifest) -> str:
    """Deterministic JSON serialization (fixed field order, stable formatting).

    The contract contains no timestamps or runtime objects, so identical
    manifests always produce identical bytes.
    """
    return manifest.model_dump_json(indent=2) + "\n"


def load_manifest(path: str | Path) -> ProductionManifest:
    """Load a manifest file: read → validate → deserialize (pydantic)."""
    return ProductionManifest.model_validate_json(Path(path).read_text(encoding="utf-8"))


def persist_manifest(
    manifest: ProductionManifest,
    run_dir: str | Path,
    registry: ArtifactRegistry,
    stage: str,
) -> object:
    """Persist the manifest into a run directory and register it as an artifact.

    Reuses the existing P0 artifact system (``ArtifactRegistry`` +
    ``ArtifactKind.SCENE_MANIFEST``); no new artifact abstraction is
    created. The stage argument identifies the (future) producing stage.
    """
    target = Path(run_dir) / MANIFEST_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dump_manifest(manifest), encoding="utf-8")
    return registry.register(
        stage,
        ArtifactKind.SCENE_MANIFEST,
        MANIFEST_FILENAME,
        {"schema_version": manifest.schema_version, "scenes": len(manifest.scenes)},
    )


