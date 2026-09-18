"""P3 — deterministic narration-audio stage (audio provider adapter seam).

A bounded, Hermes-compatible production capability:

    persisted Scene Manifest (P1-A contract)
        → per-scene Narration (in manifest order)
        → provider.health() gate (truthful AdapterHealth)
        → AudioProvider.resolve_narration() per scene
        → ResolvedNarrationAudio (run-local copy + sha256 + provenance)
        → NarrationManifest (separate artifact — NEVER merged into the
          Scene Contract)
        → RunState lifecycle → ArtifactRegistry → reload + verify
        → NarrationAudioResult

Boundary rules:

- The Scene Contract stays pure creative intent: no audio paths, hashes,
  provider ids, or generated-audio metadata. Narration audio lives in a
  separate ``narration_manifest`` artifact.
- The stage is provider-agnostic: it executes the single ``AudioProvider``
  it is given. Provider selection and fallback are orchestration (future
  Hermes) policy.
- ``AudioProvider`` extends the P0 ``Adapter`` ABC; the file-backed
  implementation proves the seam with local fixture WAV files. NO real
  TTS, no FFmpeg, no audio processing.
- Fail-fast policy: if ANY narration fails to resolve, the stage fails
  and publishes NO narration manifest.
- Deterministic: same scene manifest + same fixture directory ⇒ identical
  narration manifest bytes.
- The vertical chain does NOT require P2: narration resolves from the
  Scene Contract alone (visual asset resolution is an independent branch).

FFmpeg note: valid fixture WAV files are generated with the Python
standard library (``wave``); no FFmpeg install is required at P3, and no
decoder validation beyond stdlib ``wave`` parsing is claimed. Real audio
QA belongs to later media stages.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import wave
from abc import abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .adapters import Adapter, AdapterHealth
from .artifacts import ArtifactKind, ArtifactRef, ArtifactRegistry
from .logging import StructuredLogger
from .scene_contract import (
    MANIFEST_FILENAME as SCENE_MANIFEST_FILENAME,
    Narration,
    ProductionManifest,
)
from .state import RunState

__all__ = [
    "STAGE_NAME",
    "ARTIFACT_STAGE",
    "NARRATION_MANIFEST_FILENAME",
    "NARRATION_MANIFEST_SCHEMA_VERSION",
    "AUDIO_DIRNAME",
    "NarrationAudioError",
    "ResolvedNarrationAudio",
    "NarrationAudioProvenance",
    "NarrationManifest",
    "AudioProvider",
    "FileBackedNarrationProvider",
    "NarrationAudioResult",
    "dump_narration_manifest",
    "load_narration_manifest",
    "persist_narration_manifest",
    "run_narration_audio_stage",
]

#: RunState stage label for this capability.
STAGE_NAME = "narration_audio"
#: ArtifactRegistry stage label identifying the producing stage.
ARTIFACT_STAGE = "audio"
#: Persisted narration-manifest filename inside the run directory.
NARRATION_MANIFEST_FILENAME = "narration_manifest.json"
#: Run-relative directory holding resolved narration audio files.
AUDIO_DIRNAME = "audio"

#: Version stamped onto newly created narration manifests.
NARRATION_MANIFEST_SCHEMA_VERSION = "1.0"
#: The only narration-manifest schema major version this build understands.
NARRATION_MANIFEST_SUPPORTED_MAJOR = 1

class NarrationAudioError(RuntimeError):
    """Raised when narration audio cannot be resolved."""


class _ManifestModel(BaseModel):
    """Base for narration-manifest models: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


def _validate_rel_path(value: str) -> str:
    """Validate a run-relative posix path (no absolute, no traversal)."""
    p = Path(value)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise ValueError(f"unsafe run-relative path {value!r}")
    return p.as_posix()


class NarrationAudioProvenance(_ManifestModel):
    """Where resolved narration audio came from.

    Mirrors the P2 provenance shape. For the file-backed provider this
    records that the audio originated from local test fixtures — this
    does NOT represent real-world licensing. ``license`` exists for
    future real TTS providers and stays ``None`` for fixtures.
    """

    provider: str = Field(description="Adapter name that produced the narration audio.")
    source: str = Field(description="Source category, e.g. 'local_fixture'.")
    source_ref: str = Field(description="Provider-relative identifier of the origin (fixture filename).")
    #: Meaningful only for real TTS providers with actual licensing data;
    #: deliberately unset (None) for fixture-sourced audio.
    license: str | None = None


