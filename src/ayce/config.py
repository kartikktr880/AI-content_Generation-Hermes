"""Centralized configuration.

Rules:
- All environment access goes through this module (prefix ``AYCE_``).
- Secrets are never hardcoded; they come from the environment or a
  git-ignored ``.env`` file (see ``.env.example``).
- Invalid configuration raises :class:`ConfigError` explicitly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_PREFIX = "AYCE_"

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

#: Frame orientations the real visual provider may require for production
#: assets (the Scene Contract itself carries no orientation field).
VALID_PEXELS_ORIENTATIONS = ("landscape", "portrait", "square")


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid."""


def load_env_file(path: str | os.PathLike[str], *, override: bool = False) -> dict[str, str]:
    """Parse a minimal ``.env`` file (``KEY=VALUE`` lines, ``#`` comments).

    Existing environment variables win unless ``override=True``.
    Returns the variables that were applied. Missing file -> empty dict
    (a ``.env`` file is optional by design).
    """
    env_path = Path(path)
    applied: dict[str, str] = {}
    if not env_path.is_file():
        return applied
    for lineno, raw in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            raise ConfigError(f"{env_path}:{lineno}: empty variable name")
        if override or key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


@dataclass(frozen=True)
class Config:
    """Process-wide configuration for the engine."""

    log_level: str = "INFO"
    data_dir: Path = Path("data")
    #: Optional explicit ffmpeg/ffprobe executable paths. When unset, the
    #: renderer falls back to PATH lookup. Never hardcode machine paths
    #: in source — set AYCE_FFMPEG_PATH / AYCE_FFPROBE_PATH instead.
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    #: Optional Piper TTS configuration (P6.6 selection; GPL-3.0 — Piper is
    #: invoked as an EXTERNAL CLI tool in its own environment, never imported
    #: into AYCE). All keys optional; when AYCE_PIPER_MODEL is unset the
    #: deterministic file-backed narration provider remains the default.
    #: AYCE_PIPER_PYTHON: python executable of the environment that has
    #:   piper-tts installed (may be a name resolved via PATH or an absolute path).
    #: AYCE_PIPER_MODEL: path to the .onnx voice model.
    #: AYCE_PIPER_CONFIG: optional path to the model's .onnx.json config.
    #: AYCE_PIPER_LENGTH_SCALE: optional speech-rate control (>0; Piper default 1.0).
    #: AYCE_PIPER_LICENSE: explicit voice-model license string recorded in
    #:   narration provenance (never guessed — stays unset when unverified).
    piper_python: str | None = None
    piper_model: str | None = None
    piper_config: str | None = None
    piper_length_scale: float | None = None
    piper_license: str | None = None
    #: Optional yt-dlp executable for the Stage 2 research worker (the
    #: external CLI is invoked as a subprocess, Piper/FFmpeg convention).
    #: When unset, PATH lookup applies (AYCE_YTDLP_PATH).
    ytdlp_path: str | None = None
    #: Real visual-asset provider (P8 selection; Pexels REST API behind the
    #: existing AssetProvider boundary). The API key is a SECRET — it comes
    #: from the environment or the git-ignored .env only and is never logged.
    #: When unset the deterministic file-backed fixture provider remains the
    #: default (verified behavior unchanged).
    #: AYCE_PEXELS_API_KEY: Pexels API key (enables the real provider).
    #: AYCE_PEXELS_ORIENTATION: landscape | portrait | square (production
    #:   requirement; the Scene Contract carries no orientation field).
    #: AYCE_PEXELS_MIN_VIDEO_DURATION_S: minimum clip length for video scenes.
    #: AYCE_PEXELS_CACHE_DIR: content cache for downloaded media (defaults to
    #:   <data_dir>/assets_cache/pexels).
    pexels_api_key: str | None = None
    pexels_orientation: str = "landscape"
    pexels_min_video_duration_s: float = 4.0
    pexels_cache_dir: str | None = None
    #: Real narration provider (P8 selection; Kokoro-82M, Apache-2.0 weights
    #: from the OFFICIAL repo, executed locally — no paid API). Strictly
    #: opt-in: AYCE_KOKORO_VOICE unset ⇒ the file-backed fixture provider
    #: remains the default and Kokoro is never imported.
    #: AYCE_KOKORO_VOICE: Kokoro voice name (e.g. af_heart, hf_alpha); its
    #:   first letter is the language code (a/b = English, h = Hindi).
    #: AYCE_KOKORO_SPEED: speaking-rate multiplier (>0; Kokoro default 1.0).
    #: AYCE_KOKORO_REPO_ID: model repository id (default hexgrad/Kokoro-82M).
    #: AYCE_KOKORO_LICENSE: explicit weights/voice license string recorded in
    #:   provenance (never guessed — stays unset when unverified).
    #: AYCE_KOKORO_CACHE_DIR: synthesis cache (defaults to
    #:   <data_dir>/narration_cache/kokoro).
    kokoro_voice: str | None = None
    kokoro_speed: float = 1.0
    kokoro_repo_id: str = "hexgrad/Kokoro-82M"
    kokoro_license: str | None = None
    kokoro_cache_dir: str | None = None

    @property
    def resolved_data_dir(self) -> Path:
        """Absolute runtime data directory (logs, run state, artifacts)."""
        p = Path(self.data_dir)
        return p if p.is_absolute() else Path.cwd() / p

    def require(self, key: str, env: dict[str, str] | None = None) -> str:
        """Return a required ``AYCE_<key>`` value or raise ConfigError.

        Future stages (e.g. providers needing API keys) must use this
        instead of reading ``os.environ`` directly.
        """
        source = os.environ if env is None else env
        value = source.get(ENV_PREFIX + key)
        if value is None or value == "":
            raise ConfigError(f"missing required configuration: {ENV_PREFIX}{key}")
        return value

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        *,
        env_file: str | os.PathLike[str] | None = ".env",
    ) -> "Config":
        """Build configuration from the environment.

        When reading the real environment (``env is None``), an optional
        ``.env`` file is loaded first (real environment always wins).
        Passing an explicit dict skips ``.env`` loading (used by tests).
        """
        if env is None:
            if env_file is not None:
                load_env_file(env_file)
            source: dict[str, str] = os.environ
        else:
            source = env

        log_level = source.get(ENV_PREFIX + "LOG_LEVEL", "INFO").strip().upper()
        if log_level not in VALID_LOG_LEVELS:
            raise ConfigError(
                f"invalid {ENV_PREFIX}LOG_LEVEL={log_level!r}; "
                f"expected one of: {', '.join(VALID_LOG_LEVELS)}"
            )

        data_dir_raw = source.get(ENV_PREFIX + "DATA_DIR", "data").strip()
        if not data_dir_raw:
            raise ConfigError(f"{ENV_PREFIX}DATA_DIR must not be empty")

        length_scale_raw = source.get(ENV_PREFIX + "PIPER_LENGTH_SCALE")
        piper_length_scale: float | None = None
        if length_scale_raw:
            try:
                piper_length_scale = float(length_scale_raw)
            except ValueError:
                raise ConfigError(
                    f"invalid {ENV_PREFIX}PIPER_LENGTH_SCALE={length_scale_raw!r}; must be a number"
                ) from None
            if piper_length_scale <= 0:
                raise ConfigError(
                    f"invalid {ENV_PREFIX}PIPER_LENGTH_SCALE={length_scale_raw!r}; must be > 0"
                )

        orientation_raw = source.get(ENV_PREFIX + "PEXELS_ORIENTATION", "landscape").strip().lower()
        if orientation_raw not in VALID_PEXELS_ORIENTATIONS:
            raise ConfigError(
                f"invalid {ENV_PREFIX}PEXELS_ORIENTATION={orientation_raw!r}; "
                f"expected one of: {', '.join(VALID_PEXELS_ORIENTATIONS)}"
            )
        pexels_orientation = orientation_raw

        min_duration_raw = source.get(ENV_PREFIX + "PEXELS_MIN_VIDEO_DURATION_S")
        pexels_min_video_duration_s = 4.0
        if min_duration_raw:
            try:
                pexels_min_video_duration_s = float(min_duration_raw)
            except ValueError:
                raise ConfigError(
                    f"invalid {ENV_PREFIX}PEXELS_MIN_VIDEO_DURATION_S={min_duration_raw!r}; "
                    f"must be a number of seconds"
                ) from None
            if pexels_min_video_duration_s <= 0:
                raise ConfigError(
                    f"invalid {ENV_PREFIX}PEXELS_MIN_VIDEO_DURATION_S={min_duration_raw!r}; must be > 0"
                )

        speed_raw = source.get(ENV_PREFIX + "KOKORO_SPEED")
        kokoro_speed = 1.0
        if speed_raw:
            try:
                kokoro_speed = float(speed_raw)
            except ValueError:
                raise ConfigError(
                    f"invalid {ENV_PREFIX}KOKORO_SPEED={speed_raw!r}; must be a number"
                ) from None
            if kokoro_speed <= 0:
                raise ConfigError(
                    f"invalid {ENV_PREFIX}KOKORO_SPEED={speed_raw!r}; must be > 0"
                )

        return cls(
            log_level=log_level,
            data_dir=Path(data_dir_raw),
            ffmpeg_path=source.get(ENV_PREFIX + "FFMPEG_PATH") or None,
            ffprobe_path=source.get(ENV_PREFIX + "FFPROBE_PATH") or None,
            piper_python=source.get(ENV_PREFIX + "PIPER_PYTHON") or None,
            piper_model=source.get(ENV_PREFIX + "PIPER_MODEL") or None,
            piper_config=source.get(ENV_PREFIX + "PIPER_CONFIG") or None,
            piper_length_scale=piper_length_scale,
            piper_license=source.get(ENV_PREFIX + "PIPER_LICENSE") or None,
            ytdlp_path=source.get(ENV_PREFIX + "YTDLP_PATH") or None,
            pexels_api_key=source.get(ENV_PREFIX + "PEXELS_API_KEY") or None,
            pexels_orientation=pexels_orientation,
            pexels_min_video_duration_s=pexels_min_video_duration_s,
            pexels_cache_dir=source.get(ENV_PREFIX + "PEXELS_CACHE_DIR") or None,
            kokoro_voice=source.get(ENV_PREFIX + "KOKORO_VOICE") or None,
            kokoro_speed=kokoro_speed,
            kokoro_repo_id=source.get(ENV_PREFIX + "KOKORO_REPO_ID") or "hexgrad/Kokoro-82M",
            kokoro_license=source.get(ENV_PREFIX + "KOKORO_LICENSE") or None,
            kokoro_cache_dir=source.get(ENV_PREFIX + "KOKORO_CACHE_DIR") or None,
        )
