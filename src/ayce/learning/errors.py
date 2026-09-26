"""Stage 7 — Learning error normalization (§34).

Every learning-boundary failure is normalized into ONE structured
``learning_``-prefixed category. Two distinctions are hard invariants:

- ``rejected`` is NEVER used for ``insufficient_evidence`` — incomplete
  evidence is not a refuted hypothesis (§34).
- ``learning_metric_unavailable`` is NEVER folded into a fabricated
  value or a zero.

These failures are structured and auditable; secrets never appear here.
"""

from __future__ import annotations

__all__ = ["ERROR_CODES", "LearningError"]

ERROR_CODES = (
    "learning_invalid_input",
    "learning_insufficient_data",
    "learning_lineage_invalid",
    "learning_metric_unavailable",
    "learning_experiment_invalid",
    "learning_evaluation_failed",
    "learning_curation_failed",
    "learning_version_conflict",
)


class LearningError(RuntimeError):
    """Classified learning-boundary failure (code + message + details).

    Mirrors :class:`ayce.analytics.errors.AnalyticsError` /
    :class:`ayce.publishing.errors.PublishError`. Raised for invalid
    inputs, insufficient data, lineage problems, unavailable metrics,
    invalid experiments, evaluation/curation failures and version
    conflicts. The learning layer never converts an error into a
    fabricated result.
    """

    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        error = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = {
                k: v for k, v in self.details.items() if v is not None
            }
        return {"ok": False, "error": error}
