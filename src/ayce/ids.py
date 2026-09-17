"""Run / job / artifact identity.

IDs are timestamp-prefixed (roughly sortable) plus a short random token:
``run-20260917T101500Z-1a2b3c4d5e6f``
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _token() -> str:
    return uuid4().hex[:12]


def new_run_id() -> str:
    """Identity for one execution of a job (a full production attempt)."""
    return f"run-{_utc_stamp()}-{_token()}"


def new_job_id() -> str:
    """Identity for a logical unit of work; stable across retries/re-runs."""
    return f"job-{_utc_stamp()}-{_token()}"


def new_artifact_id() -> str:
    """Identity for a stage output artifact."""
    return f"art-{_utc_stamp()}-{_token()}"
