"""Stage 6 — Analytics observation store (worker-internal SQLite, analytics ONLY).

The SECOND (and final) SQLite component of AYCE, scoped EXCLUSIVELY to
analytics measurement history (§14). It is NOT a RunState replacement,
NOT a publish ledger extension, and never touches production or
publication state, which stay exactly where Stage 1–5 put them.

Why a small SQLite store (based on the actual repository architecture):
measurement history needs a durable uniqueness constraint on the
deterministic observation identity (§13/§22 — idempotent ingestion
across processes and restarts, §30 "process restart preserves
history"). The Stage 5 publish ledger already established the SQLite
precedent for exactly this class of problem; JSON artifact storage has
no such constraint. This store contains ONLY observations.

Two layers, kept distinct (§9)::

    raw_observations         — what the source actually reported
                               (verbatim response + exact request)
    normalized_measurements  — stable project-level metric semantics
                               derived from a raw observation (raw
                               values are never overwritten)

Uniqueness:

- ``raw_observations.observation_id`` — deterministic content hash of
  (source, youtube_video_id, scope, metrics, window_start, window_end,
  observed_at); PRIMARY KEY + an explicit identity UNIQUE index.
- ``normalized_measurements.measurement_id`` — derived from the
  observation id + metric name; INSERT OR IGNORE keeps re-derivation
  idempotent.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

__all__ = ["AnalyticsStore"]

_OBSERVATION_FIELDS = (
    "observation_id", "source", "youtube_video_id", "scope",
    "window_start", "window_end", "window_timezone", "observed_at",
    "metrics_requested", "source_request", "raw_response", "lineage",
    "collected_at",
)

_MEASUREMENT_FIELDS = (
    "measurement_id", "observation_id", "youtube_video_id", "source",
    "metric_name", "raw_value", "value", "value_type", "unit",
    "availability", "scope", "window_start", "window_end", "observed_at",
    "normalization_method", "baseline_reference", "lineage", "normalized_at",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_observations (
    observation_id    TEXT PRIMARY KEY,
    source            TEXT NOT NULL,
    youtube_video_id  TEXT NOT NULL,
    scope             TEXT NOT NULL,
    window_start      TEXT,
    window_end        TEXT,
    window_timezone   TEXT,
    observed_at       TEXT NOT NULL,
    metrics_requested TEXT NOT NULL,
    source_request    TEXT NOT NULL,
    raw_response      TEXT,
    lineage           TEXT NOT NULL,
    collected_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_analytics_observation_identity
    ON raw_observations(source, youtube_video_id, scope, metrics_requested,
                        window_start, window_end, observed_at);
CREATE TABLE IF NOT EXISTS normalized_measurements (
    measurement_id       TEXT PRIMARY KEY,
    observation_id       TEXT NOT NULL REFERENCES raw_observations(observation_id),
    youtube_video_id     TEXT NOT NULL,
    source               TEXT NOT NULL,
    metric_name          TEXT NOT NULL,
    raw_value            TEXT,
    value                REAL,
    value_type           TEXT NOT NULL,
    unit                 TEXT,
    availability         TEXT NOT NULL,
    scope                TEXT NOT NULL,
    window_start         TEXT,
    window_end           TEXT,
    observed_at          TEXT NOT NULL,
    normalization_method TEXT NOT NULL DEFAULT 'identity',
    baseline_reference   TEXT,
    lineage              TEXT NOT NULL,
    normalized_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_measurements_video
    ON normalized_measurements(youtube_video_id);
"""


def _measurement_id(observation_id: str, metric_name: str) -> str:
    digest = hashlib.sha256(
        f"{observation_id}|{metric_name}".encode("utf-8")).hexdigest()[:24]
    return "msr-" + digest
