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

        return cls(log_level=log_level, data_dir=Path(data_dir_raw))
