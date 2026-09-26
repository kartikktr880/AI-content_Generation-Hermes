"""Stage 2 — Deterministic outlier + view-velocity analytics.

Pure functions over normalized candidate metadata. No network, no clock
dependence except an injectable ``now`` — every function is
deterministic for fixed inputs and NEVER raises for edge cases; it
returns ``(value | None, truthful_reason)`` instead.

Outlier multiplier (channel baseline, not raw views):

    outlier_multiplier = target_views / channel_median_views

View velocity — an explicitly labeled TEMPORAL PROXY:

    velocity_proxy = current_views / days_since_publication

This is NOT a real-time velocity measurement (we only have publication
age + current view count); every result carries the proxy note.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

__all__ = [
    "MIN_BASELINE_SAMPLES",
    "VELOCITY_PROXY_NOTE",
    "channel_median_views",
    "outlier_multiplier",
    "velocity_proxy",
]

#: Minimum channel samples before a median baseline is trusted.
MIN_BASELINE_SAMPLES = 3

#: Truthful label attached to every velocity result.
VELOCITY_PROXY_NOTE = (
    "temporal proxy: current views / days since publication "
    "(not a real-time velocity measurement)"
)


def channel_median_views(view_counts: Sequence[int | None]) -> float | None:
    """Median of the available (non-None, non-negative) view counts.

    Returns ``None`` when fewer than :data:`MIN_BASELINE_SAMPLES` usable
    samples exist (insufficient baseline — never guessed).
    """
    usable = sorted(int(v) for v in view_counts if v is not None and v >= 0)
    if len(usable) < MIN_BASELINE_SAMPLES:
        return None
    n = len(usable)
    mid = n // 2
    if n % 2 == 1:
        return float(usable[mid])
    return (usable[mid - 1] + usable[mid]) / 2.0


def outlier_multiplier(
    view_count: int | None, channel_median: float | None
) -> tuple[float | None, str]:
    """``view_count / channel_median`` rounded to 2 decimals.

    Edge cases return ``(None, reason)`` — no exceptions, no invented
    numbers.
    """
    if view_count is None:
        return None, "missing_view_count"
    if view_count < 0:
        return None, "invalid_view_count"
    if channel_median is None:
        return None, "insufficient_baseline"
    if channel_median <= 0:
        return None, "zero_median"
    return round(view_count / channel_median, 2), "ok"


def _parse_published_at(value: str) -> datetime | None:
    """Parse the normalized ISO date (``YYYY-MM-DD``) or full ISO-8601."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def velocity_proxy(
    view_count: int | None,
    published_at: str | None,
    now: datetime | None = None,
) -> tuple[float | None, str]:
    """Views per day since publication — a labeled TEMPORAL PROXY.

    Deterministic: pass ``now`` for repeatable results (tests); the
    worker passes a fixed ``collected_at``-derived instant.
    """
    if view_count is None or view_count < 0:
        return None, "missing_or_invalid_view_count"
    if not published_at:
        return None, "missing_published_at"
    published = _parse_published_at(published_at)
    if published is None:
        return None, "unparseable_published_at"
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    age_days = (reference - published).total_seconds() / 86400.0
    if age_days <= 0:
        return None, "non_positive_age"
    return round(view_count / age_days, 1), VELOCITY_PROXY_NOTE