"""Stage 6 — Analytics & measurement ingestion (OBSERVATION ONLY).

Observation-only measurement layer for videos published through the
Stage 5 publishing subsystem:

    Published Video
        → official source APIs (Data API v3 + Analytics API v2)
        → durable raw observations (deterministic identity, immutable)
        → normalized measurements (stable project semantics)
        → full production lineage (video → package → run → script
          → research → objective)

Hard boundary (§4/§16/§36): analytics OBSERVES, STORES, NORMALIZES and
TRACES — it never decides. No policy, recommendations, scoring, ranking
or learning exists here; Stage 7 owns the decision/experimentation
boundary. Production state (RunState, packages, research) and
publication state (publish ledger) are read-only to this subsystem.
"""

from .errors import (
    ERROR_CATEGORIES,
    RETRYABLE_CATEGORIES,
    AnalyticsError,
    categorize_exception,
    categorize_http_status,
)
from .store import AnalyticsStore
from .transport import (
    ANALYTICS_API_BASE,
    AnalyticsTransport,
    UrllibAnalyticsTransport,
)
from .collector import (
    DEFAULT_METRICS,
    METRIC_CATALOG,
    SUPPORTED_SOURCES,
    collect_analytics,
    resolve_publication,
    show_analytics,
)

__all__ = [
    "ERROR_CATEGORIES",
    "RETRYABLE_CATEGORIES",
    "ANALYTICS_API_BASE",
    "AnalyticsError",
    "AnalyticsStore",
    "AnalyticsTransport",
    "UrllibAnalyticsTransport",
    "DEFAULT_METRICS",
    "METRIC_CATALOG",
    "SUPPORTED_SOURCES",
    "categorize_exception",
    "categorize_http_status",
    "collect_analytics",
    "resolve_publication",
    "show_analytics",
]
