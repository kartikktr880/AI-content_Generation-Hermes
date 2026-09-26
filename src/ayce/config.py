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
        )
