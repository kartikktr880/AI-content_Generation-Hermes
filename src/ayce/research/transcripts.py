"""Stage 2 — Transcript / opening-hook extraction (deterministic, bounded).

Captions are obtained through the same existing mechanism as ingestion:
the external yt-dlp CLI (``--write-subs --write-auto-subs --sub-format
json3/vtt``), invoked with an explicit argv list — never a shell. Only
the opening-hook window is kept; the full transcript NEVER leaves this
module and NEVER reaches Hermes.

Deterministic no-caption behavior (never a crash, never fabricated
content):

    hook_summary = "Captions unavailable"
    confidence   = lowered (0.2)

One video without captions degrades that candidate only — the research
job continues.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .models import CaptionsStatus

__all__ = [
    "HOOK_WINDOW_SECONDS",
    "HOOK_MAX_CHARS",
    "CAPTIONS_UNAVAILABLE_TEXT",
    "CONFIDENCE_MANUAL",
    "CONFIDENCE_AUTO",
    "CONFIDENCE_UNAVAILABLE",
    "CONFIDENCE_FAILED",
    "HookResult",
    "parse_json3",
    "parse_vtt",
    "extract_hook",
    "fetch_hook",
]

#: Default opening-hook window (seconds from publication start).
HOOK_WINDOW_SECONDS = 45.0
#: Hard bound on the retained hook text (full transcript never retained).
HOOK_MAX_CHARS = 600

CAPTIONS_UNAVAILABLE_TEXT = "Captions unavailable"

CONFIDENCE_MANUAL = 0.85
CONFIDENCE_AUTO = 0.70
CONFIDENCE_UNAVAILABLE = 0.20
CONFIDENCE_FAILED = 0.10

#: Bounded caption-download runtime.
_SUBTITLE_TIMEOUT_S = 120
_SUBTITLE_LANGS = "en.*,en"
_SUB_FORMAT = "json3/vtt"

_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class HookResult:
    """Truthful per-video hook extraction outcome."""

    hook_summary: str | None
    confidence: float
    status: CaptionsStatus
    lang: str | None = None
    captions_source: str | None = None  # "manual" | "auto"
    warnings: list[str] = field(default_factory=list)


def parse_json3(text: str) -> list[tuple[float, float, str]]:
    """Parse a YouTube ``json3`` caption track into (start, end, text)."""
    import json

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    cues: list[tuple[float, float, str]] = []
    for event in data.get("events") or []:
        if not isinstance(event, dict):
            continue
        start_ms = event.get("tStartMs")
        if not isinstance(start_ms, (int, float)):
            continue
        duration_ms = event.get("dDurationMs")
        end = (start_ms + duration_ms) / 1000.0 if isinstance(duration_ms, (int, float)) \
            else start_ms / 1000.0
        segs = event.get("segs") or []
        text_bits = [
            seg.get("utf8", "") for seg in segs if isinstance(seg, dict)
        ]
        joined = _WS_RE.sub(" ", "".join(text_bits)).strip()
        if joined:
            cues.append((start_ms / 1000.0, end, joined))
    return cues


_VTT_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})\.(\d{3})")


def _vtt_seconds(stamp: str) -> float | None:
    match = _VTT_TIME_RE.match(stamp.strip())
    if match is None:
        return None
    h, m, s, ms = (int(g) for g in match.groups())
    return h * 3600 + m * 60 + s + ms / 1000.0


def parse_vtt(text: str) -> list[tuple[float, float, str]]:
    """Minimal WebVTT parser → (start, end, text) cues."""
    cues: list[tuple[float, float, str]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        timing_line = None
        for ln in lines:
            if "-->" in ln:
                timing_line = ln
                break
        if timing_line is None:
            continue
        left, _, right = timing_line.partition("-->")
        start = _vtt_seconds(left.split()[0] if left.split() else left)
        end = _vtt_seconds(right.strip().split()[0] if right.strip().split() else right)
        if start is None or end is None:
            continue
        body = _WS_RE.sub(" ", " ".join(lines[lines.index(timing_line) + 1 :])).strip()
        if body:
            cues.append((start, end, body))
    return cues


def extract_hook(
    cues: list[tuple[float, float, str]], window: float = HOOK_WINDOW_SECONDS
) -> str:
    """Bounded opening hook: cue text within the first ``window`` seconds.

    Deterministic: cues are sorted by start time; text is
    whitespace-normalized and hard-bounded to :data:`HOOK_MAX_CHARS`.
    """
    ordered = sorted(cues, key=lambda c: (c[0], c[1], c[2]))
    parts: list[str] = []
    for start, _end, text in ordered:
        if start >= window:
            break
        parts.append(text)
    hook = _WS_RE.sub(" ", " ".join(parts)).strip()
    if len(hook) > HOOK_MAX_CHARS:
        hook = hook[: HOOK_MAX_CHARS - 1].rstrip() + "…"
    return hook


def _default_runner(argv: list[str], timeout_s: float) -> tuple[int, str, str]:
    """Real subprocess seam — explicit argv, never a shell."""
    import subprocess

    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, shell=False,
            timeout=timeout_s, stdin=subprocess.DEVNULL, close_fds=True,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout_s:g}s"
    except OSError as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"
    return proc.returncode, proc.stdout or "", (proc.stderr or "")[-2000:]


def _find_caption_file(directory: Path) -> tuple[Path | None, str | None, str | None]:
    """Locate the downloaded caption track. Prefers manual over auto,
    json3 over vtt; deterministic (sorted file listing)."""
    best: tuple[Path | None, str | None, str | None] = (None, None, None)
    for path in sorted(directory.iterdir()):
        if path.suffix not in (".json3", ".vtt"):
            continue
        stem_parts = path.stem.split(".")
        lang = stem_parts[1] if len(stem_parts) > 2 else None
        source = "manual" if ".json3" == path.suffix and len(stem_parts) > 2 else None
        # manual tracks were requested with --write-subs; auto with
        # --write-auto-subs. yt-dlp names both ``<id>.<lang>.<ext>``;
        # prefer json3 (precise timing), then vtt.
        if best[0] is None or (path.suffix == ".json3" and best[0].suffix != ".json3"):
            best = (path, lang, source)
    return best


def fetch_hook(
    ytdlp_path: str,
    url: str,
    video_id: str,
    *,
    has_manual_subtitles: bool | None = None,
    output_dir: Path | None = None,
    runner=None,
    window: float = HOOK_WINDOW_SECONDS,
) -> HookResult:
    """Download the caption track (bounded) and extract the opening hook.

    ``runner`` is a test seam: ``runner(argv, timeout_s) -> (rc, out, err)``.
    ``has_manual_subtitles`` comes from the already-fetched video metadata
    (``subtitles`` dict present); when unknown, the auto-generated
    confidence is used (conservative, never inflated).
    """
    run = runner or _default_runner
    tmp_ctx = None
    if output_dir is None:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="ayce-research-")
        work_dir = Path(tmp_ctx.name)
    else:
        work_dir = Path(output_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

    try:
        out_template = str(work_dir / f"{video_id}.%(ext)s")
        argv = [
            ytdlp_path,
            "--skip-download",
            "--no-warnings",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs", _SUBTITLE_LANGS,
            "--sub-format", _SUB_FORMAT,
            "-o", out_template,
            url,
        ]
        rc, _stdout, stderr = run(argv, _SUBTITLE_TIMEOUT_S)
        if rc != 0:
            warning = f"caption fetch failed for {video_id}: {stderr[-200:] or 'rc=' + str(rc)}"
            return HookResult(
                hook_summary=None,
                confidence=CONFIDENCE_FAILED,
                status=CaptionsStatus.EXTRACTION_FAILED,
                warnings=[warning],
            )

        path, lang, _source_hint = _find_caption_file(work_dir)
        if path is None:
            return HookResult(
                hook_summary=CAPTIONS_UNAVAILABLE_TEXT,
                confidence=CONFIDENCE_UNAVAILABLE,
                status=CaptionsStatus.CAPTIONS_UNAVAILABLE,
            )

        text = path.read_text(encoding="utf-8", errors="replace")
        cues = parse_json3(text) if path.suffix == ".json3" else parse_vtt(text)
        hook = extract_hook(cues, window=window)
        if not hook:
            return HookResult(
                hook_summary=CAPTIONS_UNAVAILABLE_TEXT,
                confidence=CONFIDENCE_UNAVAILABLE,
                status=CaptionsStatus.CAPTIONS_UNAVAILABLE,
                lang=lang,
                warnings=[f"caption track for {video_id} had no text inside the hook window"],
            )
        # Truthful confidence: manual tracks (metadata said ``subtitles``
        # present) are human-curated; everything else may be ASR output.
        if has_manual_subtitles:
            confidence, source, status = (
                CONFIDENCE_MANUAL, "manual", CaptionsStatus.AVAILABLE)
        else:
            confidence, source, status = (
                CONFIDENCE_AUTO, "auto", CaptionsStatus.AUTO_GENERATED)
        return HookResult(
            hook_summary=hook,
            confidence=confidence,
            status=status,
            lang=lang,
            captions_source=source,
        )
    except Exception as exc:  # one bad video must never fail the run
        return HookResult(
            hook_summary=None,
            confidence=CONFIDENCE_FAILED,
            status=CaptionsStatus.EXTRACTION_FAILED,
            warnings=[f"hook extraction error for {video_id}: {type(exc).__name__}: {exc}"],
        )
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()