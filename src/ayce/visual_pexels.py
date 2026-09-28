"""Real visual-asset provider (P8) — Pexels REST API behind the P2 adapter seam.

Production replacement for the fixture-backed asset provider: it implements
:class:`ayce.asset_resolution.AssetProvider` and is selected ONLY when
``AYCE_PEXELS_API_KEY`` is configured (see :func:`select_asset_provider`).
The deterministic file-backed fixture provider remains the default and is
untouched for tests, so the verified Golden Path input is unchanged.

Boundary rules (inherited from the P2 contract):

- ``resolve()`` copies the downloaded media into the run directory
  (``assets/``) and returns the existing :class:`ResolvedAsset` record. The
  Scene Contract is never modified and no provider concept leaks into it.
- Real provenance/licensing is recorded truthfully: the Pexels license and
  the full source metadata (asset id, kind, creator, source/page URL,
  retrieval time, dimensions, duration, content hash) are written to a
  provenance SIDECAR next to the resolved copy
  (``assets/<scene_id>.provenance.json``). The asset-manifest record itself
  keeps the existing fields (``provider``/``source``/``source_ref``/
  ``license``) — no manifest-schema change.
- Fail-fast and truthful: missing credentials, network/API/auth failures,
  malformed responses, no candidate meeting the production minimums, or a
  downloaded file that is not the expected media type all raise
  :class:`AssetResolutionError`. Nothing is invented — no placeholder media,
  no "best effort" silent success.
- Bounded network behavior (§25): explicit timeouts, bounded retries with
  backoff for TRANSIENT failures only (429/5xx/timeouts), a download-size
  ceiling, and a local content cache keyed by Pexels asset id so repeat runs
  do not re-download.
- Orientation is provider CONFIGURATION (``AYCE_PEXELS_ORIENTATION``,
  default ``landscape``): the Scene Contract deliberately carries no
  orientation/aspect field, so no contract field is invented. Minimum
  resolution follows the orientation (landscape >= 1920x1080, portrait
  >= 1080x1920, square >= 1080x1080).
- Video duration adequacy is enforced at the provider level via
  ``AYCE_PEXELS_MIN_VIDEO_DURATION_S`` (default 4.0 s). The P2
  ``resolve()`` signature does not receive the scene duration, so per-scene
  duration adequacy cannot be enforced here without changing the contract —
  a documented limitation, not a silently ignored requirement.
- The API key is a SECRET: read from configuration/environment only, sent
  as the ``Authorization`` header, and never logged or written to disk.

Pixabay is NOT implemented: the verified provider architecture executes the
single provider it is given and explicitly forbids in-provider fallback
composition ("Selection and fallback are orchestration policy"). Adding a
Pixabay fallback would therefore require a new fallback mechanism — out of
scope by the existing design.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from .adapters import AdapterHealth
from .asset_resolution import (
    ASSETS_DIRNAME,
    AssetProvider,
    AssetProvenance,
    AssetResolutionError,
    FileBackedAssetProvider,
    ResolvedAsset,
)
from .config import Config
from .scene_contract import AssetKind, AssetRequirement

__all__ = [
    "PexelsVisualProvider",
    "select_asset_provider",
]

#: Adapter name (recorded in the asset manifest ``provider`` field).
PROVIDER_NAME = "pexels-api"

#: Official Pexels REST API (v1).
API_BASE = "https://api.pexels.com/v1"
PHOTOS_SEARCH_PATH = "/search"
VIDEOS_SEARCH_PATH = "/videos/search"

#: Results requested per search (bounded, deterministic single page).
PER_PAGE = 15

#: Bounded network behavior (§25). Timeouts are hang-guards, not targets.
HTTP_TIMEOUT_SECONDS = 30
DOWNLOAD_TIMEOUT_SECONDS = 120
#: Transient failures are retried; auth/4xx client errors are permanent.
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.0
TRANSIENT_STATUS_CODES = (429, 500, 502, 503, 504)
#: Hard ceiling on a single download (bounded autonomous pipeline).
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024

#: Production minimum resolution per orientation (frame must be usable for
#: the target video format; enforced on real API metadata AND the download).
MIN_RESOLUTION_BY_ORIENTATION: dict[str, tuple[int, int]] = {
    "landscape": (1920, 1080),
    "portrait": (1080, 1920),
    "square": (1080, 1080),
}

#: Run-relative extension per asset kind for the resolved copy.
EXTENSIONS: dict[AssetKind, str] = {AssetKind.IMAGE: ".jpg", AssetKind.VIDEO: ".mp4"}

#: Pexels license facts recorded in the provenance sidecar (stated by the
#: provider's own license page — attribution is appreciated, not required).
LICENSE_NAME = "Pexels License"
LICENSE_URL = "https://www.pexels.com/license/"
ATTRIBUTION = "not_required"

#: Default cache location under the configured data directory.
DEFAULT_CACHE_SUBDIR = Path("assets_cache") / "pexels"


# ---- transport seams (injectable for tests; the real path uses urllib) ------


def _now_iso() -> str:
    """UTC retrieval timestamp for provenance (never invented locally)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _open_url(url: str, *, api_key: str, timeout: int):
    """Open one HTTP request with the Pexels Authorization header.

    Single transport seam for both searches and downloads. The key is only
    ever sent in the header (never in the URL/query, never logged).
    """
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": api_key,
            "Accept": "application/json, video/*, image/*",
            "User-Agent": "ayce/0.0.1 (AI-content_Generation-Hermes)",
        },
    )
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 — fixed https API base


