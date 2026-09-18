"""P2 — deterministic file-backed asset resolution (provider adapter seam).

A bounded, Hermes-compatible production capability:

    persisted Scene Manifest (P1-A contract)
        → AssetRequirement extraction (per scene, in manifest order)
        → provider.health() gate (truthful AdapterHealth)
        → AssetProvider.resolve() per requirement
        → ResolvedAsset (provider-owned copy into the run directory)
        → AssetManifest (separate artifact — NEVER merged into the
          Scene Contract)
        → RunState lifecycle → ArtifactRegistry → reload + verify
        → AssetResolutionResult

Boundary rules:

- Creative intent (Scene Contract) ≠ Asset Requirement ≠ Resolved Asset.
  Resolved-asset data must NOT be written back into ``ProductionManifest``;
  it lives in a separate ``asset_manifest`` artifact.
- The stage is provider-agnostic: it executes the single ``AssetProvider``
  it is given and never chooses providers or fallbacks. Selection and
  fallback are orchestration (future Hermes) policy.
- The provider boundary extends the P0 ``Adapter`` ABC (``name``,
  truthful ``health()``); the file-backed implementation proves the seam
  with local fixture files only. No network, no scraping, no AI.
- Partial-resolution policy (documented, explicit): if ANY required asset
  fails to resolve, the stage fails and publishes NO asset manifest.
  Scenes without an ``AssetRequirement`` are skipped — no fake resolution.
- Deterministic: same scene manifest + same fixture directory ⇒ identical
  asset manifest bytes (no randomness, no timestamps in records).

Copy semantics: resolved files are COPIED into the run directory
(``assets/``) so the run is self-contained; each ``ResolvedAsset`` records
the run-relative copy path plus a sha256 integrity digest. Copied media
files are referenced by the manifest (no separate artifact refs at P2).

Fixture provenance is NOT real-world licensing: the file-backed provider
marks every record as originating from a local test fixture; a ``license``
field exists for future real providers and stays unset (``None``) for
fixtures.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from abc import abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .adapters import Adapter, AdapterHealth
from .artifacts import ArtifactKind, ArtifactRef, ArtifactRegistry
from .logging import StructuredLogger
from .adapters import Adapter, AdapterHealth
from .artifacts import ArtifactKind, ArtifactRef, ArtifactRegistry
from .logging import StructuredLogger
from .scene_contract import (
    AssetKind,
    AssetRequirement,
    MANIFEST_FILENAME as SCENE_MANIFEST_FILENAME,
    ProductionManifest,
)
from .state import RunState

__all__ = [
    "STAGE_NAME",
    "ARTIFACT_STAGE",
    "ASSET_MANIFEST_FILENAME",
    "ASSET_MANIFEST_SCHEMA_VERSION",
    "AssetResolutionError",
    "AssetProvenance",
    "ResolvedAsset",
    "AssetManifest",
    "AssetProvider",
    "FileBackedAssetProvider",
    "AssetResolutionResult",
    "dump_asset_manifest",
    "load_asset_manifest",
    "persist_asset_manifest",
    "run_asset_resolution_stage",
]

#: RunState stage label for this capability.
STAGE_NAME = "asset_resolution"
#: ArtifactRegistry stage label identifying the producing stage.
ARTIFACT_STAGE = "asset_manifest"
#: Persisted asset-manifest filename inside the run directory.
ASSET_MANIFEST_FILENAME = "asset_manifest.json"
#: Run-relative directory holding resolved (copied) media files.
ASSETS_DIRNAME = "assets"

#: Version stamped onto newly created asset manifests.
ASSET_MANIFEST_SCHEMA_VERSION = "1.0"
#: The only asset-manifest schema major version this build understands.
ASSET_MANIFEST_SUPPORTED_MAJOR = 1


class AssetResolutionError(RuntimeError):
    """Raised when an asset requirement cannot be resolved."""


# ---- resolved-asset records (separate from the Scene Contract) --------------


class _ManifestModel(BaseModel):
    """Base for asset-manifest models: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


