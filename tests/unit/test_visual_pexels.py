"""P8 PexelsVisualProvider tests.

The network is MOCKED at the provider's transport seam — the normal suite
never contacts the Pexels API and needs no API key. Covers: request
construction/query parameters, credential handling, resolution and duration
filtering, MP4 rendition selection, malformed responses, API failures
(transient vs permanent), provenance sidecar, content hashing, downloaded
byte validation, cache reuse, and config-driven provider selection.
"""

import hashlib
import json
import os
import urllib.error
from io import BytesIO
from pathlib import Path

import pytest

from ayce.asset_resolution import (
    ASSETS_DIRNAME,
    AssetResolutionError,
    FileBackedAssetProvider,
    ResolvedAsset,
)
from ayce.config import Config, ConfigError
from ayce.scene_contract import AssetKind, AssetRequirement
from ayce.visual_pexels import (
    API_BASE,
    PER_PAGE,
    PexelsVisualProvider,
    select_asset_provider,
)

API_KEY = "test-pexels-key-not-a-real-secret"

#: A minimal but structurally valid MP4 header (ftyp box) + payload.
MP4_BYTES = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 64
#: A minimal JPEG magic header + payload.
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 64


def requirement(kind: AssetKind = AssetKind.VIDEO, description: str = "sunrise over mountains"):
    return AssetRequirement(kind=kind, description=description)


def photo(asset_id: int, width: int, height: int, *, photographer: str = "Ada Lens") -> dict:
    return {
        "id": asset_id,
        "width": width,
        "height": height,
        "url": f"https://www.pexels.com/photo/{asset_id}/",
        "photographer": photographer,
        "photographer_url": f"https://www.pexels.com/@ada-{asset_id}",
        "src": {"original": f"https://images.pexels.com/photos/{asset_id}/original.jpg"},
        "alt": "a scene",
    }


def video(asset_id: int, width: int, height: int, duration: float, *files) -> dict:
    entries = [
        {
            "id": asset_id * 10 + index,
            "quality": quality,
            "file_type": file_type,
            "width": file_width,
            "height": file_height,
            "link": link,
        }
        for index, (quality, file_type, file_width, file_height, link) in enumerate(files)
    ]
    return {
        "id": asset_id,
        "width": width,
        "height": height,
        "duration": duration,
        "url": f"https://www.pexels.com/video/{asset_id}/",
        "user": {"id": 7, "name": "Vera Motion", "url": f"https://www.pexels.com/@vera-{asset_id}"},
        "video_files": entries,
    }