class AnalyticsStore:
    """Small durable SQLite store for analytics observations ONLY."""

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

    # -- reads ----------------------------------------------------------------

    @staticmethod
    def _loads(text: Any, default: Any) -> Any:
        if text is None:
            return default
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return default

    def _observation(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        data = dict(row)
        data["metrics_requested"] = self._loads(data.get("metrics_requested"), [])
        data["source_request"] = self._loads(data.get("source_request"), {})
        data["lineage"] = self._loads(data.get("lineage"), {})
        return data

    def _measurement(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        data = dict(row)
        data["lineage"] = self._loads(data.get("lineage"), {})
        return data

    def get_observation(self, observation_id: str) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM raw_observations WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()
        return self._observation(row)

    def list_observations(self, youtube_video_id: str | None = None) -> list[dict]:
        """Durable observation history (deterministic order); optionally
        filtered to one video. Never synthesized — only stored rows."""
        if youtube_video_id is not None:
            rows = self._connection.execute(
                "SELECT * FROM raw_observations WHERE youtube_video_id = ?"
                " ORDER BY observed_at, observation_id",
                (youtube_video_id,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM raw_observations ORDER BY observed_at, observation_id"
            ).fetchall()
        return [self._observation(row) for row in rows]

    def measurements_for_video(self, youtube_video_id: str) -> list[dict]:
        rows = self._connection.execute(
            "SELECT * FROM normalized_measurements WHERE youtube_video_id = ?"
            " ORDER BY observed_at, source, metric_name",
            (youtube_video_id,),
        ).fetchall()
        return [self._measurement(row) for row in rows]

    def measurements_for_observation(self, observation_id: str) -> list[dict]:
        rows = self._connection.execute(
            "SELECT * FROM normalized_measurements WHERE observation_id = ?"
            " ORDER BY metric_name",
            (observation_id,),
        ).fetchall()
        return [self._measurement(row) for row in rows]

    # -- writes (raw observations are IMMUTABLE once persisted, §12) -----------

    def insert_observation(
        self,
        *,
        observation_id: str,
        source: str,
        youtube_video_id: str,
        scope: str,
        observed_at: str,
        metrics_requested: list,
        source_request: dict,
        raw_response: str | None,
        lineage: dict,
        collected_at: str,
        window_start: str | None = None,
        window_end: str | None = None,
        window_timezone: str | None = None,
    ) -> bool:
        """Persist one raw observation. Returns False (and changes
        NOTHING) when the deterministic identity already exists — the
        idempotent-ingestion guarantee (§22)."""
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO raw_observations ("
                    + ", ".join(_OBSERVATION_FIELDS)
                    + ") VALUES ("
                    + ", ".join("?" for _ in _OBSERVATION_FIELDS)
                    + ")",
                    (
                        observation_id, source, youtube_video_id, scope,
                        window_start, window_end, window_timezone, observed_at,
                        json.dumps(list(metrics_requested), sort_keys=True),
                        json.dumps(source_request, sort_keys=True),
                        raw_response,
                        json.dumps(lineage, sort_keys=True),
                        collected_at,
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def insert_measurements(
        self,
        *,
        observation_id: str,
        source: str,
        youtube_video_id: str,
        scope: str,
        observed_at: str,
        lineage: dict,
        measurements: list[dict],
        normalized_at: str,
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> int:
        """Persist the normalized measurements derived from ONE raw
        observation. Re-derivation is idempotent (INSERT OR IGNORE on the
        measurement identity). Returns the number of NEW rows."""
        inserted = 0
        with self._connection:
            for metric in measurements:
                mid = _measurement_id(observation_id, metric["metric_name"])
                raw_value = metric.get("raw_value")
                cursor = self._connection.execute(
                    "INSERT OR IGNORE INTO normalized_measurements ("
                    + ", ".join(_MEASUREMENT_FIELDS)
                    + ") VALUES ("
                    + ", ".join("?" for _ in _MEASUREMENT_FIELDS)
                    + ")",
                    (
                        mid, observation_id, youtube_video_id, source,
                        metric["metric_name"],
                        None if raw_value is None else json.dumps(raw_value),
                        metric.get("value"),
                        metric["value_type"],
                        metric.get("unit"),
                        metric["availability"],
                        scope, window_start, window_end, observed_at,
                        metric.get("normalization_method", "identity"),
                        metric.get("baseline_reference"),
                        json.dumps(lineage, sort_keys=True),
                        normalized_at,
                    ),
                )
                inserted += cursor.rowcount if cursor.rowcount > 0 else 0
        return inserted