def _check_text(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must contain non-whitespace text")
    return stripped


def _validate_rel_path(value: str) -> str:
    """Validate a run-relative posix path (no absolute, no traversal)."""
    p = Path(value)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise ValueError(f"unsafe run-relative path {value!r}")
    return p.as_posix()


class AssetProvenance(_ManifestModel):
    """Where a resolved asset came from.

    For the file-backed provider this records that the asset originated
    from the local test-fixture provider. This does NOT represent
    real-world licensing. ``license`` exists for future real providers
    and stays ``None`` for fixtures.
    """

    provider: str = Field(description="Adapter name that performed the resolution.")
    source: str = Field(description="Source category, e.g. 'local_fixture'.")
    source_ref: str = Field(description="Provider-relative identifier of the origin (fixture filename).")
    #: Meaningful only for real providers with actual licensing data;
    #: deliberately unset (None) for fixture-sourced assets.
    license: str | None = None

    _v_provider = field_validator("provider")(_check_text)
    _v_source = field_validator("source")(_check_text)
    _v_source_ref = field_validator("source_ref")(_check_text)


class ResolvedAsset(_ManifestModel):
    """A resolved physical asset — deliberately OUTSIDE the Scene Contract.

    Links one scene's ``AssetRequirement`` to a run-local copied file,
    with integrity digest and provenance. Never inserted back into
    ``ProductionManifest``.
    """

    scene_id: str
    kind: AssetKind
    #: Run-relative posix path of the resolved (copied) file.
    path: str
    #: sha256 hex digest of the resolved file content (copy integrity).
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: Echo of the requirement brief that this asset satisfies.
    requirement_description: str
    provenance: AssetProvenance

    _v_path = field_validator("path")(_validate_rel_path)
    _v_requirement = field_validator("requirement_description")(_check_text)

class AssetManifest(_ManifestModel):
    """The persisted asset-resolution output for one production.

    Document-level artifact: schema version, production identity, the
    scene manifest it was resolved from (run-relative reference), the
    provider used, and the resolved-asset records in scene order.
    """

    schema_version: str = ASSET_MANIFEST_SCHEMA_VERSION
    production_id: str
    provider: str
    scene_manifest: str = Field(description="Run-relative path of the source scene manifest artifact.")
    resolved_assets: tuple[ResolvedAsset, ...]

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9]+\.[0-9]+", value):
            raise ValueError(
                f"schema_version {value!r} must be 'MAJOR.MINOR' (e.g. {ASSET_MANIFEST_SCHEMA_VERSION!r})"
            )
        major = int(value.split(".", 1)[0])
        if major != ASSET_MANIFEST_SUPPORTED_MAJOR:
            raise ValueError(
                f"unsupported schema_version {value!r}; this build understands "
                f"major version {ASSET_MANIFEST_SUPPORTED_MAJOR} only"
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

    @model_validator(mode="after")
    def _check_records(self) -> "AssetManifest":
        scene_ids = [a.scene_id for a in self.resolved_assets]
        duplicates = sorted({sid for sid in scene_ids if scene_ids.count(sid) > 1})
        if duplicates:
            raise ValueError(f"duplicate resolved assets for scenes: {duplicates}")
        return self


# ---- provider adapter boundary (extends the P0 Adapter ABC) -----------------


class AssetProvider(Adapter):
    """Provider-agnostic capability boundary for asset resolution.

    Extends the P0 ``Adapter`` ABC (``name`` + truthful ``health()``)
    with a single resolution operation. The stage depends on THIS
    interface, never on a concrete provider — future Hermes selects and
    injects providers; the stage never chooses between them.
    """

    @abstractmethod
    def resolve(
        self,
        requirement: AssetRequirement,
        scene_id: str,
        *,
        run_dir: Path,
    ) -> ResolvedAsset:
        """Resolve one requirement into a run-local copied asset.

        Implementations copy the resolved media into ``run_dir`` (under
        ``assets/``) and return a :class:`ResolvedAsset` referencing the
        copy. Missing assets must raise :class:`AssetResolutionError` —
        never invent paths or placeholders.
        """


class FileBackedAssetProvider(AssetProvider):
    """Trivial deterministic local provider backed by a fixture directory.

    Matching rule (deterministic, obvious): the asset for scene ``S`` of
    kind ``K`` is the file ``<fixture_dir>/<scene_id><ext>`` where the
    extension follows :data:`EXTENSIONS`. Anything else (fuzzy search,
    embeddings, AI matching) is explicitly out of scope at P2.
    """

    name: ClassVar[str] = "file-backed-fixtures"

    #: Deterministic filename extension per asset kind.
    EXTENSIONS: ClassVar[dict[AssetKind, str]] = {AssetKind.IMAGE: ".png", AssetKind.VIDEO: ".mp4"}

    #: Default fixture directory, resolved against the current working
    #: directory (repo root in the dev/test workflow). Real providers
    #: will receive their own configuration instead.
    DEFAULT_FIXTURE_DIR: ClassVar[Path] = Path("tests") / "fixtures" / "asset_provider"

    def __init__(self, config, fixture_dir: str | Path | None = None) -> None:
        super().__init__(config)
        self.fixture_dir = Path(fixture_dir) if fixture_dir is not None else Path(self.DEFAULT_FIXTURE_DIR)

    def health(self) -> AdapterHealth:
        """Truthful: healthy only if the fixture directory exists and is readable."""
        if not self.fixture_dir.is_dir():
            return AdapterHealth(
                name=self.name,
                available=False,
                detail=f"fixture directory not found: {self.fixture_dir}",
            )
        try:
            file_count = sum(1 for _ in self.fixture_dir.iterdir())
        except OSError as exc:
            return AdapterHealth(name=self.name, available=False, detail=f"unreadable: {exc}")
        return AdapterHealth(
            name=self.name,
            available=True,
            detail=f"fixture directory usable ({file_count} entries)",
        )

    def resolve(
        self,
        requirement: AssetRequirement,
        scene_id: str,
        *,
        run_dir: Path,
    ) -> ResolvedAsset:
        """Deterministically resolve one requirement from the fixture directory.

        Lookup: ``<fixture_dir>/<scene_id><extension>``. Missing fixture
        file raises :class:`AssetResolutionError` (explicit failure — no
        invented paths, no placeholders).
        """
        extension = self.EXTENSIONS.get(requirement.kind)
        if extension is None:
            raise AssetResolutionError(
                f"provider {self.name!r} cannot resolve asset kind {requirement.kind.value!r}"
            )
        source_ref = f"{scene_id}{extension}"
        source = self.fixture_dir / f"{scene_id}{extension}"
        if not source.is_file():
            raise AssetResolutionError(
                f"fixture asset not found for scene {scene_id!r} "
                f"({requirement.kind.value}): expected {source}"
            )
        destination_dir = Path(run_dir) / ASSETS_DIRNAME
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"{scene_id}{extension}"
        shutil.copyfile(source, destination)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        return ResolvedAsset(
            scene_id=scene_id,
            kind=requirement.kind,
            path=f"{ASSETS_DIRNAME}/{destination.name}",
            sha256=digest,
            requirement_description=requirement.description,
            provenance=AssetProvenance(
                provider=self.name,
                source="local_fixture",
                source_ref=source_ref,
            ),
        )

# ---- serialization / artifact persistence -----------------------------------


def dump_asset_manifest(manifest: AssetManifest) -> str:
    """Deterministic JSON serialization (fixed field order, no timestamps)."""
    return manifest.model_dump_json(indent=2) + "\n"


def load_asset_manifest(path: str | Path) -> AssetManifest:
    """Load an asset manifest file: read → validate → deserialize (pydantic)."""
    return AssetManifest.model_validate_json(Path(path).read_text(encoding="utf-8"))


def persist_asset_manifest(
    manifest: AssetManifest,
    run_dir: str | Path,
    registry: ArtifactRegistry,
) -> ArtifactRef:
    """Persist the asset manifest and register it (kind ``asset_manifest``).

    Reuses the existing P0 artifact system; no new persistence layer.
    """
    target = Path(run_dir) / ASSET_MANIFEST_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    return registry.register(
        ARTIFACT_STAGE,
        ArtifactKind.ASSET_MANIFEST,
        ASSET_MANIFEST_FILENAME,
        {
            "schema_version": manifest.schema_version,
            "resolved_assets": len(manifest.resolved_assets),
            "provider": manifest.provider,
        },
    )


def _find_existing_asset_manifest(
    registry: ArtifactRegistry, run_dir: Path, manifest_json: str
) -> ArtifactRef | None:
    """Idempotency guard: return an already-registered, content-identical artifact."""
    for ref in registry.for_stage(ARTIFACT_STAGE):
        if ref.kind is not ArtifactKind.ASSET_MANIFEST:
            continue
        existing_file = Path(run_dir) / ref.path
        if existing_file.is_file() and existing_file.read_text(encoding="utf-8") == manifest_json:
            return ref
    return None


# ---- stage execution (the Hermes-callable boundary) --------------------------


@dataclass(frozen=True)
class AssetResolutionResult:
    """Outcome of one asset-resolution execution, for future Hermes.

    ``ok`` is trustworthy: ``True`` only after every requirement was
    resolved, the manifest was validated, persisted, registered, and
    successfully reloaded and re-verified.
    """

    ok: bool
    stage: str
    run_id: str
    production_id: str | None
    provider: str | None
    manifest: AssetManifest | None
    artifact: ArtifactRef | None
    error: str | None = None


def run_asset_resolution_stage(
    scene_manifest: ProductionManifest,
    run_state: RunState,
    artifact_registry: ArtifactRegistry,
    provider: AssetProvider,
    *,
    logger: StructuredLogger | None = None,
) -> AssetResolutionResult:
    """Execute the deterministic Asset Resolution stage.

    This is the single callable a future orchestrator (Hermes) uses:

        result = run_asset_resolution_stage(
            scene_manifest=manifest,       # loaded from the scene_manifest artifact
            run_state=run_state,
            artifact_registry=registry,
            provider=provider,             # selected/injected BY the orchestrator
        )

    Lifecycle (existing P0 state machine):

        pending → running → succeeded          (happy path)
        pending → running → failed             (any stage-critical failure)
        failed  → running → ...                (retry is permitted)

    Policy (explicit, deterministic):
    - The provider's health is checked first; an unhealthy provider fails
      the stage before any resolution attempt.
    - Scenes without an ``AssetRequirement`` are skipped (no fake assets).
    - Fail-fast: if ANY requirement fails to resolve, the stage fails and
      publishes NO asset manifest. No fallback routing here — provider
      selection/fallback is orchestration policy (future Hermes).
    - Resolved-asset data is written ONLY to the asset manifest; the
      Scene Contract is never mutated.
    """
    log = logger if logger is not None else StructuredLogger(level="INFO")
    log = log.bind(stage=STAGE_NAME, run_id=run_state.run_id, job_id=run_state.job_id)
    result = AssetResolutionResult(
        ok=False,
        stage=STAGE_NAME,
        run_id=run_state.run_id,
        production_id=None,
        provider=None,
        manifest=None,
        artifact=None,
        error=None,
    )

    def fail(exc: BaseException) -> AssetResolutionResult:
        error = f"{type(exc).__name__}: {exc}"
        try:
            run_state.set_stage(STAGE_NAME, "failed", error=error)
        except Exception as mark_failure_error:
            # state machine corruption must not mask the original cause
            log.error("asset_resolution_stage_failed", error=exc, state_error=str(mark_failure_error))
        else:
            log.error("asset_resolution_stage_failed", error=exc)
        return replace(result, error=error)

    try:
        run_state.set_stage(STAGE_NAME, "running")
        result = replace(result, provider=provider.name)
        log = log.bind(provider=provider.name)
        log.info("asset_resolution_started")

        # 1) provider must be actually usable (truthful AdapterHealth)
        health = provider.health()
        if not health.available:
            raise AssetResolutionError(f"provider {provider.name!r} is unhealthy: {health.detail}")

        # 2) validate the scene manifest input
        if not isinstance(scene_manifest, ProductionManifest):
            scene_manifest = ProductionManifest.model_validate(scene_manifest)
        result = replace(result, production_id=scene_manifest.production_id)
        log = log.bind(production_id=scene_manifest.production_id)

        # 3) resolve every requirement, in deterministic scene order
        resolved: list[ResolvedAsset] = []
        for scene in scene_manifest.scenes:
            requirement = scene.visual.requirement
            if requirement is None:
                continue  # documented: scenes without requirements are skipped
            log.info(
                "asset_requirement_resolution_started",
                scene_id=scene.scene_id,
                kind=requirement.kind.value,
            )
            asset = provider.resolve(requirement, scene.scene_id, run_dir=artifact_registry.run_dir)
            log.info("asset_resolved", scene_id=asset.scene_id, path=asset.path, sha256=asset.sha256)
            resolved.append(asset)

        manifest = AssetManifest(
            production_id=scene_manifest.production_id,
            provider=provider.name,
            scene_manifest=SCENE_MANIFEST_FILENAME,
            resolved_assets=tuple(resolved),
        )

        # 4) persist through the existing artifact mechanism (idempotent)
        manifest_json = dump_asset_manifest(manifest)
        run_dir = artifact_registry.run_dir
        existing = _find_existing_asset_manifest(artifact_registry, run_dir, manifest_json)
        if existing is not None:
            ref = existing
            log.info("asset_manifest_persisted", artifact_id=ref.artifact_id, reused=True)
        else:
            ref = persist_asset_manifest(manifest, run_dir, artifact_registry)
            log.info("asset_manifest_persisted", artifact_id=ref.artifact_id)

        # 5) reload + verify before claiming success
        reloaded = load_asset_manifest(Path(run_dir) / ref.path)
        if reloaded != manifest:
            raise AssetResolutionError("persisted asset manifest failed reload verification")

        # 6) success
        run_state.set_stage(STAGE_NAME, "succeeded")
        log.info("asset_resolution_succeeded", artifact_id=ref.artifact_id, resolved=len(resolved))
        return replace(result, ok=True, manifest=manifest, artifact=ref)
    except Exception as exc:  # noqa: BLE001 — the stage boundary must never fake success
        return fail(exc)





