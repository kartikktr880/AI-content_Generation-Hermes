"""Stage 2 analytics tests — median baseline, outlier multiplier,
velocity proxy, and edge cases (all deterministic)."""

from datetime import datetime, timezone

from ayce.research.analytics import (
    VELOCITY_PROXY_NOTE,
    channel_median_views,
    outlier_multiplier,
    velocity_proxy,
)

_NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


def test_channel_median_baseline():
    assert channel_median_views([8000, 10000, 12000, 3000]) == 9000.0
    assert channel_median_views([10000, 20000, 30000]) == 20000.0


def test_channel_median_ignores_missing_and_negative():
    assert channel_median_views([None, 8000, -5, 10000, 12000]) == 10000.0


def test_channel_median_insufficient_samples():
    assert channel_median_views([]) is None
    assert channel_median_views([100]) is None
    assert channel_median_views([100, None, 200]) is None


def test_outlier_multiplier_acceptance_case():
    # channel median = 10,000; target views = 75,000 → 7.50
    mult, reason = outlier_multiplier(75000, 10000.0)
    assert mult == 7.50
    assert reason == "ok"


def test_outlier_multiplier_edges():
    assert outlier_multiplier(None, 10000.0) == (None, "missing_view_count")
    assert outlier_multiplier(75000, None) == (None, "insufficient_baseline")
    assert outlier_multiplier(75000, 0.0) == (None, "zero_median")
    assert outlier_multiplier(-1, 100.0) == (None, "invalid_view_count")


def test_velocity_proxy_deterministic():
    # 9,000 views over 7 days (2026-09-13 → 2026-09-20) = 1285.7/day
    value, note = velocity_proxy(9000, "2026-09-13", now=_NOW)
    assert value == 1285.7
    assert note == VELOCITY_PROXY_NOTE
    assert "temporal proxy" in note
    assert "not a real-time velocity measurement" in note


def test_velocity_proxy_edges():
    assert velocity_proxy(None, "2026-09-13", now=_NOW)[0] is None
    assert velocity_proxy(100, None, now=_NOW) == (None, "missing_published_at")
    assert velocity_proxy(100, "not-a-date", now=_NOW) == (None, "unparseable_published_at")
    # future publication date → non-positive age, never a negative velocity
    assert velocity_proxy(100, "2026-09-25", now=_NOW) == (None, "non_positive_age")


def test_velocity_accepts_full_iso_and_yyyymmdd():
    v1, _ = velocity_proxy(7300, "2026-09-13T00:00:00Z", now=_NOW)
    v2, _ = velocity_proxy(7300, "2026-09-13", now=_NOW)
    assert v1 == v2


def test_analytics_deterministic_repeatable():
    inputs = ([8000, 10000, 12000], 75000)
    assert outlier_multiplier(inputs[1], channel_median_views(inputs[0])) \
        == outlier_multiplier(inputs[1], channel_median_views(inputs[0]))