class FakeResponse:
    """Minimal context-managed HTTP response for the transport seam."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self, amount: int | None = None) -> bytes:
        if amount is None:
            body, self._body = self._body, b""
            return body
        chunk, self._body = self._body[:amount], self._body[amount:]
        return chunk

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def install_transport(monkeypatch, payload: dict | None, *, media: bytes = MP4_BYTES,
                      media_status: int = 200, search_status: int = 200, calls: list | None = None):
    """Replace the module transport seam with a recording fake."""
    from ayce import visual_pexels

    recorded = calls if calls is not None else []

    def fake_open(url: str, *, api_key: str, timeout: int):
        recorded.append({"url": url, "api_key": api_key, "timeout": timeout})
        if url.startswith(API_BASE):
            if search_status != 200:
                raise urllib.error.HTTPError(
                    url, search_status, "boom", {}, BytesIO(b'{"error":"boom"}')
                )
            body = json.dumps(payload).encode("utf-8")
            return FakeResponse(body)
        if media_status != 200:
            raise urllib.error.HTTPError(url, media_status, "boom", {}, BytesIO(b""))
        return FakeResponse(media)

    monkeypatch.setattr(visual_pexels, "_open_url", fake_open)
    monkeypatch.setattr(visual_pexels.time, "sleep", lambda _seconds: None)
    return recorded


def make_provider(tmp_path: Path, **overrides) -> PexelsVisualProvider:
    config = Config(
        data_dir=tmp_path / "data",
        pexels_api_key=overrides.pop("pexels_api_key", API_KEY),
        **overrides,
    )
    return PexelsVisualProvider(config, cache_dir=tmp_path / "cache")


# ---- request construction / health ------------------------------------------


def test_request_construction_and_query_parameters(tmp_path, monkeypatch):
    calls = install_transport(
        monkeypatch, {"photos": [photo(1, 2400, 1600)]}, media=JPEG_BYTES
    )
    provider = make_provider(tmp_path)
    asset = provider.resolve(requirement(AssetKind.IMAGE, "city skyline"), "scene-001",
                             run_dir=tmp_path / "run")

    search = calls[0]
    assert search["url"].startswith(f"{API_BASE}/search?")
    query = search["url"].split("?", 1)[1]
    assert "query=city+skyline" in query
    assert "orientation=landscape" in query
    assert "size=large" in query
    assert f"per_page={PER_PAGE}" in query
    # the API key travels ONLY in the Authorization header
    assert search["api_key"] == API_KEY
    assert "test-pexels-key" not in search["url"]
    assert search["timeout"] > 0
    assert asset.path == f"{ASSETS_DIRNAME}/scene-001.jpg"


def test_health_requires_api_key(tmp_path):
    provider = make_provider(tmp_path, pexels_api_key=None)
    health = provider.health()
    assert health.available is False
    assert "AYCE_PEXELS_API_KEY" in health.detail
    with pytest.raises(AssetResolutionError, match="AYCE_PEXELS_API_KEY"):
        provider.resolve(requirement(), "scene-001", run_dir=tmp_path / "run")


def test_health_reports_configuration_without_network(tmp_path, monkeypatch):
    calls = install_transport(monkeypatch, {"videos": []})
    provider = make_provider(tmp_path)
    health = provider.health()
    assert health.available is True
    assert "1920x1080" in health.detail
    assert calls == []  # health never contacts the API


# ---- resolution + duration filtering ---------------------------------------


def test_resolution_filtering_picks_largest_sufficient_photo(tmp_path, monkeypatch):
    install_transport(
        monkeypatch,
        {"photos": [photo(11, 640, 360), photo(12, 3000, 2000), photo(13, 1920, 1080)]},
        media=JPEG_BYTES,
    )
    provider = make_provider(tmp_path)
    asset = provider.resolve(requirement(AssetKind.IMAGE), "scene-002",
                             run_dir=tmp_path / "run")
    sidecar = json.loads(
        (tmp_path / "run" / "assets" / "scene-002.provenance.json").read_text("utf-8")
    )
    assert sidecar["asset_id"] == "12"  # 3000x2000 beats 1920x1080
    assert (sidecar["width"], sidecar["height"]) == (3000, 2000)
    assert asset.provenance.source_ref == "12"


def test_photo_below_production_minimum_fails_truthfully(tmp_path, monkeypatch):
    install_transport(monkeypatch, {"photos": [photo(11, 1280, 720)]}, media=JPEG_BYTES)
    provider = make_provider(tmp_path)
    with pytest.raises(AssetResolutionError) as excinfo:
        provider.resolve(requirement(AssetKind.IMAGE), "scene-003", run_dir=tmp_path / "run")
    message = str(excinfo.value)
    assert "1920x1080" in message and "1280x720" in message
    assert not (tmp_path / "run" / "assets" / "scene-003.jpg").exists()  # no fake asset


def test_video_duration_filtering(tmp_path, monkeypatch):
    install_transport(
        monkeypatch,
        {"videos": [
            video(21, 1920, 1080, 2.0, (
                "hd", "video/mp4", 1920, 1080,
                "https://videos.pexels.com/video-files/21/short.mp4")),
            video(22, 1920, 1080, 12.0, (
                "hd", "video/mp4", 1920, 1080,
                "https://videos.pexels.com/video-files/22/long.mp4")),
        ]},
        media=MP4_BYTES,
    )
    provider = make_provider(tmp_path)
    provider.resolve(requirement(AssetKind.VIDEO), "scene-004", run_dir=tmp_path / "run")
    sidecar = json.loads(
        (tmp_path / "run" / "assets" / "scene-004.provenance.json").read_text("utf-8")
    )
    assert sidecar["asset_id"] == "22"
    assert sidecar["duration_seconds"] == 12.0
    assert sidecar["source_url"].endswith("long.mp4")


def test_video_all_too_short_fails_with_duration_requirement(tmp_path, monkeypatch):
    install_transport(
        monkeypatch,
        {"videos": [video(31, 1920, 1080, 1.5, (
            "sd", "video/mp4", 1920, 1080,
            "https://videos.pexels.com/video-files/31/a.mp4"))]},
    )
    provider = make_provider(tmp_path, pexels_min_video_duration_s=4.0)
    with pytest.raises(AssetResolutionError, match="duration >= 4.0s"):
        provider.resolve(requirement(AssetKind.VIDEO), "scene-005", run_dir=tmp_path / "run")


def test_mp4_rendition_selected_and_non_mp4_ignored(tmp_path, monkeypatch):
    install_transport(
        monkeypatch,
        {"videos": [video(
            41, 1920, 1080, 9.0,
            ("hls", "application/x-mpegURL", 1920, 1080,
             "https://videos.pexels.com/video-files/41/stream.m3u8"),
            ("uhd", "video/mp4", 3840, 2160,
             "https://videos.pexels.com/video-files/41/uhd.mp4"),
        )]},
        media=MP4_BYTES,
    )
    provider = make_provider(tmp_path)
    asset = provider.resolve(requirement(AssetKind.VIDEO), "scene-006", run_dir=tmp_path / "run")
    sidecar = json.loads(
        (tmp_path / "run" / "assets" / "scene-006.provenance.json").read_text("utf-8")
    )
    assert sidecar["source_url"].endswith("uhd.mp4")   # the MP4 rendition
    assert sidecar["rendition_id"] == "411"
    assert asset.path.endswith(".mp4")


def test_portrait_orientation_requires_portrait_minimums(tmp_path, monkeypatch):
    calls = install_transport(monkeypatch, {"photos": [photo(51, 1080, 1920)]}, media=JPEG_BYTES)
    provider = make_provider(tmp_path, pexels_orientation="portrait")
    provider.resolve(requirement(AssetKind.IMAGE), "scene-007", run_dir=tmp_path / "run")
    assert "orientation=portrait" in calls[0]["url"]

    install_transport(monkeypatch, {"photos": [photo(52, 1920, 1080)]}, media=JPEG_BYTES)
    with pytest.raises(AssetResolutionError, match="1080x1920"):
        provider.resolve(requirement(AssetKind.IMAGE), "scene-008", run_dir=tmp_path / "run")


# ---- malformed responses ----------------------------------------------------


def test_malformed_response_missing_field(tmp_path, monkeypatch):
    install_transport(monkeypatch, {"page": 1, "per_page": PER_PAGE})
    provider = make_provider(tmp_path)
    with pytest.raises(AssetResolutionError, match="missing 'photos'"):
        provider.resolve(requirement(AssetKind.IMAGE), "scene-009", run_dir=tmp_path / "run")


def test_malformed_response_wrong_type(tmp_path, monkeypatch):
    install_transport(monkeypatch, {"photos": "not-a-list"})
    provider = make_provider(tmp_path)
    with pytest.raises(AssetResolutionError, match="must be a list"):
        provider.resolve(requirement(AssetKind.IMAGE), "scene-010", run_dir=tmp_path / "run")


def test_malformed_response_no_usable_entry(tmp_path, monkeypatch):
    install_transport(monkeypatch, {"photos": [{"id": 1, "width": "x"}, {"nothing": True}]})
    provider = make_provider(tmp_path)
    with pytest.raises(AssetResolutionError, match="none carried a usable image rendition"):
        provider.resolve(requirement(AssetKind.IMAGE), "scene-011", run_dir=tmp_path / "run")


def test_malformed_non_json_body(tmp_path, monkeypatch):
    from ayce import visual_pexels

    monkeypatch.setattr(
        visual_pexels,
        "_open_url",
        lambda url, *, api_key, timeout: FakeResponse(b"<html>not json</html>"),
    )
    monkeypatch.setattr(visual_pexels.time, "sleep", lambda _seconds: None)
    provider = make_provider(tmp_path)
    with pytest.raises(AssetResolutionError, match="malformed \\(non-JSON\\) response"):
        provider.resolve(requirement(AssetKind.IMAGE), "scene-012", run_dir=tmp_path / "run")


# ---- API failures -----------------------------------------------------------


def test_transient_server_error_is_retried_then_fails(tmp_path, monkeypatch):
    from ayce import visual_pexels

    attempts = []

    def failing_open(url, *, api_key, timeout):
        attempts.append(url)
        raise urllib.error.HTTPError(url, 503, "unavailable", {}, BytesIO(b'{"error":"busy"}'))

    monkeypatch.setattr(visual_pexels, "_open_url", failing_open)
    monkeypatch.setattr(visual_pexels.time, "sleep", lambda _seconds: None)
    provider = make_provider(tmp_path)
    with pytest.raises(AssetResolutionError, match="HTTP 503"):
        provider.resolve(requirement(AssetKind.IMAGE), "scene-013", run_dir=tmp_path / "run")
    assert len(attempts) == visual_pexels.MAX_ATTEMPTS  # bounded retries, then fail


def test_invalid_credentials_are_not_retried(tmp_path, monkeypatch):
    from ayce import visual_pexels

    attempts = []

    def unauthorised_open(url, *, api_key, timeout):
        attempts.append(url)
        raise urllib.error.HTTPError(url, 401, "unauthorised", {}, BytesIO(b'{"error":"key"}'))

    monkeypatch.setattr(visual_pexels, "_open_url", unauthorised_open)
    monkeypatch.setattr(visual_pexels.time, "sleep", lambda _seconds: None)
    provider = make_provider(tmp_path)
    with pytest.raises(AssetResolutionError, match="rejected the credentials"):
        provider.resolve(requirement(AssetKind.IMAGE), "scene-014", run_dir=tmp_path / "run")
    assert len(attempts) == 1  # permanent failure: a bad key is never retried


def test_unreachable_api_fails_bounded(tmp_path, monkeypatch):
    from ayce import visual_pexels

    attempts = []

    def unreachable_open(url, *, api_key, timeout):
        attempts.append(url)
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(visual_pexels, "_open_url", unreachable_open)
    monkeypatch.setattr(visual_pexels.time, "sleep", lambda _seconds: None)
    provider = make_provider(tmp_path)
    with pytest.raises(AssetResolutionError, match="unreachable after"):
        provider.resolve(requirement(AssetKind.IMAGE), "scene-015", run_dir=tmp_path / "run")
    assert len(attempts) == visual_pexels.MAX_ATTEMPTS


# ---- provenance sidecar + content hash --------------------------------------


def test_provenance_sidecar_and_content_hash(tmp_path, monkeypatch):
    install_transport(
        monkeypatch,
        {"videos": [video(61, 1920, 1080, 8.0, (
            "hd", "video/mp4", 1920, 1080,
            "https://videos.pexels.com/video-files/61/hd.mp4"))]},
        media=MP4_BYTES,
    )
    provider = make_provider(tmp_path)
    asset = provider.resolve(requirement(AssetKind.VIDEO, "solar panels"), "scene-016",
                             run_dir=tmp_path / "run")

    resolved = tmp_path / "run" / "assets" / "scene-016.mp4"
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    assert asset.sha256 == digest                       # manifest hash == real bytes
    assert isinstance(asset, ResolvedAsset)
    assert asset.provenance.provider == "pexels-api"
    assert asset.provenance.source == "pexels"
    assert asset.provenance.source_ref == "61"
    assert "Pexels License" in (asset.provenance.license or "")

    sidecar = json.loads(
        (tmp_path / "run" / "assets" / "scene-016.provenance.json").read_text("utf-8")
    )
    assert sidecar["asset_id"] == "61"
    assert sidecar["creator"] == "Vera Motion"
    assert sidecar["creator_url"].endswith("@vera-61")
    assert sidecar["source_url"].endswith("hd.mp4")
    assert sidecar["page_url"] == "https://www.pexels.com/video/61/"
    assert sidecar["license"] == "Pexels License"
    assert sidecar["license_url"].startswith("https://www.pexels.com/license")
    assert sidecar["attribution"] == "not_required"
    assert sidecar["resolved_path"] == "assets/scene-016.mp4"
    assert sidecar["query"] == "solar panels"
    assert sidecar["sha256"] == digest
    assert sidecar["size_bytes"] == resolved.stat().st_size
    assert sidecar["retrieved_at"].endswith("Z")
    assert sidecar["cache_path"].endswith(".mp4")


def test_downloaded_bytes_are_validated(tmp_path, monkeypatch):
    install_transport(
        monkeypatch,
        {"videos": [video(71, 1920, 1080, 6.0, (
            "hd", "video/mp4", 1920, 1080,
            "https://videos.pexels.com/video-files/71/hd.mp4"))]},
        media=b"<html>this is not a video</html>",
    )
    provider = make_provider(tmp_path)
    with pytest.raises(AssetResolutionError, match="not an ISO-BMFF/MP4 container"):
        provider.resolve(requirement(AssetKind.VIDEO), "scene-017", run_dir=tmp_path / "run")
    assert not (tmp_path / "run" / "assets" / "scene-017.mp4").exists()
    assert list((tmp_path / "cache").glob("*.mp4")) == []  # rejected bytes are not cached


def test_empty_download_fails(tmp_path, monkeypatch):
    install_transport(monkeypatch, {"photos": [photo(81, 2200, 1400)]}, media=b"")
    provider = make_provider(tmp_path)
    with pytest.raises(AssetResolutionError, match="empty"):
        provider.resolve(requirement(AssetKind.IMAGE), "scene-018", run_dir=tmp_path / "run")


def test_cache_reuse_avoids_second_download(tmp_path, monkeypatch):
    calls = install_transport(
        monkeypatch,
        {"videos": [video(91, 1920, 1080, 7.0, (
            "hd", "video/mp4", 1920, 1080,
            "https://videos.pexels.com/video-files/91/hd.mp4"))]},
        media=MP4_BYTES,
    )
    provider = make_provider(tmp_path)
    provider.resolve(requirement(AssetKind.VIDEO), "scene-019", run_dir=tmp_path / "run-a")
    downloads_before = [c["url"] for c in calls if not c["url"].startswith(API_BASE)]
    provider.resolve(requirement(AssetKind.VIDEO), "scene-020", run_dir=tmp_path / "run-b")
    downloads_after = [c["url"] for c in calls if not c["url"].startswith(API_BASE)]
    assert len(downloads_before) == 1
    assert downloads_after == downloads_before  # the second run reused the cache
    assert (tmp_path / "run-b" / "assets" / "scene-020.mp4").is_file()


# ---- provider selection -----------------------------------------------------


def test_selection_defaults_to_fixture_provider():
    selected = select_asset_provider(Config.from_env(env={}))
    assert isinstance(selected, FileBackedAssetProvider)
    assert not isinstance(selected, PexelsVisualProvider)


def test_selection_honors_assets_dir_for_default_provider():
    selected = select_asset_provider(Config.from_env(env={}), assets_dir="some/fixtures")
    assert isinstance(selected, FileBackedAssetProvider)
    assert str(selected.fixture_dir).replace("\\", "/") == "some/fixtures"


def test_pexels_selected_only_when_configured():
    configured = select_asset_provider(Config(pexels_api_key=API_KEY))
    assert isinstance(configured, PexelsVisualProvider)

    # an orientation alone is NOT enough — the key enables the real provider
    assert isinstance(
        select_asset_provider(Config(pexels_orientation="portrait")),
        FileBackedAssetProvider,
    )


def test_pexels_provider_fits_the_adapter_convention(tmp_path):
    """The real provider honors the P0 adapter convention (name + health)."""
    from ayce.adapters import Adapter

    provider = make_provider(tmp_path)
    assert isinstance(provider, Adapter)
    assert provider.name == "pexels-api"
    assert provider.health().name == "pexels-api"


def test_provider_configuration_is_validated():
    with pytest.raises(ConfigError, match="AYCE_PEXELS_ORIENTATION"):
        Config.from_env(env={"AYCE_PEXELS_ORIENTATION": "diagonal"})
    with pytest.raises(ConfigError, match="AYCE_PEXELS_MIN_VIDEO_DURATION_S"):
        Config.from_env(env={"AYCE_PEXELS_MIN_VIDEO_DURATION_S": "0"})
    with pytest.raises(ConfigError, match="AYCE_PEXELS_MIN_VIDEO_DURATION_S"):
        Config.from_env(env={"AYCE_PEXELS_MIN_VIDEO_DURATION_S": "soon"})

    configured = Config.from_env(env={
        "AYCE_PEXELS_API_KEY": API_KEY,
        "AYCE_PEXELS_ORIENTATION": "PORTRAIT",
        "AYCE_PEXELS_MIN_VIDEO_DURATION_S": "6.5",
    })
    provider = PexelsVisualProvider(configured, cache_dir=Path("unused-cache"))
    assert provider.orientation == "portrait"          # normalized
    assert provider.minimum_resolution() == (1080, 1920)
    assert provider.min_video_duration_s == 6.5


# ---- opt-in live smoke test (real API; never in the normal suite) -----------

@pytest.mark.live
def test_live_pexels_resolves_one_real_asset(tmp_path):
    """Real Pexels retrieval (opt-in; needs a configured AYCE_PEXELS_API_KEY).

    Verifies the ACTUAL downloaded file, its resolution, its provenance
    sidecar and its content hash. Skips — never fabricates — when no
    credential is configured.
    """
    if not os.environ.get("AYCE_PEXELS_API_KEY", "").strip():
        pytest.skip("AYCE_PEXELS_API_KEY is not configured")
    config = Config.from_env()
    provider = PexelsVisualProvider(config, cache_dir=tmp_path / "cache")
    assert provider.health().available, provider.health().detail

    asset = provider.resolve(
        requirement(AssetKind.IMAGE, "mountain sunrise landscape"),
        "scene-live-001",
        run_dir=tmp_path / "run",
    )
    resolved = tmp_path / "run" / asset.path
    assert resolved.is_file() and resolved.stat().st_size > 0
    assert hashlib.sha256(resolved.read_bytes()).hexdigest() == asset.sha256

    sidecar = json.loads(
        (tmp_path / "run" / "assets" / "scene-live-001.provenance.json").read_text("utf-8")
    )
    minimum_width, minimum_height = provider.minimum_resolution()
    assert sidecar["width"] >= minimum_width
    assert sidecar["height"] >= minimum_height
    assert sidecar["asset_id"] == asset.provenance.source_ref
    assert sidecar["source_url"].startswith("https://")
    assert sidecar["sha256"] == asset.sha256