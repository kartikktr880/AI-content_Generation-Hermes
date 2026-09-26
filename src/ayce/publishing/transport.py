"""Stage 5 — YouTube transport (narrow seam around the official API).

The ONLY external-API surface of the publishing subsystem. It speaks the
official **YouTube Data API v3** REST protocol directly (stdlib ``urllib``
— no new dependency, no scraping, no browser automation):

- resumable upload session initiation
  (``POST /upload/youtube/v3/videos?uploadType=resumable``)
- chunked upload PUTs with ``Content-Range`` (200/201 done, 308 resume)
- resumable session status query (``PUT`` with ``Content-Range: bytes */N``)
- video lookup (``GET /youtube/v3/videos``) for reconciliation

The abstraction is deliberately NARROW (§30): three upload operations +
one read. Production uses :class:`UrllibYouTubeTransport`; tests use a
fake implementing the same protocol (§30). Transport-level failures are
classified by :mod:`ayce.publishing.errors` — the transport never
decides policy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "API_BASE",
    "UPLOAD_BASE",
    "HttpResponse",
    "YouTubeTransport",
    "UrllibYouTubeTransport",
]

API_BASE = "https://www.googleapis.com/youtube/v3"
UPLOAD_BASE = "https://www.googleapis.com/upload/youtube/v3"


@dataclass(frozen=True)
class HttpResponse:
    """Minimal HTTP response: status, relevant headers, parsed-ready body."""

    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    @property
    def range_end(self) -> int | None:
        """The last received byte offset from a 308 ``Range`` header."""
        value = self.headers.get("range") or self.headers.get("Range")
        if not value or "=" not in value:
            return None
        end = value.split("=", 1)[1].split("-", 1)[-1].strip()
        try:
            return int(end)
        except ValueError:
            return None

    @property
    def location(self) -> str | None:
        return self.headers.get("location") or self.headers.get("Location")


class YouTubeTransport:
    """The narrow protocol seam (§30). Implementations MUST NOT make
    policy decisions, retry, or log secrets."""

    def initiate_resumable(self, access_token: str, metadata: dict,
                           file_size: int) -> HttpResponse:
        raise NotImplementedError

    def put_chunk(self, session_url: str, data: bytes, offset: int,
                  total_size: int) -> HttpResponse:
        raise NotImplementedError

    def upload_status(self, session_url: str, total_size: int) -> HttpResponse:
        raise NotImplementedError

    def get_video(self, access_token: str, video_id: str) -> HttpResponse:
        raise NotImplementedError


class UrllibYouTubeTransport(YouTubeTransport):
    """The REAL transport: official YouTube Data API v3 over stdlib HTTP."""

    def __init__(self, *, api_base: str = API_BASE,
                 upload_base: str = UPLOAD_BASE, urlopen: Any = None) -> None:
        self._api_base = api_base
        self._upload_base = upload_base
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

    def initiate_resumable(self, access_token: str, metadata: dict,
                           file_size: int) -> HttpResponse:
        import urllib.request

        request = urllib.request.Request(
            f"{self._upload_base}/videos?uploadType=resumable&part=snippet,status",
            data=json.dumps(metadata).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(file_size),
                "X-Upload-Content-Type": "video/mp4",
            },
        )
        return self._request(request)

    def put_chunk(self, session_url: str, data: bytes, offset: int,
                  total_size: int) -> HttpResponse:
        import urllib.request

        end = offset + len(data) - 1
        content_range = f"bytes {offset}-{end}/{total_size}"
        if len(data) == 0:
            content_range = f"bytes */{total_size}"
        request = urllib.request.Request(
            session_url, data=data, method="PUT",
            headers={
                "Content-Length": str(len(data)),
                "Content-Range": content_range,
            },
        )
        return self._request(request)

    def upload_status(self, session_url: str, total_size: int) -> HttpResponse:
        import urllib.request

        request = urllib.request.Request(
            session_url, data=b"", method="PUT",
            headers={
                "Content-Length": "0",
                "Content-Range": f"bytes */{total_size}",
            },
        )
        return self._request(request)

    def get_video(self, access_token: str, video_id: str) -> HttpResponse:
        import urllib.request

        request = urllib.request.Request(
            f"{self._api_base}/videos?part=snippet,status&id={video_id}",
            method="GET",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return self._request(request)