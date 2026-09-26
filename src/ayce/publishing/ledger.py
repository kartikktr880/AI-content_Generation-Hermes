"""Stage 5 — Publishing ledger (worker-internal SQLite, publishing ONLY).

The FIRST (and only) SQLite component of AYCE. It owns exclusively the
external side-effect state of publishing — it is NOT a RunState/Artifact
replacement and never touches production state, which remains JSON.

One row per publish identity::

    (package_seal, destination)  — UNIQUE

The package seal is the natural idempotency key (Stage 4): the same
sealed production content published to the same destination can only
ever produce ONE ledger row, hence ONE upload.

States (explicit small machine, §19)::

    created → validated → auth_ready → upload_in_progress → uploaded
            → reconciling → published
    failures: validation_failed / auth_failed / upload_failed /
              reconciliation_pending / reconciliation_failed

Crash safety: every state transition is a single SQLite transaction
(WAL journal; ``busy_timeout`` for multi-process contention). The
resumable upload session URL and byte offset are persisted so a new
process can resume instead of re-uploading.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["PUBLISH_STATES", "PublishRecord", "PublishLedger"]

PUBLISH_STATES = (
    "created",
    "validated",
    "auth_ready",
    "upload_in_progress",
    "uploaded",
    "reconciling",
    "published",
    "validation_failed",
    "auth_failed",
    "upload_failed",
    "reconciliation_pending",
    "reconciliation_failed",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS publish_attempts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    package_seal       TEXT NOT NULL,
    package_id         TEXT NOT NULL,
    run_id             TEXT NOT NULL,
    destination        TEXT NOT NULL,
    status             TEXT NOT NULL,
    youtube_video_id   TEXT,
    upload_session_url TEXT,
    bytes_sent         INTEGER NOT NULL DEFAULT 0,
    privacy_status     TEXT NOT NULL,
    title              TEXT NOT NULL,
    requested_metadata TEXT NOT NULL,
    error_code         TEXT,
    error_message      TEXT,
    attempt_count      INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    reconciled_at      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_publish_identity
    ON publish_attempts(package_seal, destination);
"""


@dataclass(frozen=True)
class PublishRecord:
    """One durable publish identity (snapshot of the ledger row)."""

    package_seal: str
    package_id: str
    run_id: str
    destination: str
    status: str
    youtube_video_id: str | None = None
    upload_session_url: str | None = None
    bytes_sent: int = 0
    privacy_status: str = "private"
    title: str = ""
    requested_metadata: dict | None = None
    error_code: str | None = None
    error_message: str | None = None
    attempt_count: int = 1
    created_at: str = ""
    updated_at: str = ""
    reconciled_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "package_seal": self.package_seal,
            "package_id": self.package_id,
            "run_id": self.run_id,
            "destination": self.destination,
            "status": self.status,
            "youtube_video_id": self.youtube_video_id,
            "bytes_sent": self.bytes_sent,
            "privacy_status": self.privacy_status,
            "title": self.title,
            "attempt_count": self.attempt_count,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reconciled_at": self.reconciled_at,
        }


class PublishLedger:
    """Small durable SQLite ledger for publishing state (§12/§14)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=30.0
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def _record(self, row: sqlite3.Row | None) -> PublishRecord | None:
        if row is None:
            return None
        data = dict(row)
        try:
            data["requested_metadata"] = json.loads(data.get("requested_metadata") or "{}")
        except (json.JSONDecodeError, TypeError):
            data["requested_metadata"] = {}
        # only the dataclass fields — extra columns (e.g. the row 'id') are
        # ledger-internal and never leak into the record snapshot
        field_names = {f for f in PublishRecord.__dataclass_fields__}
        return PublishRecord(**{k: v for k, v in data.items() if k in field_names})


    def find(self, package_seal: str, destination: str) -> PublishRecord | None:
        """The idempotency lookup: one identity per (seal, destination)."""
        row = self._connection.execute(
            "SELECT * FROM publish_attempts WHERE package_seal = ? AND destination = ?",
            (package_seal, destination),
        ).fetchone()
        return self._record(row) if row else None

    def find_by_video(self, youtube_video_id: str) -> PublishRecord | None:
        row = self._connection.execute(
            "SELECT * FROM publish_attempts WHERE youtube_video_id = ?",
            (youtube_video_id,),
        ).fetchone()
        return self._record(row) if row else None

    def find_by_run_id(self, run_id: str) -> list[PublishRecord]:
        """Read-only lookup by originating run (used by downstream
        observation subsystems to resolve the publication identity)."""
        rows = self._connection.execute(
            "SELECT * FROM publish_attempts WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        return [self._record(row) for row in rows]

    # -- mutations (one atomic transaction each) -----------------------------

    def create(
        self,
        *,
        package_seal: str,
        package_id: str,
        run_id: str,
        destination: str,
        privacy_status: str,
        title: str,
        requested_metadata: dict,
        now: str,
    ) -> PublishRecord:
        """Insert the identity row (status ``created``). Raises
        ``sqlite3.IntegrityError`` on a concurrent duplicate — the caller
        then re-reads the winner's row (the SQLite UNIQUE constraint is the
        multi-process duplicate protection, §32)."""
        with self._connection:
            self._connection.execute(
                "INSERT INTO publish_attempts"
                " (package_seal, package_id, run_id, destination, status,"
                "  privacy_status, title, requested_metadata,"
                "  created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'created', ?, ?, ?, ?, ?)",
                (package_seal, package_id, run_id, destination,
                 privacy_status, title,
                 json.dumps(requested_metadata, sort_keys=True), now, now),
            )
        return self.find(package_seal, destination)  # type: ignore[return-value]

    def update(
        self,
        package_seal: str,
        destination: str,
        *,
        now: str,
        status: str | None = None,
        youtube_video_id: str | None = None,
        upload_session_url: str | None = None,
        clear_session: bool = False,
        bytes_sent: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        clear_error: bool = False,
        attempt_count: int | None = None,
        reconciled_at: str | None = None,
    ) -> PublishRecord:
        """One atomic transition. ``None``-valued fields are unchanged;
        explicit ``clear_*`` flags null out error/session fields."""
        sets, values = [], []
        for column, value in (
            ("status", status),
            ("youtube_video_id", youtube_video_id),
            ("upload_session_url", upload_session_url),
            ("bytes_sent", bytes_sent),
            ("error_code", error_code),
            ("error_message", error_message),
            ("attempt_count", attempt_count),
            ("reconciled_at", reconciled_at),
        ):
            if value is not None:
                sets.append(f"{column} = ?")
                values.append(value)
        if clear_session:
            sets.append("upload_session_url = NULL")
            sets.append("bytes_sent = 0")
        if clear_error:
            sets.append("error_code = NULL")
            sets.append("error_message = NULL")
        sets.append("updated_at = ?")
        values.append(now)
        values.extend([package_seal, destination])
        with self._connection:
            self._connection.execute(
                f"UPDATE publish_attempts SET {', '.join(sets)}"
                " WHERE package_seal = ? AND destination = ?",
                values,
            )
        return self.find(package_seal, destination)  # type: ignore[return-value]