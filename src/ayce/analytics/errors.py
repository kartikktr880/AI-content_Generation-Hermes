"""Stage 6 — Analytics error normalization (§20).

Every external YouTube/API failure is normalized into ONE structured,
``analytics_``-prefixed category before it can touch analytics state.
The classification rules are REUSED from the Stage 5 publishing error
model (same HTTP semantics, same YouTube error ``reason`` extraction)
so both subsystems speak the same failure language — only the prefix
differs, so an analytics failure can never be confused with a
publishing failure. Secrets are never carried here.

Two analytics-specific categories have no publishing equivalent:

- ``analytics_lineage_invalid`` — the publication record is missing or
  inconsistent with the sealed package; NOTHING is attributed (§7).
- ``analytics_metric_unavailable`` — a requested metric is not offered
  by the selected official source (never fabricated, never turned into
  a zero; §10/§19/§32).
"""

from __future__ import annotations

__all__ = [
    "ERROR_CATEGORIES",
    "RETRYABLE_CATEGORIES",
    "AnalyticsError",
    "categorize_http_status",
    "categorize_exception",
]

#: Normalized failure categories (§20). The first nine mirror the Stage 5
#: publishing categories under the ``analytics_`` prefix; the last two are
#: analytics-specific lineage/metric-catalog failures.
ERROR_CATEGORIES = (
    "analytics_authentication_error",
    "analytics_authorization_error",
    "analytics_invalid_request",
    "analytics_quota_error",
    "analytics_rate_limited",
    "analytics_transient_network_error",
    "analytics_not_found",
    "analytics_server_error",
    "analytics_unknown_error",
    "analytics_lineage_invalid",
    "analytics_metric_unavailable",
)

#: Bounded-retry categories (§21). Everything else is permanent — auth,
#: permission, invalid request, lineage, unsupported metrics — and is NOT
#: retried without operator intervention.
RETRYABLE_CATEGORIES = frozenset({
    "analytics_transient_network_error",
    "analytics_server_error",
    "analytics_rate_limited",
})

_PUBLISHING_TO_ANALYTICS = {
    "authentication_error": "analytics_authentication_error",
    "authorization_error": "analytics_authorization_error",
    "invalid_request": "analytics_invalid_request",
    "quota_error": "analytics_quota_error",
    "rate_limited": "analytics_rate_limited",
    "transient_network_error": "analytics_transient_network_error",
    "not_found": "analytics_not_found",
    "server_error": "analytics_server_error",
    "unknown_error": "analytics_unknown_error",
}


def _map_category(publishing_category: str) -> str:
    return _PUBLISHING_TO_ANALYTICS.get(publishing_category, "analytics_unknown_error")


def categorize_http_status(status: int, body_text: str = "") -> str:
    """Map an HTTP status (+ optional diagnostic body) to an analytics
    category, reusing the Stage 5 publishing classifier verbatim."""
    from ..publishing.errors import categorize_http_status as _publishing

    return _map_category(_publishing(status, body_text))


def categorize_exception(exc: BaseException) -> str:
    """Map a local/transport exception to an analytics category."""
    from ..publishing.errors import categorize_exception as _publishing

    return _map_category(_publishing(exc))


class AnalyticsError(RuntimeError):
    """Classified analytics failure (code + message + optional details).

    Mirrors :class:`ayce.publishing.errors.PublishError`. Raised for
    lineage, metric-catalog, and API failures; the collector never
    converts an error into a fabricated measurement.
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
