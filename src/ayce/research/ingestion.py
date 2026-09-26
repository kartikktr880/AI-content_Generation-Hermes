"""Stage 2 — YouTube ingestion via the existing quota-free yt-dlp path.

yt-dlp is already installed on this machine as an EXTERNAL CLI tool.
Exactly like the verified Piper/FFmpeg boundaries, it is invoked as a
subprocess with an explicit argv list (never a shell, never imported,
never an AYCE dependency). The caller (Hermes via the Director MCP)
can never influence argv, cwd, environment, or output paths.

Capability:
- search discovery (``ytsearchN:<query>``) and per-channel recent-video
  discovery (``.../videos`` flat playlist) — no YouTube Data API key,
  no quota;
- per-video full metadata fetch (view count, publication timestamp,
  channel identity, description, caption availability);
- normalization into :class:`VideoRecord` with truthful ``None`` values
  for anything yt-dlp did not report. Malformed/partial entries are
  skipped with a recorded warning — one bad video never fails the run.

Every spawn is bounded by a timeout and classified; HTTP 429 / bot-check
stderr signatures are surfaced as an explicit rate-limit flag so the
worker can enter RATE_LIMITED mode instead of failing vaguely.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..config import Config
from .models import VIDEO_ID_RE

__all__ = [
    "YTDLP_TIMEOUT_S",
    "METADATA_TIMEOUT_S",
    "VideoRecord",
    "IngestionOutcome",
    "resolve_ytdlp",
    "ytdlp_version",
    "is_rate_limited",
    "search_videos",
    "channel_recent_videos",
    "fetch_video_metadata",
    "ingest",
]

#: Bounded discovery runtime (flat playlist listings).
YTDLP_TIMEOUT_S = 180
#: Bounded per-video metadata runtime.
METADATA_TIMEOUT_S = 90

_RATE_LIMIT_MARKERS = ("429", "Sign in to confirm", "confirm you're not a bot")


class VideoRecord(BaseModel):
    """Normalized internal video metadata (``None`` = truthfully unknown)."""

    model_config = ConfigDict(extra="forbid")

    video_id: str
    url: str
    title: str | None = None
    channel: str | None = None
    channel_id: str | None = None
    published_at: str | None = None  # ISO-8601 date
    view_count: int | None = None
    duration_s: int | None = None
    description_excerpt: str | None = None
    has_manual_subtitles: bool = False
    has_auto_captions: bool = False


class IngestionOutcome(BaseModel):
    """Truthful ingestion report for the worker."""

    model_config = ConfigDict(extra="forbid")

    videos: list[VideoRecord] = Field(default_factory=list)
    status: str = "SUCCESS"  # SUCCESS | PARTIAL | RATE_LIMITED | SOURCE_UNAVAILABLE
    rate_limited: bool = False
    warnings: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


def _run(argv: list[str], timeout_s: float) -> tuple[int, str, str]:
    """Bounded subprocess execution — explicit argv list, NEVER a shell."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, shell=False,
            timeout=timeout_s, stdin=subprocess.DEVNULL, close_fds=True,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout_s:g}s"
    except OSError as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"
    return (
        proc.returncode,
        proc.stdout or "",
        (proc.stderr or "")[-2000:],
    )


def resolve_ytdlp(config: Config) -> str | None:
    """Locate the yt-dlp executable (config override, then PATH)."""
    if config.ytdlp_path:
        resolved = shutil.which(config.ytdlp_path)
        if resolved:
            return resolved
        if Path(config.ytdlp_path).is_file():
            return config.ytdlp_path
        return None
    return shutil.which("yt-dlp")


def ytdlp_version(ytdlp_path: str) -> str | None:
    rc, stdout, _ = _run([ytdlp_path, "--version"], 30)
    return stdout.strip() if rc == 0 else None


def is_rate_limited(stderr: str) -> bool:
    return any(marker in (stderr or "") for marker in _RATE_LIMIT_MARKERS)


