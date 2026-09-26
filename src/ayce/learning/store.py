"""Stage 7 — Learning store (worker-internal SQLite, learning ONLY).

A small DEDICATED store for the learning layer (§30): it does NOT touch
RunState, the publish ledger, sealed packages or the Stage 6 analytics
observation store (which remains authoritative and immutable — §6).
Same SQLite conventions as the Stage 5 ledger / Stage 6 analytics
store: WAL journal, busy timeout, explicit UNIQUE constraints, one
atomic transaction per mutation (§33).

Tables (durable identities, full history, INSERT-mostly)::

    derived_metrics       — provenance-carrying derived metric values
    candidates            — explicit learning candidates (hypothesis +
                            evidence + counterexamples, kept separate)
    experiments           — immutable content-addressed definitions
    assignments           — deterministic unit → variant assignments
    evaluations           — evaluation records (never overwritten; a new
                            dataset produces a NEW evaluation record)
    validated_knowledge   — versioned curated knowledge
                            (knowledge_id, knowledge_version) — history
                            is immutable, superseded versions retained

Concurrency: writers rely on SQLite transactions + UNIQUE constraints
(never on in-memory locks alone); a lost insert race is reported
truthfully and re-read, mirroring the Stage 5 ledger pattern.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

__all__ = ["LearningStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS derived_metrics (
    derived_metric_id  TEXT PRIMARY KEY,
    formula            TEXT NOT NULL,
    formula_version    TEXT NOT NULL,
    youtube_video_id   TEXT NOT NULL,
    observed_at        TEXT NOT NULL,
    window_start       TEXT,
    window_end         TEXT,
    value              REAL,
    availability       TEXT NOT NULL,
    note               TEXT,
    source_observation_ids TEXT NOT NULL,
    lineage            TEXT NOT NULL,
    created_at         TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_derived_identity
    ON derived_metrics(formula, formula_version, youtube_video_id,
                       observed_at, window_start, window_end);
CREATE INDEX IF NOT EXISTS ix_derived_video ON derived_metrics(youtube_video_id);

CREATE TABLE IF NOT EXISTS candidates (
    candidate_id   TEXT PRIMARY KEY,
    hypothesis     TEXT NOT NULL,
    scope          TEXT NOT NULL,
    evidence       TEXT NOT NULL,
    counterexamples TEXT NOT NULL,
    lineage        TEXT NOT NULL,
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id  TEXT PRIMARY KEY,
    candidate_id   TEXT NOT NULL,
    definition     TEXT NOT NULL,
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    experiment_id    TEXT NOT NULL,
    unit_id          TEXT NOT NULL,
    variant          TEXT NOT NULL,
    assignment_method TEXT NOT NULL,
    assignment_seed  TEXT NOT NULL,
    assigned_at      TEXT NOT NULL,
    PRIMARY KEY (experiment_id, unit_id)
);

CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id   TEXT PRIMARY KEY,
    experiment_id   TEXT NOT NULL,
    status          TEXT NOT NULL,
    control_n       INTEGER,
    treatment_n     INTEGER,
    control_mean    REAL,
    treatment_mean  REAL,
    effect          REAL,
    standard_error  REAL,
    p_value         REAL,
    criterion       TEXT NOT NULL,
    observed        TEXT NOT NULL,
    holdout         TEXT,
    metric_selection TEXT NOT NULL,
    dataset_fingerprint TEXT NOT NULL,
    details         TEXT NOT NULL,
    evaluated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_evaluations_experiment
    ON evaluations(experiment_id);

CREATE TABLE IF NOT EXISTS validated_knowledge (
    knowledge_id      TEXT NOT NULL,
    knowledge_version INTEGER NOT NULL,
    candidate_id      TEXT NOT NULL,
    experiment_id     TEXT NOT NULL,
    evaluation_id     TEXT NOT NULL,
    statement         TEXT NOT NULL,
    scope             TEXT NOT NULL,
    evidence          TEXT NOT NULL,
    validation        TEXT NOT NULL,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    PRIMARY KEY (knowledge_id, knowledge_version)
);
CREATE INDEX IF NOT EXISTS ix_knowledge_scope ON validated_knowledge(scope);
"""


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
class LearningStore:
    """Small durable SQLite store for learning state ONLY (derived
    metrics, candidates, experiments, assignments, evaluations,
    validated knowledge). Read-mostly; INSERT-mostly; every mutation is
    one atomic transaction guarded by explicit UNIQUE constraints."""

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

    # -- JSON helpers -----------------------------------------------------------

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

    # -- derived metrics ----------------------------------------------------------

    def insert_derived(self, *, row: dict) -> bool:
        """Insert one derived metric (deterministic identity → idempotent).
        Returns False when the identical derived metric already exists."""
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO derived_metrics ("
                    "derived_metric_id, formula, formula_version,"
                    " youtube_video_id, observed_at, window_start,"
                    " window_end, value, availability, note,"
                    " source_observation_ids, lineage, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["derived_metric_id"], row["formula"],
                        row["formula_version"], row["youtube_video_id"],
                        row["observed_at"], row.get("window_start"),
                        row.get("window_end"), row.get("value"),
                        row["availability"], row.get("note"),
                        self._dumps(row["source_observation_ids"]),
                        self._dumps(row["lineage"]), row["created_at"],
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    @staticmethod
    def _derived_row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        data = dict(row)
        data["source_observation_ids"] = json.loads(data["source_observation_ids"])
        data["lineage"] = json.loads(data["lineage"])
        return data

    def get_derived(self, derived_metric_id: str) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM derived_metrics WHERE derived_metric_id = ?",
            (derived_metric_id,),
        ).fetchone()
        return self._derived_row(row)

    def derived_for_video(self, youtube_video_id: str,
                          formula: str | None = None) -> list[dict]:
        if formula is None:
            rows = self._connection.execute(
                "SELECT * FROM derived_metrics WHERE youtube_video_id = ?"
                " ORDER BY observed_at, formula",
                (youtube_video_id,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM derived_metrics WHERE youtube_video_id = ?"
                " AND formula = ? ORDER BY observed_at",
                (youtube_video_id, formula),
            ).fetchall()
        return [self._derived_row(row) for row in rows]

    def latest_present_derived(self, formula: str,
                               youtube_video_id: str) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM derived_metrics WHERE formula = ?"
            " AND youtube_video_id = ? AND availability = 'present'"
            " ORDER BY observed_at DESC LIMIT 1",
            (formula, youtube_video_id),
        ).fetchone()
        return self._derived_row(row)

    def list_derived(self, formula: str | None = None) -> list[dict]:
        if formula is None:
            rows = self._connection.execute(
                "SELECT * FROM derived_metrics ORDER BY observed_at,"
                " youtube_video_id, formula"
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM derived_metrics WHERE formula = ?"
                " ORDER BY observed_at, youtube_video_id",
                (formula,),
            ).fetchall()
        return [self._derived_row(row) for row in rows]

    # -- candidates -------------------------------------------------------------

    @staticmethod
    def _candidate_row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        data = dict(row)
        data["evidence"] = json.loads(data["evidence"])
        data["counterexamples"] = json.loads(data["counterexamples"])
        data["lineage"] = json.loads(data["lineage"])
        return data

    def insert_candidate(self, *, row: dict) -> bool:
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO candidates (candidate_id, hypothesis,"
                    " scope, evidence, counterexamples, lineage, status,"
                    " created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["candidate_id"], row["hypothesis"], row["scope"],
                        self._dumps(row["evidence"]),
                        self._dumps(row["counterexamples"]),
                        self._dumps(row["lineage"]), row["status"],
                        row["created_at"], row["updated_at"],
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def get_candidate(self, candidate_id: str) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        return self._candidate_row(row)

    def update_candidate_status(self, candidate_id: str, status: str, *,
                                now: str) -> bool:
        """One atomic status transition (candidate status is derived from
        evaluations, which are themselves idempotent — the last write
        reflects the truthful latest evaluation)."""
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE candidates SET status = ?, updated_at = ?"
                " WHERE candidate_id = ?",
                (status, now, candidate_id),
            )
        return cursor.rowcount > 0

    def list_candidates(self) -> list[dict]:
        rows = self._connection.execute(
            "SELECT * FROM candidates ORDER BY created_at, candidate_id"
        ).fetchall()
        return [self._candidate_row(row) for row in rows]

    # -- experiments --------------------------------------------------------------

    @staticmethod
    def _experiment_row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        data = dict(row)
        data["definition"] = json.loads(data["definition"])
        return data

    def insert_experiment(self, *, row: dict) -> bool:
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO experiments (experiment_id, candidate_id,"
                    " definition, status, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        row["experiment_id"], row["candidate_id"],
                        self._dumps(row["definition"]), row["status"],
                        row["created_at"], row["updated_at"],
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def get_experiment(self, experiment_id: str) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM experiments WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()
        return self._experiment_row(row)

    def list_experiments(self) -> list[dict]:
        rows = self._connection.execute(
            "SELECT * FROM experiments ORDER BY created_at, experiment_id"
        ).fetchall()
        return [self._experiment_row(row) for row in rows]

    def transition_experiment(self, experiment_id: str, *, from_status: str,
                              to_status: str, now: str) -> bool:
        """One guarded atomic transition: succeeds ONLY when the current
        status still matches ``from_status`` (optimistic concurrency;
        a concurrent transition makes this return False truthfully)."""
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE experiments SET status = ?, updated_at = ?"
                " WHERE experiment_id = ? AND status = ?",
                (to_status, now, experiment_id, from_status),
            )
        return cursor.rowcount > 0

    # -- assignments ---------------------------------------------------------------

    def insert_assignment(self, *, row: dict) -> bool:
        """Insert one unit assignment. Returns False when the unit is
        already assigned to this experiment (no silent reassignment,
        §14) — the caller reads back and compares the existing variant."""
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO assignments (experiment_id, unit_id,"
                    " variant, assignment_method, assignment_seed,"
                    " assigned_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        row["experiment_id"], row["unit_id"], row["variant"],
                        row["assignment_method"], row["assignment_seed"],
                        row["assigned_at"],
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def get_assignment(self, experiment_id: str, unit_id: str) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM assignments WHERE experiment_id = ?"
            " AND unit_id = ?",
            (experiment_id, unit_id),
        ).fetchone()
        return dict(row) if row else None

    def assignments_for_experiment(self, experiment_id: str) -> list[dict]:
        rows = self._connection.execute(
            "SELECT * FROM assignments WHERE experiment_id = ?"
            " ORDER BY unit_id",
            (experiment_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- evaluations -----------------------------------------------------------------

    @staticmethod
    def _evaluation_row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        data = dict(row)
        data["observed"] = json.loads(data["observed"])
        data["holdout"] = (json.loads(data["holdout"])
                           if data.get("holdout") else None)
        data["metric_selection"] = json.loads(data["metric_selection"])
        data["details"] = json.loads(data["details"])
        return data

    def insert_evaluation(self, *, row: dict) -> bool:
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO evaluations (evaluation_id, experiment_id,"
                    " status, control_n, treatment_n, control_mean,"
                    " treatment_mean, effect, standard_error, p_value,"
                    " criterion, observed, holdout, metric_selection,"
                    " dataset_fingerprint, details, evaluated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["evaluation_id"], row["experiment_id"],
                        row["status"], row.get("control_n"),
                        row.get("treatment_n"), row.get("control_mean"),
                        row.get("treatment_mean"), row.get("effect"),
                        row.get("standard_error"), row.get("p_value"),
                        self._dumps(row["criterion"]),
                        self._dumps(row["observed"]),
                        (self._dumps(row["holdout"])
                         if row.get("holdout") is not None else None),
                        self._dumps(row["metric_selection"]),
                        row["dataset_fingerprint"],
                        self._dumps(row["details"]), row["evaluated_at"],
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def get_evaluation(self, evaluation_id: str) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM evaluations WHERE evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()
        return self._evaluation_row(row)

    # -- validated knowledge ----------------------------------------------------------

    @staticmethod
    def _knowledge_row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        data = dict(row)
        data["evidence"] = json.loads(data["evidence"])
        data["validation"] = json.loads(data["validation"])
        return data

    def next_knowledge_version(self, knowledge_id: str) -> int:
        row = self._connection.execute(
            "SELECT MAX(knowledge_version) AS v FROM validated_knowledge"
            " WHERE knowledge_id = ?",
            (knowledge_id,),
        ).fetchone()
        return (row["v"] or 0) + 1 if row else 1

    def insert_knowledge(self, *, row: dict) -> bool:
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO validated_knowledge (knowledge_id,"
                    " knowledge_version, candidate_id, experiment_id,"
                    " evaluation_id, statement, scope, evidence,"
                    " validation, status, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["knowledge_id"], row["knowledge_version"],
                        row["candidate_id"], row["experiment_id"],
                        row["evaluation_id"], row["statement"],
                        row["scope"], self._dumps(row["evidence"]),
                        self._dumps(row["validation"]), row["status"],
                        row["created_at"],
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def supersede_knowledge(self, knowledge_id: str, *, below_version: int) -> int:
        """Mark older versions of one knowledge statement as superseded
        (history is RETAINED — nothing is deleted or rewritten)."""
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE validated_knowledge SET status = 'superseded'"
                " WHERE knowledge_id = ? AND knowledge_version < ?"
                " AND status = 'active'",
                (knowledge_id, below_version),
            )
        return cursor.rowcount

    def get_knowledge(self, knowledge_id: str,
                      version: int | None = None) -> dict | None:
        if version is None:
            row = self._connection.execute(
                "SELECT * FROM validated_knowledge WHERE knowledge_id = ?"
                " ORDER BY knowledge_version DESC LIMIT 1",
                (knowledge_id,),
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT * FROM validated_knowledge WHERE knowledge_id = ?"
                " AND knowledge_version = ?",
                (knowledge_id, version),
            ).fetchone()
        return self._knowledge_row(row)

    def knowledge_history(self, knowledge_id: str) -> list[dict]:
        rows = self._connection.execute(
            "SELECT * FROM validated_knowledge WHERE knowledge_id = ?"
            " ORDER BY knowledge_version",
            (knowledge_id,),
        ).fetchall()
        return [self._knowledge_row(row) for row in rows]

    def query_knowledge(self, scope: str | None = None,
                        status: str | None = "active") -> list[dict]:
        """Deterministic filtered lookup (NO semantic search, §27)."""
        clauses, params = [], []
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._connection.execute(
            "SELECT * FROM validated_knowledge" + where
            + " ORDER BY scope, knowledge_id, knowledge_version",
            tuple(params),
        ).fetchall()
        return [self._knowledge_row(row) for row in rows]

    def list_all(self) -> dict:
        """Compact whole-store snapshot (operator show / tests)."""
        return {
            "derived_metrics": len(self.list_derived()),
            "candidates": len(self.list_candidates()),
            "experiments": len(self.list_experiments()),
            "evaluations": len(self.list_evaluations()),
            "knowledge": len(self.query_knowledge(status=None)),
        }

    def latest_evaluation(self, experiment_id: str) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM evaluations WHERE experiment_id = ?"
            " ORDER BY evaluated_at DESC, evaluation_id DESC LIMIT 1",
            (experiment_id,),
        ).fetchone()
        return self._evaluation_row(row)

    def list_evaluations(self, experiment_id: str | None = None) -> list[dict]:
        if experiment_id is None:
            rows = self._connection.execute(
                "SELECT * FROM evaluations ORDER BY evaluated_at,"
                " evaluation_id"
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM evaluations WHERE experiment_id = ?"
                " ORDER BY evaluated_at, evaluation_id",
                (experiment_id,),
            ).fetchall()
        return [self._evaluation_row(row) for row in rows]

