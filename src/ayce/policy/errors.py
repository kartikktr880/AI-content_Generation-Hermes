"""Stage 8 — Policy error normalization (§20).

Every policy-boundary failure is normalized into ONE structured
``policy_``-prefixed category. Raw SQLite exceptions are NEVER exposed
as the public contract (``policy_storage_error`` wraps them).

Distinctions that are hard invariants:

- ``policy_approval_required`` — the explicit operator approval boundary
  was not crossed; promotion fails CLOSED.
- ``policy_source_not_validated`` — the referenced knowledge is not an
  ACTIVE validated record (rejected/insufficient/superseded evidence can
  never become policy).
- ``policy_unsupported`` — the validated evidence cannot legitimately
  support the requested policy semantics (no invented causal claims).
"""

from __future__ import annotations

__all__ = ["ERROR_CODES", "PolicyError"]

ERROR_CODES = (
    "policy_invalid_candidate",
    "policy_source_not_validated",
    "policy_source_missing",
    "policy_scope_invalid",
    "policy_rule_invalid",
    "policy_unsupported",
    "policy_approval_required",
    "policy_already_approved",
    "policy_conflict",
    "policy_activation_conflict",
    "policy_not_active",
    "policy_version_conflict",
    "policy_rollback_invalid",
    "policy_storage_error",
    # Stage 9 — policy EXECUTION-context failures (read-only consumption).
    # None of these are writable-path errors: the director can only ever
    # fail while READING or interpreting canonical policy state.
    "policy_context_invalid",
    "policy_context_conflict",
    "policy_scope_unresolved",
    "policy_read_failed",
    "policy_decision_invalid",
    "policy_consumption_failed",
    # Stage 11 — cross-run lineage projection (derived, read-only).
    "policy_lineage_invalid",
    # Stage 12 — policy effectiveness evidence (derived, read-only).
    "policy_effectiveness_invalid",
    # Stage 13 — experiment intake boundary (derived, read-only; the
    # only mutation is explicit approval THROUGH the existing Stage 7
    # learning-store owner).
    "policy_intake_invalid",
    # Stage 14 — controlled experiment composition boundary (derived,
    # read-only definition proposal; approval creates the Stage 7
    # experiment ONLY through the existing Stage 7 owner).
    "policy_composition_invalid",
)


class PolicyError(RuntimeError):
    """Classified policy-boundary failure (code + message + details).

    Mirrors :class:`ayce.learning.errors.LearningError` /
    :class:`ayce.analytics.errors.AnalyticsError` /
    :class:`ayce.publishing.errors.PublishError`. Raised for invalid
    candidates, unvalidated sources, scope/rule violations, unsupported
    semantics, approval/activation/rollback boundary violations and
    storage failures. The policy layer never converts an error into a
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