def _normalize_timestamp(value: object) -> str | None:
    """yt-dlp unix timestamp or ``YYYYMMDD`` string → ISO date."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d")
        text = str(value).strip()
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").strftime("%Y-%m-%d")
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _normalize_entry(entry: object) -> VideoRecord | None:
    """Normalize one raw yt-dlp JSON entry; malformed entries are skipped."""
    if not isinstance(entry, dict):
        return None
    raw_id = entry.get("id")
    if not isinstance(raw_id, str) or not VIDEO_ID_RE.match(raw_id):
        return None
    view_count = entry.get("view_count")
    if isinstance(view_count, bool) or not isinstance(view_count, int):
        view_count = (
            int(view_count)
            if isinstance(view_count, float) and view_count.is_integer()
            else None
        )
    duration = entry.get("duration")
    duration = int(duration) if isinstance(duration, (int, float)) and duration >= 0 else None
    title = entry.get("title")
    uploader = entry.get("channel") or entry.get("uploader")
    description = entry.get("description")
    channel_id = entry.get("channel_id")
    published = _normalize_timestamp(
        entry.get("timestamp") or entry.get("release_timestamp") or entry.get("upload_date")
    )
    return VideoRecord(
        video_id=raw_id,
        url=entry.get("webpage_url") or f"https://www.youtube.com/watch?v={raw_id}",
        title=title.strip() if isinstance(title, str) and title.strip() else None,
        channel=uploader.strip() if isinstance(uploader, str) and uploader.strip() else None,
        channel_id=channel_id if isinstance(channel_id, str) else None,
        published_at=published,
        view_count=view_count,
        duration_s=duration,
        description_excerpt=(
            description.strip()[:200]
            if isinstance(description, str) and description.strip()
            else None
        ),
        has_manual_subtitles=bool(entry.get("subtitles")),
        has_auto_captions=bool(entry.get("automatic_captions")),
    )


def _parse_json_output(stdout: str) -> object | None:
    import json

    try:
        return json.loads(stdout.strip())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def channel_recent_videos(
    ytdlp_path: str, channel: str, limit: int
) -> tuple[list[VideoRecord], list[str], bool]:
    """Recent videos of one channel via flat playlist (bounded)."""
    target = channel.strip()
    if target.startswith("http"):
        url = target
    elif target.startswith("@"):
        url = f"https://www.youtube.com/{target}/videos"
    else:
        url = f"https://www.youtube.com/@{target}/videos"
    argv = [
        ytdlp_path, "--dump-single-json", "--flat-playlist",
        "--no-warnings", "--playlist-items", f"1:{max(1, limit)}", url,
    ]
    rc, stdout, stderr = _run(argv, YTDLP_TIMEOUT_S)
    if rc != 0:
        return [], [f"channel listing failed for {target!r}: {stderr[-200:] or 'rc=' + str(rc)}"], is_rate_limited(stderr)
    data = _parse_json_output(stdout)
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return [], [f"channel listing unparseable for {target!r}"], False
    videos, warnings = [], []
    for entry in entries:
        record = _normalize_entry(entry)
        if record is None:
            warnings.append(f"skipped malformed channel entry for {target!r}")
        else:
            videos.append(record)
    return videos, warnings, False


def search_videos(ytdlp_path: str, query: str, limit: int) -> tuple[list[VideoRecord], list[str], bool]:
    """YouTube search via the quota-free ``ytsearchN:`` flat path."""
    argv = [
        ytdlp_path, "--dump-single-json", "--flat-playlist",
        "--no-warnings", f"ytsearch{max(1, limit)}:{query.strip()}",
    ]
    rc, stdout, stderr = _run(argv, YTDLP_TIMEOUT_S)
    if rc != 0:
        return [], [f"search failed for {query!r}: {stderr[-200:] or 'rc=' + str(rc)}"], is_rate_limited(stderr)
    data = _parse_json_output(stdout)
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return [], [f"search output unparseable for {query!r}"], False
    videos, warnings = [], []
    for entry in entries:
        record = _normalize_entry(entry)
        if record is None:
            warnings.append("skipped malformed search entry")
        else:
            videos.append(record)
    return videos, warnings, False


def fetch_video_metadata(
    ytdlp_path: str, video: VideoRecord
) -> tuple[VideoRecord | None, str | None, bool]:
    """Full per-video metadata (one bounded yt-dlp call). Returns
    ``(record | None, warning | None, rate_limited)``."""
    argv = [ytdlp_path, "--dump-single-json", "--no-warnings", video.url]
    rc, stdout, stderr = _run(argv, METADATA_TIMEOUT_S)
    if rc != 0:
        return None, f"metadata fetch failed for {video.video_id}: {stderr[-200:] or 'rc=' + str(rc)}", is_rate_limited(stderr)
    data = _parse_json_output(stdout)
    if not isinstance(data, dict):
        return None, f"metadata output unparseable for {video.video_id}", False
    record = _normalize_entry(data)
    if record is None:
        return None, f"metadata normalization failed for {video.video_id}", False
    # keep the discovery-side identity when full metadata is thinner
    merged = video.model_copy(update={
        k: getattr(record, k)
        for k in ("title", "channel", "channel_id", "published_at", "view_count",
                  "duration_s", "description_excerpt", "has_manual_subtitles",
                  "has_auto_captions")
        if getattr(record, k) is not None
    })
    return merged, None, False


def ingest(
    config: Config,
    *,
    target_channels: list[str] | None = None,
    query: str | None = None,
    max_videos: int = 6,
) -> IngestionOutcome:
    """Bounded discovery + metadata collection. Never raises; the outcome
    carries the truthful status (SUCCESS/PARTIAL/RATE_LIMITED/
    SOURCE_UNAVAILABLE), warnings and counts."""
    ytdlp_path = resolve_ytdlp(config)
    warnings: list[str] = []
    if ytdlp_path is None:
        return IngestionOutcome(status="SOURCE_UNAVAILABLE", warnings=[
            "yt-dlp executable not found (set AYCE_YTDLP_PATH or install yt-dlp)"
        ])

    candidates: list[VideoRecord] = []
    rate_limited = False
    channels_queried = searches = 0

    for channel in (target_channels or [])[:5]:
        channels_queried += 1
        videos, warns, rl = channel_recent_videos(ytdlp_path, channel, max_videos)
        rate_limited = rate_limited or rl
        warnings.extend(warns)
        candidates.extend(videos)

    if query and query.strip():
        searches = 1
        videos, warns, rl = search_videos(ytdlp_path, query, max_videos)
        rate_limited = rate_limited or rl
        warnings.extend(warns)
        candidates.extend(videos)

    # dedupe by video_id, preserving deterministic first-seen order
    seen: set[str] = set()
    deduped: list[VideoRecord] = []
    for video in candidates:
        if video.video_id in seen:
            continue
        seen.add(video.video_id)
        deduped.append(video)
    deduped = deduped[: max(1, min(max_videos, 12))]

    # bounded per-video metadata enrichment
    enriched: list[VideoRecord] = []
    for video in deduped:
        record, warn, rl = fetch_video_metadata(ytdlp_path, video)
        if rl:
            rate_limited = True
            warnings.append(f"rate-limited during metadata fetch for {video.video_id}; stopped enrichment")
            enriched.append(video)  # keep the thin discovery record
            break
        if warn:
            warnings.append(warn)
            enriched.append(video)
        else:
            enriched.append(record or video)

    if rate_limited and not enriched:
        status = "RATE_LIMITED"
    elif not enriched:
        status = "SOURCE_UNAVAILABLE"
    elif warnings or rate_limited:
        status = "PARTIAL"
    else:
        status = "SUCCESS"

    return IngestionOutcome(
        videos=enriched,
        status=status,
        rate_limited=rate_limited,
        warnings=warnings,
        counts={
            "channels_queried": channels_queried,
            "searches": searches,
            "discovered": len(candidates),
            "collected": len(enriched),
        },
    )