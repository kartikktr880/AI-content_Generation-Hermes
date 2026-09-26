"""Stage 8 — Policy store (dedicated SQLite, policy domain ONLY).

A small DEDICATED store for the ProductionPolicy domain (§18): it does
NOT touch RunState, the publish ledger, the analytics observation store,
or the learning knowledge store (which remain authoritative and
immutable). Same SQLite conventions as the Stage 5 ledger / Stage 6
analytics / Stage 7 learning stores: WAL journal, busy timeout,
explicit UNIQUE constraints, one atomic transaction per mutation.

Tables (durable identities, full history, INSERT-mostly)::

    policy_candidates    — explicit operator-authored candidates
                           (content-addressed; deterministic id →
                           duplicate creation is impossible)
    candidate_approvals  — the EXPLICIT operator approval record
                           (one per candidate; idempotent by PK)
    policies             — IMMUTABLE versioned ProductionPolicy records
                           (policy_id, policy_version); content is
                           content-addressed (content_hash UNIQUE) and
                           never rewritten — a change is a NEW version
    policy_activations   — activation/rollback audit trail

Concurrency: writers rely on SQLite transactions + UNIQUE constraints —
including a partial UNIQUE index that enforces AT MOST ONE active policy
per scope at the database level — never on in-memory locks alone.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .errors import PolicyError

__all__ = ["PolicyStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_candidates (
    candidate_id   TEXT PRIMARY KEY,
    source_knowledge_refs TEXT NOT NULL,
    scope          TEXT NOT NULL,
    proposed_rules TEXT NOT NULL,
    rationale      TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    policy_id      TEXT,
    policy_version INTEGER
);
CREATE INDEX IF NOT EXISTS ix_candidates_scope ON policy_candidates(scope);

CREATE TABLE IF NOT EXISTS candidate_approvals (
    candidate_id TEXT PRIMARY KEY,
    decision     TEXT NOT NULL,
    approved_by  TEXT NOT NULL,
    approved_at  TEXT NOT NULL,
    candidate_status_before TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policies (
    policy_id      TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    scope          TEXT NOT NULL,
    rules          TEXT NOT NULL,
    source_knowledge_refs TEXT NOT NULL,
    rationale      TEXT NOT NULL,
    candidate_id   TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    status         TEXT NOT NULL,
    previous_policy_id TEXT,
    previous_policy_version INTEGER,
    created_at     TEXT NOT NULL,
    activated_at   TEXT,
    PRIMARY KEY (policy_id, policy_version)
);
-- identical content must never create a second durable policy identity
CREATE UNIQUE INDEX IF NOT EXISTS ux_policy_content ON policies(content_hash);
-- version numbers are unique per scope line (the concurrency arbiter)
CREATE UNIQUE INDEX IF NOT EXISTS ux_policy_scope_version
    ON policies(scope, policy_version);
-- AT MOST ONE active policy per scope — enforced by the DATABASE
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_active_per_scope
    ON policies(scope) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS ix_policies_scope ON policies(scope);

CREATE TABLE IF NOT EXISTS policy_activations (
    activation_id TEXT PRIMARY KEY,
    policy_id     TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    action        TEXT NOT NULL,
    previous_policy_id TEXT,
    previous_policy_version INTEGER,
    performed_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_activations_policy
    ON policy_activations(policy_id, policy_version);
"""


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


