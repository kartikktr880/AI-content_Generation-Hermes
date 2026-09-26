"""Stage 6 — Analytics transport (narrow read-only seam, §28).

The ONLY external-API surface of the analytics subsystem. Two READ-ONLY
operations over two OFFICIAL APIs — the project's established separation
of concerns (accepted 4A–4L research: "YouTube Analytics Closed-Loop
Architecture") is preserved, NOT collapsed into one generic API:

- :meth:`AnalyticsTransport.get_video_statistics` — official
  **YouTube Data API v3** ``videos.list`` (``part=statistics``): the
  public per-video engagement counts (views / likes / comments) as a
  cumulative lifetime snapshot. Same official API family the Stage 5
  publisher already uses for reconciliation.
- :meth:`AnalyticsTransport.query_report` — official
  **YouTube Analytics API v2** ``reports.query``: the private per-video
  metrics (watch time, average view duration / percentage, subscriber
  changes, likes, comments) over an EXPLICIT date window.

Reach metrics (impressions / thumbnail CTR) are deliberately NOT
requested here: per the accepted research (verified finding VF-01/VF-02),
the Analytics API rejects them with HTTP 400 and they are available
exclusively through the YouTube Reporting API bulk CSV exports, which
remain out of scope for Stage 6 (honest limitation, never fabricated).

No scraping, no browser automation, no secrets in logs, no persistence.
Implementations MUST NOT make policy decisions, retry, or store data —
the collector owns all of that.
"""

from __future__ import annotations

from typing import Any

from ..publishing.transport import API_BASE, HttpResponse

__all__ = ["ANALYTICS_API_BASE", "AnalyticsTransport", "UrllibAnalyticsTransport"]

#: Official YouTube Analytics API v2 base (targeted reports endpoint).
ANALYTICS_API_BASE = "https://youtubeanalytics.googleapis.com/v2"


class AnalyticsTransport:
    """The narrow read-only protocol seam (§28). Implementations MUST NOT
    make policy decisions, retry, persist anything, or log secrets."""

    def query_report(self, access_token: str, params: dict) -> HttpResponse:
        """YouTube Analytics API v2 ``reports.query`` — ``params`` are the
        exact query parameters (ids, startDate, endDate, metrics,
        filters); the access token is NEVER part of them."""
        raise NotImplementedError

    def get_video_statistics(self, access_token: str, video_id: str) -> HttpResponse:
        """YouTube Data API v3 ``videos.list`` (``part=statistics``)."""
        raise NotImplementedError


class UrllibAnalyticsTransport(AnalyticsTransport):
    """The REAL transport: official YouTube Analytics API v2 + Data API v3
    over stdlib HTTP (no new dependency, same pattern as Stage 5)."""

    def __init__(self, *, analytics_base: str = ANALYTICS_API_BASE,
                 data_api_base: str = API_BASE, urlopen: Any = None) -> None:
        self._analytics_base = analytics_base
        self._data_api_base = data_api_base
        self._urlopen = urlopen

    def _request(self, request: Any) -> HttpResponse:
        import urllib.error
        import urllib.request

        opener = self._urlopen or urllib.request.urlopen
        try:
            with opener(request, timeout=120) as response:
                return HttpResponse(
                    status=response.status,
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:  # 4xx/5xx ARE responses here
            return HttpResponse(
                status=exc.code,
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=exc.read(),
            )

    def query_report(self, access_token: str, params: dict) -> HttpResponse:
        import urllib.parse
        import urllib.request

        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(
            f"{self._analytics_base}/reports?{query}",
            method="GET",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return self._request(request)

    def get_video_statistics(self, access_token: str, video_id: str) -> HttpResponse:
        import urllib.parse
        import urllib.request

        request = urllib.request.Request(
            f"{self._data_api_base}/videos?part=statistics"
            f"&id={urllib.parse.quote(video_id, safe='')}",
            method="GET",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return self._request(request)
