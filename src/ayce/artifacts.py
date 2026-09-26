"""Artifact identity and references.

An :class:`ArtifactRef` is a *reference* to a stage output (research,
script, audio, rendered video, ...). It does not implement any of the
producing subsystems — it only provides safe, durable identity:
who produced it (run + stage), what it is (kind), and where it lives
(a run-relative, validated path). Paths that escape the run directory
(absolute paths, ``..`` segments) are rejected explicitly.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any

from .ids import new_artifact_id
from .state import validate_stage_name

MANIFEST_FILENAME = "artifacts.json"


class ArtifactError(RuntimeError):
    """Raised for invalid artifact references or registry misuse."""


class ArtifactKind(str, Enum):
    """Known artifact kinds for the future Golden Path stages."""

    RESEARCH = "research"
    SCRIPT = "script"
    SCENE_MANIFEST = "scene_manifest"
    ASSET_MANIFEST = "asset_manifest"
    AUDIO = "audio"
    CAPTIONS = "captions"
    TIMELINE = "timeline"
    RENDERED_VIDEO = "rendered_video"
    QA_REPORT = "qa_report"
    # Stage 10 — decision-time policy consumption evidence (observability
    # ONLY: the artifact records WHICH policy version a decision consumed;
    # it is never policy state and never writable through Hermes).
    POLICY_CONSUMPTION = "policy_consumption"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _coerce_kind(kind: ArtifactKind | str) -> ArtifactKind:
    if isinstance(kind, ArtifactKind):
        return kind
    try:
        return ArtifactKind(str(kind).lower())
    except ValueError:
        valid = ", ".join(k.value for k in ArtifactKind)
        raise ArtifactError(f"unknown artifact kind {kind!r}; expected one of: {valid}") from None


def _validate_rel_path(path: str | Path) -> str:
    """Validate a run-relative artifact path and return it in posix form."""
    p = Path(path)
    if p.is_absolute():
        raise ArtifactError(
            f"artifact path must be relative to the run directory, got {str(path)!r}"
        )
    posix = PurePosixPath(p.as_posix())
    if not posix.parts or any(part == ".." for part in posix.parts):
        raise ArtifactError(f"unsafe artifact path {str(path)!r}: must stay inside the run directory")
    return posix.as_posix()


@dataclass(frozen=True)
class ArtifactRef:
    """Immutable reference to a stage output."""

    artifact_id: str
    run_id: str
    stage: str
    kind: ArtifactKind
    path: str  # run-relative, posix
    created_at: str = field(default_factory=_utcnow_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.artifact_id or not isinstance(self.artifact_id, str):
            raise ArtifactError("artifact_id must be a non-empty string")
        if not self.run_id or not isinstance(self.run_id, str):
            raise ArtifactError("run_id must be a non-empty string")
        validate_stage_name(self.stage)
        # direct assignment is not allowed on a frozen dataclass
        object.__setattr__(self, "kind", _coerce_kind(self.kind))
        object.__setattr__(self, "path", _validate_rel_path(self.path))
        if not isinstance(self.metadata, dict):
            raise ArtifactError("metadata must be a dict")

    def with_metadata(self, **extra: Any) -> "ArtifactRef":
        return replace(self, metadata={**self.metadata, **extra})


class ArtifactRegistry:
    """Per-run artifact manifest, persisted as ``<run_dir>/artifacts.json``."""

    def __init__(self, run_dir: str | Path, run_id: str) -> None:
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.manifest_path = self.run_dir / MANIFEST_FILENAME
        self._artifacts: dict[str, ArtifactRef] = {}

    def register(
        self,
        stage: str,
        kind: ArtifactKind | str,
        path: str | Path,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        """Register a new artifact reference and persist the manifest."""
        ref = ArtifactRef(
            artifact_id=new_artifact_id(),
            run_id=self.run_id,
            stage=stage,
            kind=kind,
            path=path,
            metadata=dict(metadata or {}),
        )
        self._artifacts[ref.artifact_id] = ref
        self.save()
        return ref

    def get(self, artifact_id: str) -> ArtifactRef | None:
        return self._artifacts.get(artifact_id)

    def require(self, artifact_id: str) -> ArtifactRef:
        ref = self._artifacts.get(artifact_id)
        if ref is None:
            raise ArtifactError(f"unknown artifact: {artifact_id!r}")
        return ref

    def for_stage(self, stage: str) -> list[ArtifactRef]:
        return [a for a in self._artifacts.values() if a.stage == stage]

    def all(self) -> list[ArtifactRef]:
        return list(self._artifacts.values())

    def save(self) -> Path:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_name(self.manifest_path.name + ".tmp")
        payload = [ref.__dict__ | {"kind": ref.kind.value} for ref in self._artifacts.values()]
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.manifest_path)
        return self.manifest_path

    @classmethod
    def load(cls, run_dir: str | Path, run_id: str) -> "ArtifactRegistry":
        registry = cls(run_dir, run_id)
        if not registry.manifest_path.is_file():
            return registry  # empty registry
        try:
            payload = json.loads(registry.manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ArtifactError(f"corrupt artifact manifest {registry.manifest_path}: {exc}") from exc
        for item in payload:
            ref = ArtifactRef(
                artifact_id=item["artifact_id"],
                run_id=item["run_id"],
                stage=item["stage"],
                kind=item["kind"],
                path=item["path"],
                created_at=item.get("created_at", _utcnow_iso()),
                metadata=item.get("metadata", {}),
            )
            registry._artifacts[ref.artifact_id] = ref
        return registry