class PolicyStore:
    """Small durable SQLite store for ProductionPolicy state ONLY
    (candidates, approvals, versioned immutable policies, activation
    audit). INSERT-mostly; every mutation is one atomic transaction
    guarded by explicit UNIQUE constraints and guarded status
    transitions; policy content columns are NEVER updated."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._connection = sqlite3.connect(
                str(self.path), check_same_thread=False, timeout=30.0
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA busy_timeout=30000")
            self._connection.executescript(_SCHEMA)
            self._connection.commit()
        except sqlite3.Error as exc:
            raise PolicyError("policy_storage_error",
                              f"could not open the policy store: {exc}") from exc

    def close(self) -> None:
        self._connection.close()

    # -- JSON helpers -----------------------------------------------------

    @staticmethod
    def _loads(text: Any, default: Any) -> Any:
        if text is None:
            return default
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return default

    @staticmethod
    def _dumps(payload: Any) -> str:
        return _canonical(payload)

    # -- candidates ---------------------------------------------------------

    def insert_candidate(self, *, row: dict) -> bool:
        """Insert one candidate (deterministic identity → idempotent).
        Returns False when the identical candidate already exists."""
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO policy_candidates ("
                    "candidate_id, source_knowledge_refs, scope,"
                    " proposed_rules, rationale, content_hash, status,"
                    " created_at, updated_at, policy_id, policy_version)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["candidate_id"],
                        self._dumps(row["source_knowledge_refs"]),
                        row["scope"], self._dumps(row["proposed_rules"]),
                        row["rationale"], row["content_hash"], row["status"],
                        row["created_at"], row["updated_at"],
                        row.get("policy_id"), row.get("policy_version"),
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    @staticmethod
    def _candidate_row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        data = dict(row)
        data["source_knowledge_refs"] = json.loads(
            data["source_knowledge_refs"])
        data["proposed_rules"] = json.loads(data["proposed_rules"])
        return data

    def get_candidate(self, candidate_id: str) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM policy_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        return self._candidate_row(row)

    def list_candidates(self) -> list[dict]:
        rows = self._connection.execute(
            "SELECT * FROM policy_candidates ORDER BY created_at,"
            " candidate_id"
        ).fetchall()
        return [self._candidate_row(row) for row in rows]

    def transition_candidate(self, candidate_id: str, *, from_status: str,
                             to_status: str, now: str,
                             policy_id: str | None = None,
                             policy_version: int | None = None) -> bool:
        """One guarded atomic transition: succeeds ONLY when the current
        status still matches ``from_status`` (optimistic concurrency; a
        concurrent transition makes this return False truthfully). The
        promotion transition binds the durable policy identity in the
        SAME statement."""
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE policy_candidates SET status = ?, updated_at = ?,"
                " policy_id = COALESCE(?, policy_id),"
                " policy_version = COALESCE(?, policy_version)"
                " WHERE candidate_id = ? AND status = ?",
                (to_status, now, policy_id, policy_version,
                 candidate_id, from_status),
            )
        return cursor.rowcount > 0

    # -- approvals ------------------------------------------------------------

    def record_approval(self, *, candidate_id: str, decision: str,
                        approved_by: str, approved_at: str,
                        candidate_status_before: str) -> bool:
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO candidate_approvals (candidate_id,"
                    " decision, approved_by, approved_at,"
                    " candidate_status_before) VALUES (?, ?, ?, ?, ?)",
                    (candidate_id, decision, approved_by, approved_at,
                     candidate_status_before),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def get_approval(self, candidate_id: str) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM candidate_approvals WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    # -- policies ---------------------------------------------------------------

    def insert_policy(self, *, row: dict) -> bool:
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO policies (policy_id, policy_version,"
                    " scope, rules, source_knowledge_refs, rationale,"
                    " candidate_id, content_hash, status,"
                    " previous_policy_id, previous_policy_version,"
                    " created_at, activated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["policy_id"], row["policy_version"],
                        row["scope"], self._dumps(row["rules"]),
                        self._dumps(row["source_knowledge_refs"]),
                        row["rationale"], row["candidate_id"],
                        row["content_hash"], row["status"],
                        row.get("previous_policy_id"),
                        row.get("previous_policy_version"),
                        row["created_at"], row.get("activated_at"),
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    @staticmethod
    def _policy_row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        data = dict(row)
        data["rules"] = json.loads(data["rules"])
        data["source_knowledge_refs"] = json.loads(
            data["source_knowledge_refs"])
        return data

    def get_policy(self, policy_id: str,
                   policy_version: int | None = None) -> dict | None:
        if policy_version is None:
            row = self._connection.execute(
                "SELECT * FROM policies WHERE policy_id = ?"
                " ORDER BY policy_version DESC LIMIT 1",
                (policy_id,),
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT * FROM policies WHERE policy_id = ?"
                " AND policy_version = ?",
                (policy_id, policy_version),
            ).fetchone()
        return self._policy_row(row)

    def get_policy_by_content(self, content_hash: str) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM policies WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
        return self._policy_row(row)

    def next_policy_version(self, scope: str) -> int:
        row = self._connection.execute(
            "SELECT MAX(policy_version) AS v FROM policies WHERE scope = ?",
            (scope,),
        ).fetchone()
        return (row["v"] or 0) + 1 if row else 1

    def latest_policy(self, scope: str) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM policies WHERE scope = ?"
            " ORDER BY policy_version DESC LIMIT 1",
            (scope,),
        ).fetchone()
        return self._policy_row(row)

    def active_policy(self, scope: str) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM policies WHERE scope = ? AND status = 'active'",
            (scope,),
        ).fetchone()
        return self._policy_row(row)

    def transition_policy(self, policy_id: str, policy_version: int, *,
                          from_statuses: tuple[str, ...], to_status: str,
                          activated_at: str | None = None) -> bool:
        """One guarded atomic status transition (content columns are
        NEVER touched). Succeeds ONLY when the current status still
        matches one of ``from_statuses``."""
        placeholders = ",".join("?" for _ in from_statuses)
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE policies SET status = ?,"
                " activated_at = COALESCE(?, activated_at)"
                " WHERE policy_id = ? AND policy_version = ?"
                f" AND status IN ({placeholders})",
                (to_status, activated_at, policy_id, policy_version,
                 *from_statuses),
            )
        return cursor.rowcount > 0

    def supersede_active(self, scope: str, *, now: str,
                         except_policy_id: str | None = None,
                         except_policy_version: int | None = None) -> int:
        """Mark every currently ACTIVE policy in ``scope`` (except the
        given one) as superseded. History is RETAINED — nothing is
        deleted or rewritten."""
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE policies SET status = 'superseded'"
                " WHERE scope = ? AND status = 'active'"
                " AND NOT (policy_id = ? AND policy_version = ?)",
                (scope, except_policy_id, except_policy_version),
            )
        return cursor.rowcount

    def activate_swap(self, *, policy_id: str, policy_version: int,
                      scope: str, now: str) -> bool:
        """ONE atomic activation swap (concurrency-safe by the SQLite
        transaction, not by in-memory locking): supersede the currently
        active policy of the scope AND activate the target in a single
        transaction. The partial UNIQUE index remains the final arbiter.
        Returns False when the target could not be activated (wrong
        status) — nothing else is modified."""
        with self._connection:
            self._connection.execute(
                "UPDATE policies SET status = 'superseded'"
                " WHERE scope = ? AND status = 'active'"
                " AND NOT (policy_id = ? AND policy_version = ?)",
                (scope, policy_id, policy_version),
            )
            cursor = self._connection.execute(
                "UPDATE policies SET status = 'active',"
                " activated_at = ?"
                " WHERE policy_id = ? AND policy_version = ?"
                " AND status IN ('approved', 'superseded')",
                (now, policy_id, policy_version),
            )
        return cursor.rowcount > 0

    # -- activation audit ---------------------------------------------------------

    def record_activation(self, *, row: dict) -> bool:
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO policy_activations (activation_id,"
                    " policy_id, policy_version, action,"
                    " previous_policy_id, previous_policy_version,"
                    " performed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["activation_id"], row["policy_id"],
                        row["policy_version"], row["action"],
                        row.get("previous_policy_id"),
                        row.get("previous_policy_version"),
                        row["performed_at"],
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def list_activations(self, policy_id: str | None = None) -> list[dict]:
        if policy_id is None:
            rows = self._connection.execute(
                "SELECT * FROM policy_activations ORDER BY performed_at,"
                " activation_id"
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM policy_activations WHERE policy_id = ?"
                " ORDER BY performed_at, activation_id",
                (policy_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_policies(self, scope: str | None = None) -> list[dict]:
        if scope is None:
            rows = self._connection.execute(
                "SELECT * FROM policies ORDER BY scope, policy_version"
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM policies WHERE scope = ?"
                " ORDER BY policy_version",
                (scope,),
            ).fetchall()
        return [self._policy_row(row) for row in rows]

    def summary(self) -> dict:
        """Compact whole-store snapshot (operator show / tests)."""
        return {
            "candidates": len(self.list_candidates()),
            "approvals": len(self._connection.execute(
                "SELECT candidate_id FROM candidate_approvals"
            ).fetchall()),
            "policies": len(self.list_policies()),
            "activations": len(self.list_activations()),
            "scopes": sorted({p["scope"] for p in self.list_policies()}),
        }


