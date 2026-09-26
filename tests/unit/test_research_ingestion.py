"""Stage 2 ingestion tests — normalization, malformed/partial entries,
deterministic subprocess invocation (argv list, never a shell)."""

import json

from ayce.config import Config
from ayce.research.ingestion import (
    _normalize_entry,
    fetch_video_metadata,
    ingest,
    is_rate_limited,
    search_videos,
)
from ayce.research.models import VIDEO_ID_RE


def _entry(video_id="dQw4w9WgXcQ", **overrides):
    entry = {
        "id": video_id,
        "title": "A great video",
        "channel": "Example Channel",
        "channel_id": "UC123",
        "view_count": 42000,
        "duration": 213.0,
        "upload_date": "20260115",
        "url": f"https://www.youtube.com/watch?v={video_id}",
    }
    entry.update({k: v for k, v in overrides.items() if v is not ...})
    return entry


def _patch_ytdlp(monkeypatch, payload_by_call: list[dict], stdout: str = "", rc: int = 0):
    """Deterministic fake of ayce.research.ingestion.subprocess.run."""
    calls: list[dict] = []

    def fake_run(argv, *args, **kwargs):
        calls.append({"argv": list(argv), "kwargs": kwargs})
        idx = min(len(calls) - 1, len(payload_by_call) - 1)
        spec = payload_by_call[idx] if payload_by_call else {}
        return type("_P", (), {
            "returncode": spec.get("rc", rc),
            "stdout": spec.get("stdout", stdout),
            "stderr": spec.get("stderr", ""),
        })()

    monkeypatch.setattr("ayce.research.ingestion.subprocess.run", fake_run)
    return calls


def test_normalized_metadata_from_flat_entry():
    record = _normalize_entry(_entry())
    assert record is not None
    assert record.video_id == "dQw4w9WgXcQ"
    assert record.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert record.title == "A great video"
    assert record.channel == "Example Channel"
    assert record.channel_id == "UC123"
    assert record.published_at == "2026-01-15"  # YYYYMMDD → ISO
    assert record.view_count == 42000
    assert record.duration_s == 213


def test_partial_entries_normalize_truthfully():
    record = _normalize_entry({"id": "abcdefghijk", "title": None})
    assert record is not None
    assert record.title is None
    assert record.view_count is None
    assert record.published_at is None
    assert record.has_manual_subtitles is False


def test_malformed_entries_are_skipped():
    assert _normalize_entry({"no_id": True}) is None
    assert _normalize_entry({"id": "bad id with spaces"}) is None
    assert _normalize_entry("not a dict") is None
    assert _normalize_entry(None) is None
    assert VIDEO_ID_RE.match("dQw4w9WgXcQ")


def test_search_argv_template_deterministic(monkeypatch):
    payload = json.dumps({"id": "playlist", "entries": [_entry()]})
    calls = _patch_ytdlp(monkeypatch, [{"stdout": payload}])
    videos, warnings, rate_limited = search_videos("yt-dlp.exe", "outlier formats", 5)
    assert rate_limited is False and warnings == []
    assert len(videos) == 1
    argv = calls[0]["argv"]
    assert argv[0] == "yt-dlp.exe"
    assert "--dump-single-json" in argv and "--flat-playlist" in argv
    assert argv[-1] == "ytsearch5:outlier formats"
    assert calls[0]["kwargs"].get("shell") is False
    # deterministic: identical call → identical argv
    calls2 = _patch_ytdlp(monkeypatch, [{"stdout": payload}])
    search_videos("yt-dlp.exe", "outlier formats", 5)
    assert calls2[0]["argv"] == argv


def test_full_metadata_adds_caption_availability(monkeypatch):
    full = _entry(subtitles={"en": [{"url": "x"}]}, automatic_captions={"en": []})
    calls = _patch_ytdlp(monkeypatch, [{"stdout": json.dumps(full)}])
    from ayce.research.ingestion import VideoRecord
    thin = VideoRecord(video_id="dQw4w9WgXcQ", url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    record, warning, rate_limited = fetch_video_metadata("yt-dlp.exe", thin)
    assert rate_limited is False and warning is None
    assert record is not None
    assert record.has_manual_subtitles is True
    assert record.has_auto_captions is True
    assert "--dump-single-json" in calls[0]["argv"]


def test_rate_limited_detection(monkeypatch):
    calls = _patch_ytdlp(monkeypatch, [{"rc": 1, "stderr": "HTTP Error 429: Too Many Requests"}])
    videos, warnings, rate_limited = search_videos("yt-dlp.exe", "q", 3)
    assert rate_limited is True
    assert videos == []
    assert is_rate_limited("Sign in to confirm you're not a bot")


def test_ingest_end_to_end_with_fake_subprocess(monkeypatch, tmp_path):
    search_payload = json.dumps({"id": "playlist", "entries": [_entry(), {"id": "bad id"}]})
    full = _entry(description="A useful description")
    calls = _patch_ytdlp(monkeypatch, [
        {"stdout": search_payload},        # search discovery
        {"stdout": json.dumps(full)},      # metadata enrichment
    ])
    config = Config(data_dir=tmp_path)
    outcome = ingest(config, query="test query", max_videos=3)
    # the skipped malformed entry is a warning → truthful PARTIAL, not SUCCESS
    assert outcome.status == "PARTIAL"
    assert outcome.rate_limited is False
    assert len(outcome.videos) == 1  # malformed entry skipped, warning recorded
    assert outcome.counts["discovered"] == 1  # only valid normalized entries count
    assert outcome.counts["collected"] == 1
    assert any("malformed" in w for w in outcome.warnings)
    # bounded: exactly one discovery call + one metadata call
    assert len(calls) == 2


def test_ingest_source_unavailable_when_no_ytdlp(tmp_path, monkeypatch):
    monkeypatch.setattr("ayce.research.ingestion.resolve_ytdlp", lambda config: None)
    config = Config(data_dir=tmp_path)
    outcome = ingest(config, query="test query")
    assert outcome.status == "SOURCE_UNAVAILABLE"
    assert outcome.videos == []