"""Stage / run state with validated transitions and durable JSON checkpoints.

Local-file based by design (scratch build). No databases, queues, or
distributed infrastructure. State files are written atomically so a crash
cannot leave a half-written checkpoint.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

_STAGE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class StateError(RuntimeError):
    """Raised for malformed or unreadable state."""


class InvalidTransitionError(StateError):
    """Raised when a stage status transition is not allowed."""


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYING = "retrying"


def _load_lineage(value: Any) -> dict[str, Any] | None:
    """Tolerant lineage reload (Stage 3): absent/None → None (old state
    files); a JSON object passes through; anything else is corrupt state."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    raise StateError(f"corrupt state: lineage must be a JSON object or null, got {type(value).__name__}")


_ALLOWED_TRANSITIONS: dict[StageStatus, frozenset[StageStatus]] = {
    StageStatus.PENDING: frozenset({StageStatus.RUNNING}),
    StageStatus.RUNNING: frozenset({StageStatus.SUCCEEDED, StageStatus.FAILED, StageStatus.RETRYING}),
    StageStatus.RETRYING: frozenset({StageStatus.RUNNING, StageStatus.FAILED}),
    StageStatus.FAILED: frozenset({StageStatus.RETRYING, StageStatus.RUNNING}),
    StageStatus.SUCCEEDED: frozenset(),  # terminal
}


def validate_stage_name(stage: str) -> str:
    if not isinstance(stage, str) or not _STAGE_NAME_RE.match(stage):
        raise StateError(
            f"invalid stage name {stage!r}; expected lowercase snake_case identifier "
            f"(letters/digits/underscore, max 64 chars)"
        )
    return stage


@dataclass
class StageRecord:
    status: StageStatus = StageStatus.PENDING
    attempts: int = 0
    updated_at: str = field(default_factory=_utcnow_iso)
    last_error: str | None = None


@dataclass
class RunState:
    """State of one run: identity plus per-stage status.

    ``lineage`` (Stage 3, optional) holds the durable production lineage
    references ({script_id, script_sha256, research_id, research_sha256,
    objective_id, objective_text}) when the run is research-derived. It is
    ADDITIVE: old state files without the field load unchanged
    (``lineage=None``), and it is serialized only when present so re-saved
    legacy state keeps its historical shape.
    """

    run_id: str
    job_id: str
    created_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)
    stages: dict[str, StageRecord] = field(default_factory=dict)
    lineage: dict[str, Any] | None = None

    def stage(self, name: str) -> StageRecord:
        validate_stage_name(name)
        if name not in self.stages:
            raise StateError(f"unknown stage {name!r} in run {self.run_id}")
        return self.stages[name]

    def set_stage(self, stage: str, status: StageStatus | str, *, error: str | None = None) -> StageRecord:
        """Transition a stage to ``status``, enforcing the state machine."""
        stage = validate_stage_name(stage)
        if isinstance(status, str):
            try:
                status = StageStatus(status.lower())
            except ValueError:
                valid = ", ".join(s.value for s in StageStatus)
                raise StateError(f"unknown stage status {status!r}; expected one of: {valid}") from None

        record = self.stages.setdefault(stage, StageRecord())
        old = record.status
        if status not in _ALLOWED_TRANSITIONS[old]:
            raise InvalidTransitionError(
                f"invalid transition for stage {stage!r}: {old.value} -> {status.value}"
            )
        record.status = status
        record.updated_at = _utcnow_iso()
        record.last_error = error
        if status is StageStatus.RUNNING:
            record.attempts += 1
        self.updated_at = _utcnow_iso()
        return record

    # ---- serialization -------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "job_id": self.job_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "stages": {
                name: {
                    "status": rec.status.value,
                    "attempts": rec.attempts,
                    "updated_at": rec.updated_at,
                    "last_error": rec.last_error,
                }
                for name, rec in self.stages.items()
            },
        }
        if self.lineage is not None:
            # additive Stage 3 field; serialized ONLY when present so a
            # re-saved legacy state keeps its historical shape
            payload["lineage"] = self.lineage
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunState":
        try:
            stages = {}
            for name, raw in data.get("stages", {}).items():
                try:
                    status = StageStatus(raw["status"])
                except (ValueError, KeyError):
                    raise StateError(
                        f"corrupt state: unknown status {raw.get('status')!r} for stage {name!r}"
                    )
                stages[name] = StageRecord(
                    status=status,
                    attempts=int(raw.get("attempts", 0)),
                    updated_at=raw.get("updated_at", _utcnow_iso()),
                    last_error=raw.get("last_error"),
                )
            return cls(
                run_id=data["run_id"],
                job_id=data["job_id"],
                created_at=data.get("created_at", _utcnow_iso()),
                updated_at=data.get("updated_at", _utcnow_iso()),
                stages=stages,
                lineage=_load_lineage(data.get("lineage")),
            )
        except KeyError as exc:
            raise StateError(f"corrupt state: missing required field {exc}") from None

    def save(self, path: str | Path) -> Path:
        """Atomically persist to ``path`` as JSON (tmp file + os.replace)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, path: str | Path) -> "RunState":
        path = Path(path)
        if not path.is_file():
            raise StateError(f"state file not found: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StateError(f"corrupt state file {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise StateError(f"corrupt state file {path}: expected a JSON object")
        return cls.from_dict(data)