class ResolvedNarrationAudio(_ManifestModel):
    """Resolved narration audio for one scene — outside the Scene Contract.

    Truthful metadata only: ``duration_seconds`` and ``format`` are set
    only when they can actually be determined from the resolved file;
    otherwise they stay ``None``. Nothing is invented.
    """

    scene_id: str
    #: Run-relative posix path of the resolved (copied) audio file.
    path: str
    #: sha256 hex digest of the resolved file content (copy integrity).
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: Duration in seconds — only when truthfully derivable from the file.
    duration_seconds: float | None = None
    #: Audio container/format identifier — only when actually known.
    format: str | None = None
    provenance: "NarrationAudioProvenance"

    _v_path = field_validator("path")(_validate_rel_path)


class NarrationManifest(_ManifestModel):
    """The persisted narration-audio output for one production.

    Document-level artifact: schema version, production identity, the
    scene manifest it was produced from (run-relative reference), the
    provider used, and the narration-audio records in scene order.
    """

    schema_version: str = NARRATION_MANIFEST_SCHEMA_VERSION
    production_id: str
    provider: str
    scene_manifest: str = Field(description="Run-relative path of the source scene manifest artifact.")
    narration_audio: tuple[ResolvedNarrationAudio, ...]

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9]+\.[0-9]+", value):
            raise ValueError(
                f"schema_version {value!r} must be 'MAJOR.MINOR' "
                f"(e.g. {NARRATION_MANIFEST_SCHEMA_VERSION!r})"
            )
        major = int(value.split(".", 1)[0])
        if major != NARRATION_MANIFEST_SUPPORTED_MAJOR:
            raise ValueError(
                f"unsupported schema_version {value!r}; this build understands "
                f"major version {NARRATION_MANIFEST_SUPPORTED_MAJOR} only"
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
    def _check_records(self) -> "NarrationManifest":
        scene_ids = [a.scene_id for a in self.narration_audio]
        duplicates = sorted({sid for sid in scene_ids if scene_ids.count(sid) > 1})
        if duplicates:
            raise ValueError(f"duplicate narration audio for scenes: {duplicates}")
        return self


# ---- provider adapter boundary (extends the P0 Adapter ABC) -----------------


class AudioProvider(Adapter):
    """Provider-agnostic capability boundary for narration audio.

    Extends the P0 ``Adapter`` ABC (``name`` + truthful ``health()``)
    with one operation. The stage depends on THIS interface, never on a
    concrete provider — future Hermes selects and injects providers; the
    stage never chooses between them or routes fallbacks.
    """

    @abstractmethod
    def resolve_narration(
        self,
        narration: Narration,
        scene_id: str,
        *,
        run_dir: Path,
    ) -> ResolvedNarrationAudio:
        """Resolve/generate narration audio for one scene.

        Implementations copy/synthesize the audio into ``run_dir``
        (under ``audio/``) and return a :class:`ResolvedNarrationAudio`
        referencing the copy. Missing audio must raise
        :class:`NarrationAudioError` — never invent paths or silence.
        """


class FileBackedNarrationProvider(AudioProvider):
    """Deterministic local narration provider backed by a fixture directory.

    Architectural stub for a future TTS engine: it does NOT synthesize
    speech. Matching rule (deterministic, obvious): the narration audio
    for scene ``S`` is the file ``<fixture_dir>/<scene_id>.wav``.
    Duration/format metadata is read truthfully from the WAV header via
    the stdlib ``wave`` module and left ``None`` when not derivable.
    """

    name: ClassVar[str] = "file-backed-narration-fixtures"

    #: Default fixture directory, resolved against the current working
    #: directory (repo root in the dev/test workflow). Real TTS providers
    #: will receive their own configuration instead.
    DEFAULT_FIXTURE_DIR: ClassVar[Path] = Path("tests") / "fixtures" / "narration_fixtures"

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

    def resolve_narration(
        self,
        narration: Narration,
        scene_id: str,
        *,
        run_dir: Path,
    ) -> ResolvedNarrationAudio:
        """Deterministically resolve one scene's narration from the fixture directory.

        Lookup: ``<fixture_dir>/<scene_id>.wav``. Missing fixture file
        raises :class:`NarrationAudioError` (explicit failure — no
        invented paths, no fake silence files).
        """
        source_ref = f"{scene_id}.wav"
        source = self.fixture_dir / source_ref
        if not source.is_file():
            raise NarrationAudioError(
                f"narration audio fixture not found for scene {scene_id!r}: expected {source}"
            )
        destination_dir = Path(run_dir) / AUDIO_DIRNAME
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"{scene_id}.wav"
        shutil.copyfile(source, destination)
        content = destination.read_bytes()
        duration, audio_format = _inspect_wav(content)
        return ResolvedNarrationAudio(
            scene_id=scene_id,
            path=f"{AUDIO_DIRNAME}/{destination.name}",
            sha256=hashlib.sha256(content).hexdigest(),
            duration_seconds=duration,
            format=audio_format,
            provenance=NarrationAudioProvenance(
                provider=self.name,
                source="local_fixture",
                source_ref=source_ref,
            ),
        )


def _inspect_wav(data: bytes) -> tuple[float | None, str | None]:
    """Read duration/format from WAV bytes via the stdlib wave module.

    Returns ``(None, None)`` when the file is not a parseable WAV —
    metadata is never invented.
    """
    import io

    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            frames = reader.getnframes()
            framerate = reader.getframerate()
    except (wave.Error, EOFError, OSError):
        return None, None
    if framerate <= 0:
        return None, None
    return round(frames / framerate, 3), "wav"

# ---- serialization / artifact persistence -----------------------------------


def dump_narration_manifest(manifest: NarrationManifest) -> str:
    """Deterministic JSON serialization (fixed field order, no timestamps)."""
    return manifest.model_dump_json(indent=2) + "\n"


def load_narration_manifest(path: str | Path) -> NarrationManifest:
    """Load a narration manifest file: read → validate → deserialize (pydantic)."""
    return NarrationManifest.model_validate_json(Path(path).read_text(encoding="utf-8"))


def persist_narration_manifest(
    manifest: NarrationManifest,
    run_dir: str | Path,
    registry: ArtifactRegistry,
) -> ArtifactRef:
    """Persist the narration manifest and register it (kind ``audio``).

    Reuses the existing P0 artifact system; no new persistence layer.
    """
    target = Path(run_dir) / NARRATION_MANIFEST_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    return registry.register(
        ARTIFACT_STAGE,
        ArtifactKind.AUDIO,
        NARRATION_MANIFEST_FILENAME,
        {
            "schema_version": manifest.schema_version,
            "narration_audio": len(manifest.narration_audio),
            "provider": manifest.provider,
        },
    )


def _find_existing_narration_manifest(
    registry: ArtifactRegistry, run_dir: Path, manifest_json: str
) -> ArtifactRef | None:
    """Idempotency guard: return an already-registered, content-identical artifact."""
    for ref in registry.for_stage(ARTIFACT_STAGE):
        if ref.kind is not ArtifactKind.AUDIO:
            continue
        existing_file = Path(run_dir) / ref.path
        if existing_file.is_file() and existing_file.read_text(encoding="utf-8") == manifest_json:
            return ref
    return None


# ---- stage execution (the Hermes-callable boundary) --------------------------


@dataclass(frozen=True)
class NarrationAudioResult:
    """Outcome of one narration-audio execution, for future Hermes.

    ``ok`` is trustworthy: ``True`` only after every scene's narration
    was resolved, the manifest was validated, persisted, registered, and
    successfully reloaded and re-verified.
    """

    ok: bool
    stage: str
    run_id: str
    production_id: str | None
    provider: str | None
    manifest: NarrationManifest | None
    artifact: ArtifactRef | None
    error: str | None = None


def run_narration_audio_stage(
    scene_manifest: ProductionManifest,
    run_state: RunState,
    artifact_registry: ArtifactRegistry,
    provider: AudioProvider,
    *,
    logger: StructuredLogger | None = None,
) -> NarrationAudioResult:
    """Execute the deterministic narration-audio stage.

    This is the single callable a future orchestrator (Hermes) uses:

        result = run_narration_audio_stage(
            scene_manifest=manifest,   # loaded from the scene_manifest artifact
            run_state=run_state,
            artifact_registry=registry,
            provider=provider,         # selected/injected BY the orchestrator
        )

    Lifecycle (existing P0 state machine):

        pending → running → succeeded          (happy path)
        pending → running → failed             (any stage-critical failure)
        failed  → running → ...                (retry is permitted)

    Policy (explicit, deterministic):
    - The provider's health is checked first; an unhealthy provider fails
      the stage before any resolution attempt.
    - Every scene of the current contract carries narration (it is a
      required field), so every scene is processed, in scene order. If a
      future schema makes narration optional, narration-less scenes are
      skipped (documented forward-compat rule).
    - Fail-fast: if ANY narration fails to resolve, the stage fails and
      publishes NO narration manifest. No fallback routing here.
    - Resolved-audio data is written ONLY to the narration manifest; the
      Scene Contract is never mutated.
    """
    log = logger if logger is not None else StructuredLogger(level="INFO")
    log = log.bind(stage=STAGE_NAME, run_id=run_state.run_id, job_id=run_state.job_id)
    result = NarrationAudioResult(
        ok=False,
        stage=STAGE_NAME,
        run_id=run_state.run_id,
        production_id=None,
        provider=None,
        manifest=None,
        artifact=None,
        error=None,
    )

    def fail(exc: BaseException) -> NarrationAudioResult:
        error = f"{type(exc).__name__}: {exc}"
        try:
            run_state.set_stage(STAGE_NAME, "failed", error=error)
        except Exception as mark_failure_error:
            # state machine corruption must not mask the original cause
            log.error("narration_audio_stage_failed", error=exc, state_error=str(mark_failure_error))
        else:
            log.error("narration_audio_stage_failed", error=exc)
        return replace(result, error=error)

    try:
        run_state.set_stage(STAGE_NAME, "running")
        result = replace(result, provider=provider.name)
        log = log.bind(provider=provider.name)
        log.info("narration_audio_started")

        # 1) provider must be actually usable (truthful AdapterHealth)
        health = provider.health()
        if not health.available:
            raise NarrationAudioError(f"provider {provider.name!r} is unhealthy: {health.detail}")

        # 2) validate the scene manifest input
        if not isinstance(scene_manifest, ProductionManifest):
            scene_manifest = ProductionManifest.model_validate(scene_manifest)
        result = replace(result, production_id=scene_manifest.production_id)
        log = log.bind(production_id=scene_manifest.production_id)

        # 3) resolve every scene's narration, in deterministic scene order
        resolved: list[ResolvedNarrationAudio] = []
        for scene in scene_manifest.scenes:
            log.info(
                "narration_resolution_started",
                scene_id=scene.scene_id,
                characters=len(scene.narration.text),
            )
            audio = provider.resolve_narration(
                scene.narration, scene.scene_id, run_dir=artifact_registry.run_dir
            )
            log.info(
                "narration_audio_resolved", scene_id=audio.scene_id, path=audio.path, sha256=audio.sha256
            )
            resolved.append(audio)

        manifest = NarrationManifest(
            production_id=scene_manifest.production_id,
            provider=provider.name,
            scene_manifest=SCENE_MANIFEST_FILENAME,
            narration_audio=tuple(resolved),
        )

        # 4) persist through the existing artifact mechanism (idempotent)
        manifest_json = dump_narration_manifest(manifest)
        run_dir = artifact_registry.run_dir
        existing = _find_existing_narration_manifest(artifact_registry, run_dir, manifest_json)
        if existing is not None:
            ref = existing
            log.info("narration_manifest_persisted", artifact_id=ref.artifact_id, reused=True)
        else:
            ref = persist_narration_manifest(manifest, run_dir, artifact_registry)
            log.info("narration_manifest_persisted", artifact_id=ref.artifact_id)

        # 5) reload + verify before claiming success
        reloaded = load_narration_manifest(Path(run_dir) / ref.path)
        if reloaded != manifest:
            raise NarrationAudioError("persisted narration manifest failed reload verification")

        # 6) success
        run_state.set_stage(STAGE_NAME, "succeeded")
        log.info("narration_audio_succeeded", artifact_id=ref.artifact_id, resolved=len(resolved))
        return replace(result, ok=True, manifest=manifest, artifact=ref)
    except Exception as exc:  # noqa: BLE001 — the stage boundary must never fake success
        return fail(exc)