class _PermanentApiError(AssetResolutionError):
    """Non-retryable failure (auth/client error, malformed response)."""


def _error_code(exc: BaseException) -> int | None:
    status = getattr(exc, "code", None)
    return status if isinstance(status, int) else None


def _fetch_json(url: str, *, api_key: str) -> dict:
    """GET one JSON document with bounded retries for transient failures."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with _open_url(url, api_key=api_key, timeout=HTTP_TIMEOUT_SECONDS) as response:
                raw = response.read()
            break
        except urllib.error.HTTPError as exc:
            code = _error_code(exc)
            detail = ""
            try:
                detail = exc.read()[:300].decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 — diagnostics only
                detail = ""
            if code in TRANSIENT_STATUS_CODES and attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            if code in (401, 403):
                raise _PermanentApiError(
                    f"Pexels rejected the credentials (HTTP {code}); check "
                    f"AYCE_PEXELS_API_KEY (an invalid key is never retried)"
                ) from exc
            raise AssetResolutionError(
                f"Pexels API request failed with HTTP {code}: {detail or exc.reason}"
            ) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            raise AssetResolutionError(
                f"Pexels API unreachable after {MAX_ATTEMPTS} attempts: {exc}"
            ) from None

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _PermanentApiError(
            f"Pexels returned a malformed (non-JSON) response: {exc}"
        ) from None
    if not isinstance(payload, dict):
        raise _PermanentApiError(
            f"Pexels response must be a JSON object, got {type(payload).__name__}"
        )
    return payload


def _stream_to_file(url: str, destination: Path, *, api_key: str) -> int:
    """Stream one media file to ``destination``; return the bytes written.

    Bounded by :data:`MAX_DOWNLOAD_BYTES`; transient failures are retried
    with backoff and a partial file is never left in place.
    """
    last_error: BaseException | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        written = 0
        try:
            with _open_url(url, api_key=api_key, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                with destination.open("wb") as handle:
                    while True:
                        chunk = response.read(_DOWNLOAD_CHUNK_BYTES)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > MAX_DOWNLOAD_BYTES:
                            raise AssetResolutionError(
                                f"Pexels asset exceeds the {MAX_DOWNLOAD_BYTES} byte "
                                f"download ceiling: {url}"
                            )
                        handle.write(chunk)
            return written
        except AssetResolutionError:
            destination.unlink(missing_ok=True)
            raise
        except urllib.error.HTTPError as exc:
            destination.unlink(missing_ok=True)
            code = _error_code(exc)
            if code in TRANSIENT_STATUS_CODES and attempt < MAX_ATTEMPTS:
                last_error = exc
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            raise AssetResolutionError(
                f"Pexels media download failed with HTTP {code}: {url}"
            ) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            destination.unlink(missing_ok=True)
            if attempt < MAX_ATTEMPTS:
                last_error = exc
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            raise AssetResolutionError(
                f"Pexels media download failed after {MAX_ATTEMPTS} attempts: {exc}"
            ) from None
    raise AssetResolutionError(  # pragma: no cover — every path above returns or raises
        f"Pexels media download failed: {last_error}"
    )


def _validate_media_signature(path: Path, kind: AssetKind) -> None:
    """Verify the downloaded bytes really are the expected media type.

    A non-empty file with the wrong container is a failure, never a silent
    pass: the run must not ship media that is not what was selected.
    """
    with path.open("rb") as handle:
        head = handle.read(16)
    if not head:
        raise AssetResolutionError(f"downloaded asset is empty: {path}")
    if kind is AssetKind.VIDEO:
        if head[4:8] != b"ftyp":
            raise AssetResolutionError(
                f"downloaded file is not an ISO-BMFF/MP4 container: {path}"
            )
        return
    if head.startswith(b"\xff\xd8\xff") or head.startswith(b"\x89PNG\r\n\x1a\n"):
        return
    raise AssetResolutionError(f"downloaded file is not a JPEG/PNG image: {path}")


# ---- search candidates ------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    """One search result normalized from the Pexels payload."""

    asset_id: str
    file_id: str | None
    kind: AssetKind
    download_url: str
    page_url: str
    creator: str
    creator_url: str
    width: int
    height: int
    duration_seconds: float | None
    extension: str

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def cache_stem(self) -> str:
        """Deterministic cache filename stem (asset id + rendition id)."""
        if self.file_id:
            return f"{self.kind.value}-{self.asset_id}-{self.file_id}"
        return f"{self.kind.value}-{self.asset_id}"


# ---- provider ---------------------------------------------------------------


class PexelsVisualProvider(AssetProvider):
    """Production visual assets from the official Pexels REST API (P8).

    Selected only when ``AYCE_PEXELS_API_KEY`` is configured; see
    :func:`select_asset_provider`. Configuration (via ``AYCE_*`` through
    :class:`ayce.config.Config`): ``AYCE_PEXELS_API_KEY`` (required),
    ``AYCE_PEXELS_ORIENTATION`` (landscape/portrait/square),
    ``AYCE_PEXELS_MIN_VIDEO_DURATION_S``, ``AYCE_PEXELS_CACHE_DIR``.
    """

    name: ClassVar[str] = PROVIDER_NAME

    def __init__(self, config: Config, *, cache_dir: str | Path | None = None) -> None:
        super().__init__(config)
        self.api_key: str | None = config.pexels_api_key
        self.orientation: str = config.pexels_orientation
        self.min_video_duration_s: float = config.pexels_min_video_duration_s
        if cache_dir is not None:
            self.cache_dir = Path(cache_dir)
        elif config.pexels_cache_dir:
            self.cache_dir = Path(config.pexels_cache_dir)
        else:
            self.cache_dir = config.resolved_data_dir / DEFAULT_CACHE_SUBDIR

    # ---- truthful health ----------------------------------------------------

    def health(self) -> AdapterHealth:
        """Cheap, truthful: credentials configured + declared production rules.

        No network request is made here (health must never hang the stage);
        the API is contacted only during :meth:`resolve`.
        """
        if not self.api_key:
            return AdapterHealth(
                name=self.name,
                available=False,
                detail=(
                    "AYCE_PEXELS_API_KEY is not configured: the production Pexels "
                    "provider needs an API key (set it in the environment or the "
                    "git-ignored .env); no request is attempted without it"
                ),
            )
        width, height = self.minimum_resolution()
        return AdapterHealth(
            name=self.name,
            available=True,
            detail=(
                f"Pexels API key configured (orientation={self.orientation}, "
                f"minimum {width}x{height}, minimum video duration "
                f"{self.min_video_duration_s}s); the API is contacted only during "
                f"resolution"
            ),
        )

    def minimum_resolution(self) -> tuple[int, int]:
        """Production minimum frame size implied by the configured orientation."""
        return MIN_RESOLUTION_BY_ORIENTATION[self.orientation]

    # ---- resolution ---------------------------------------------------------

    def resolve(
        self,
        requirement: AssetRequirement,
        scene_id: str,
        *,
        run_dir: Path,
    ) -> ResolvedAsset:
        """Resolve one requirement from Pexels into a run-local copied asset."""
        if not self.api_key:
            raise AssetResolutionError(
                "provider 'pexels-api' is not configured: AYCE_PEXELS_API_KEY is "
                "unset (set it in the environment or the git-ignored .env)"
            )
        if requirement.kind not in EXTENSIONS:
            raise AssetResolutionError(
                f"provider {self.name!r} cannot resolve asset kind "
                f"{requirement.kind.value!r}"
            )
        query = requirement.description
        candidate = self._select(query, requirement.kind)
        cached = self._ensure_cached(candidate)

        assets_dir = Path(run_dir) / ASSETS_DIRNAME
        assets_dir.mkdir(parents=True, exist_ok=True)
        destination = assets_dir / f"{scene_id}{candidate.extension}"
        digest, size_bytes = _copy_and_hash(cached, destination)
        _write_provenance_sidecar(
            assets_dir=assets_dir,
            scene_id=scene_id,
            candidate=candidate,
            sha256=digest,
            size_bytes=size_bytes,
            query=query,
            orientation=self.orientation,
            cached_path=cached,
        )
        return ResolvedAsset(
            scene_id=scene_id,
            kind=requirement.kind,
            path=f"{ASSETS_DIRNAME}/{destination.name}",
            sha256=digest,
            requirement_description=requirement.description,
            provenance=AssetProvenance(
                provider=self.name,
                source="pexels",
                source_ref=candidate.asset_id,
                license=f"{LICENSE_NAME} ({LICENSE_URL})",
            ),
        )

    # ---- search + selection -------------------------------------------------

    def _search(self, query: str, kind: AssetKind) -> list[_Candidate]:
        """Search Pexels and normalize the response (strict schema checks)."""
        path = VIDEOS_SEARCH_PATH if kind is AssetKind.VIDEO else PHOTOS_SEARCH_PATH
        params = {
            "query": query,
            "orientation": self.orientation,
            "size": "large",
            "per_page": PER_PAGE,
        }
        url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
        payload = _fetch_json(url, api_key=self.api_key or "")
        key = "videos" if kind is AssetKind.VIDEO else "photos"
        if key not in payload:
            raise _PermanentApiError(
                f"malformed Pexels response for {path}: missing {key!r} field"
            )
        entries = payload[key]
        if not isinstance(entries, list):
            raise _PermanentApiError(
                f"malformed Pexels response for {path}: {key!r} must be a list, "
                f"got {type(entries).__name__}"
            )
        parser = _parse_video if kind is AssetKind.VIDEO else _parse_photo
        candidates = [
            candidate
            for entry in entries
            if isinstance(entry, dict)
            for candidate in parser(entry)
        ]
        if entries and not candidates:
            raise _PermanentApiError(
                f"malformed Pexels response for {path}: {len(entries)} result(s) "
                f"but none carried a usable {kind.value} rendition"
            )
        return candidates

    def _select(self, query: str, kind: AssetKind) -> _Candidate:
        """Choose the best candidate that meets the production minimums.

        Deterministic: highest pixel area, then longest duration, then the
        stable cache stem. No candidate satisfying the minimums is a truthful
        failure — never a smaller or looser substitution.
        """
        candidates = self._search(query, kind)
        width, height = self.minimum_resolution()
        eligible = [c for c in candidates if c.width >= width and c.height >= height]
        if kind is AssetKind.VIDEO:
            eligible = [
                c
                for c in eligible
                if c.duration_seconds is not None
                and c.duration_seconds >= self.min_video_duration_s
            ]
        if not eligible:
            raise AssetResolutionError(
                f"no Pexels {kind.value} for query {query!r} met the production "
                f"minimums ({width}x{height}"
                + (
                    f", duration >= {self.min_video_duration_s}s"
                    if kind is AssetKind.VIDEO
                    else ""
                )
                + f"); examined {len(candidates)} candidate(s)"
                + self._describe_best(candidates)
            )
        return sorted(
            eligible,
            key=lambda c: (-c.area, -(c.duration_seconds or 0.0), c.cache_stem),
        )[0]

    @staticmethod
    def _describe_best(candidates: list[_Candidate]) -> str:
        if not candidates:
            return " (the API returned none)"
        best = sorted(candidates, key=lambda c: (-c.area, c.cache_stem))[0]
        duration = (
            f", duration {best.duration_seconds}s"
            if best.duration_seconds is not None
            else ""
        )
        return f"; largest was {best.width}x{best.height}{duration}"

    # ---- cache --------------------------------------------------------------

    def _ensure_cached(self, candidate: _Candidate) -> Path:
        """Return the cached file for this candidate, downloading if needed.

        The cache is keyed by Pexels asset id (+ rendition id), so repeat runs
        reuse the same real file instead of re-downloading. A cached file is
        re-validated (non-empty + correct media signature) before reuse; a
        partial download is never left behind.
        """
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        target = self.cache_dir / f"{candidate.cache_stem}{candidate.extension}"
        if target.is_file() and target.stat().st_size > 0:
            _validate_media_signature(target, candidate.kind)
            return target
        partial = target.with_name(target.name + ".part")
        try:
            written = _stream_to_file(
                candidate.download_url, partial, api_key=self.api_key or ""
            )
            if written <= 0:
                raise AssetResolutionError(
                    f"Pexels returned an empty file for {candidate.download_url}"
                )
            _validate_media_signature(partial, candidate.kind)
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        return target


# ---- provenance sidecar -----------------------------------------------------


def _copy_and_hash(source: Path, destination: Path) -> tuple[str, int]:
    """Copy ``source`` to ``destination``, returning (sha256, bytes).

    Streamed, so large video assets are never loaded into memory.
    """
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as reader, destination.open("wb") as writer:
        while True:
            chunk = reader.read(_DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            writer.write(chunk)
    return digest.hexdigest(), size


def _write_provenance_sidecar(
    *,
    assets_dir: Path,
    scene_id: str,
    candidate: _Candidate,
    sha256: str,
    size_bytes: int,
    query: str,
    orientation: str,
    cached_path: Path,
) -> Path:
    """Record real-world provenance/licensing next to the resolved copy.

    Operator-facing record (NOT part of the Scene Contract or the asset
    manifest). Every value is taken from the API response or the actual
    downloaded bytes; unknown values stay empty rather than being guessed.
    """
    payload = {
        "schema_version": "1.0",
        "provider": PROVIDER_NAME,
        "source": "pexels",
        "asset_id": candidate.asset_id,
        "rendition_id": candidate.file_id,
        "asset_kind": candidate.kind.value,
        "query": query,
        "orientation": orientation,
        "source_url": candidate.download_url,
        "page_url": candidate.page_url,
        "creator": candidate.creator,
        "creator_url": candidate.creator_url,
        "license": LICENSE_NAME,
        "license_url": LICENSE_URL,
        "attribution": ATTRIBUTION,
        "retrieved_at": _now_iso(),
        "width": candidate.width,
        "height": candidate.height,
        "duration_seconds": candidate.duration_seconds,
        "resolved_path": f"{ASSETS_DIRNAME}/{scene_id}{candidate.extension}",
        "sha256": sha256,
        "size_bytes": size_bytes,
        "cache_path": str(cached_path),
    }
    sidecar = assets_dir / f"{scene_id}.provenance.json"
    sidecar.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return sidecar


# ---- selection (the ONLY wiring point in the pipeline) ----------------------


def select_asset_provider(config: Config, assets_dir: str | Path | None = None):
    """Opt-in provider selection (mirrors ``select_narration_provider``).

    - Pexels is selected ONLY when ``AYCE_PEXELS_API_KEY`` is configured.
    - Otherwise the deterministic file-backed fixture provider is returned
      (with ``assets_dir`` when the caller supplied one) — the verified
      Golden Path behavior is untouched.

    Selection is orchestration policy: the stage itself never chooses or
    falls back between providers, so no fallback chain is composed here.
    """
    if config.pexels_api_key:
        return PexelsVisualProvider(config)
    if assets_dir is not None:
        return FileBackedAssetProvider(config, assets_dir)
    return FileBackedAssetProvider(config)


def _as_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _as_float(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _parse_photo(entry: dict) -> list[_Candidate]:
    """Normalize one Pexels photo entry; unusable entries yield nothing."""
    asset_id = str(entry.get("id") or "").strip()
    source = entry.get("src")
    url = source.get("original") if isinstance(source, dict) else None
    width, height = _as_int(entry.get("width")), _as_int(entry.get("height"))
    if not asset_id or not isinstance(url, str) or not url or width is None or height is None:
        return []
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    extension = suffix if suffix in (".jpg", ".jpeg", ".png") else ".jpg"
    return [
        _Candidate(
            asset_id=asset_id,
            file_id=None,
            kind=AssetKind.IMAGE,
            download_url=url,
            page_url=str(entry.get("url") or ""),
            # Empty strings mean the provider response did not supply the value —
            # recorded as-is rather than invented.
            creator=str(entry.get("photographer") or ""),
            creator_url=str(entry.get("photographer_url") or ""),
            width=width,
            height=height,
            duration_seconds=None,
            extension=extension,
        )
    ]


def _parse_video(entry: dict) -> list[_Candidate]:
    """Normalize one Pexels video entry into one candidate per MP4 file.

    Non-MP4 renditions (streaming manifests) are ignored: the production
    contract asks for an appropriate MP4 asset. A video without any usable
    MP4 rendition yields nothing (never a substituted URL).
    """
    asset_id = str(entry.get("id") or "").strip()
    files = entry.get("video_files")
    if not asset_id or not isinstance(files, list):
        return []
    duration = _as_float(entry.get("duration"))
    user = entry.get("user") if isinstance(entry.get("user"), dict) else {}
    candidates: list[_Candidate] = []
    for file_entry in files:
        if not isinstance(file_entry, dict):
            continue
        if str(file_entry.get("file_type") or "").lower() != "video/mp4":
            continue
        link = file_entry.get("link")
        width, height = _as_int(file_entry.get("width")), _as_int(file_entry.get("height"))
        if not isinstance(link, str) or not link or width is None or height is None:
            continue
        if not urllib.parse.urlparse(link).path.lower().endswith(".mp4"):
            continue
        file_id = file_entry.get("id")
        candidates.append(
            _Candidate(
                asset_id=asset_id,
                file_id=str(file_id) if file_id is not None else "",
                kind=AssetKind.VIDEO,
                download_url=link,
                page_url=str(entry.get("url") or ""),
                creator=str(user.get("name") or ""),
                creator_url=str(user.get("url") or ""),
                width=width,
                height=height,
                duration_seconds=duration,
                extension=".mp4",
            )
        )
    return candidates