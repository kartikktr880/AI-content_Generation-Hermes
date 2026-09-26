"""Stage 2 transcript/hook tests — successful hook extraction, missing
captions, bounded hook output, deterministic subprocess invocation."""

import json

from ayce.research.models import CaptionsStatus
from ayce.research.transcripts import (
    CAPTIONS_UNAVAILABLE_TEXT,
    CONFIDENCE_UNAVAILABLE,
    HOOK_MAX_CHARS,
    HOOK_WINDOW_SECONDS,
    extract_hook,
    fetch_hook,
    parse_json3,
    parse_vtt,
)

_YTDLP = "yt-dlp.exe"
_URL = "https://www.youtube.com/watch?v=abc12345678"


def _json3(cues):
    return json.dumps({
        "events": [
            {"tStartMs": int(start * 1000), "dDurationMs": int(dur * 1000),
             "segs": [{"utf8": text}]}
            for start, dur, text in cues
        ]
    })


def _fake_runner(files: dict, calls: list | None = None):
    """Runner seam that 'downloads' caption files into the -o directory."""

    def run(argv, timeout_s):
        if calls is not None:
            calls.append({"argv": list(argv), "timeout_s": timeout_s})
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        assert "--skip-download" in argv  # never fetch media
        import os
        template = argv[argv.index("-o") + 1]
        out_dir = os.path.dirname(template)
        for name, content in files.items():
            with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
                fh.write(content)
        return 0, "", ""

    return run


def test_parse_json3_cues():
    cues = parse_json3(_json3([(0.0, 2.0, "Hello world"), (3.0, 2.0, "  second  cue ")]))
    assert cues == [(0.0, 2.0, "Hello world"), (3.0, 5.0, "second cue")]


def test_parse_vtt_cues():
    vtt = (
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nHello world\n\n"
        "00:00:03.000 --> 00:00:05.000\nsecond cue\n"
    )
    assert parse_vtt(vtt) == [(0.0, 2.0, "Hello world"), (3.0, 5.0, "second cue")]


def test_extract_hook_bounded_to_window():
    cues = [(float(s), 1.0, f"cue{s}") for s in range(0, 120, 5)]
    hook = extract_hook(cues, window=HOOK_WINDOW_SECONDS)
    assert "cue0" in hook and "cue40" in hook
    assert "cue45" not in hook and "cue100" not in hook


def test_extract_hook_hard_char_bound():
    cues = [(0.0, 1.0, "word " * 500)]
    assert len(extract_hook(cues)) <= HOOK_MAX_CHARS


def test_successful_hook_extraction(tmp_path):
    calls: list = []
    runner = _fake_runner({"abc12345678.en.json3": _json3(
        [(0.0, 2.0, "Most people get this wrong."), (2.0, 2.0, "Here is why.")])
    }, calls)
    result = fetch_hook(_YTDLP, _URL, "abc12345678",
                        has_manual_subtitles=True, output_dir=tmp_path, runner=runner)
    assert result.status is CaptionsStatus.AVAILABLE
    assert result.hook_summary == "Most people get this wrong. Here is why."
    assert result.confidence == 0.85
    assert result.captions_source == "manual"
    # deterministic subprocess invocation: one fixed argv, list form
    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert argv[:1] == [_YTDLP]
    assert "--write-subs" in argv and "--write-auto-subs" in argv


def test_missing_captions_is_deterministic_and_graceful(tmp_path):
    runner = _fake_runner({}, None)
    result = fetch_hook(_YTDLP, _URL, "abc12345678", output_dir=tmp_path, runner=runner)
    assert result.status is CaptionsStatus.CAPTIONS_UNAVAILABLE
    assert result.hook_summary == CAPTIONS_UNAVAILABLE_TEXT
    assert result.confidence == CONFIDENCE_UNAVAILABLE
    again = fetch_hook(_YTDLP, _URL, "abc12345678", output_dir=tmp_path, runner=runner)
    assert again.hook_summary == result.hook_summary


def test_caption_fetch_failure_does_not_raise(tmp_path):
    def boom(argv, timeout_s):
        return 1, "", "ERROR: something broke"
    result = fetch_hook(_YTDLP, _URL, "abc12345678", output_dir=tmp_path, runner=boom)
    assert result.status is CaptionsStatus.EXTRACTION_FAILED
    assert result.hook_summary is None
    assert result.confidence < CONFIDENCE_UNAVAILABLE
    assert result.warnings