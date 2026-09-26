"""Stage 5 — Publishing error normalization.

Every external YouTube/API failure is normalized into ONE structured
category before it can touch state. Raw HTTP responses are diagnostic
detail, never the primary state model. Secrets are never carried here.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "ERROR_CATEGORIES",
    "RETRYABLE_CATEGORIES",
    "PublishError",
    "categorize_http_status",
    "categorize_exception",
]

#: Normalized failure categories (§22).
ERROR_CATEGORIES = (
    "authentication_error",
    "authorization_error",
    "invalid_request",
    "quota_error",
    "rate_limited",
    "transient_network_error",
    "upload_session_error",
    "not_found",
    "server_error",
    "unknown_error",
)

#: Bounded-retry categories (everything else is permanent: auth, invalid
#: request, quota/authorization need operator action and are NOT retried).
RETRYABLE_CATEGORIES = frozenset({
    "transient_network_error",
    "server_error",
    "rate_limited",
    "upload_session_error",
})


class PublishError(RuntimeError):
    """Classified publishing failure (code + message + optional details)."""

    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        error = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"ok": False, "error": error}


def categorize_http_status(status: int, body_text: str = "") -> str:
    """Map an HTTP status (with optional diagnostic body) to a category.
    Body text is inspected ONLY for YouTube's error ``reason`` strings —
    it is never stored wholesale."""
    if status in (401,):
        return "authentication_error"
    if status in (403,):
        reason = _youtube_reason(body_text)
        if reason in ("quotaExceeded", "dailyLimitExceeded"):
            return "quota_error"
        if reason == "rateLimitExceeded":
            return "rate_limited"
        return "authorization_error"
    if status in (400, 422):
        return "invalid_request"
    if status in (404, 410):
        return "not_found"
    if status == 429:
        return "rate_limited"
    if 500 <= status <= 599:
        return "server_error"
    if 200 <= status < 300:
        return "unknown_error"  # a 2xx is never an error; defensive only
    return "unknown_error"


def categorize_exception(exc: BaseException) -> str:
    """Map a local/transport exception to a category."""
    from urllib.error import HTTPError, URLError

    if isinstance(exc, HTTPError):
        return categorize_http_status(exc.code, _safe_body(exc))
    if isinstance(exc, URLError):
        return "transient_network_error"
    if isinstance(exc, TimeoutError):
        return "transient_network_error"
    return "transient_network_error"


def _youtube_reason(body_text: str) -> str | None:
    """Extract YouTube's structured error ``reason`` (diagnostic only)."""
    try:
        data = json.loads(body_text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if isinstance(error, dict):
        errors = error.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return errors[0].get("reason")
        return error.get("status")
    return None


def _safe_body(exc: "HTTPError") -> str:
    try:
        return exc.read().decode("utf-8", "replace")[:2000]
    except Exception:  # noqa: BLE001 — diagnostics only
        return ""
