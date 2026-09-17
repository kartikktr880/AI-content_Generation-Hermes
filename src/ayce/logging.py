"""Minimal structured logging (stdlib only, JSON lines).

Every record carries at least: ``ts``, ``level``, ``message``.
Contextual identity fields (``run_id``, ``job_id``, ``stage``, ``artifact_id``)
are attached via :meth:`StructuredLogger.bind`. Errors are serialized as
``error: {type, message}``. Values whose keys look secret-like are
redacted before serialization.
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

# Keys whose values must never reach a log sink.
_REDACT_MARKERS = ("api_key", "apikey", "authorization", "password", "secret", "token", "credential")

_REDACTED = "[REDACTED]"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _redact(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        lowered = key.lower()
        if any(marker in lowered for marker in _REDACT_MARKERS):
            out[key] = _REDACTED
        else:
            out[key] = value
    return out


class StructuredLogger:
    """JSON-lines logger writing to stderr and optionally a file."""

    def __init__(
        self,
        level: str = "INFO",
        *,
        stream: Any = None,
        file_path: str | Path | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        if level not in _LEVELS:
            raise ValueError(f"unknown log level: {level!r}; expected one of {', '.join(_LEVELS)}")
        self._level = _LEVELS[level]
        self._level_name = level
        self._stream = stream if stream is not None else sys.stderr
        self._file_path = Path(file_path) if file_path else None
        self._lock = threading.Lock()
        self._context = dict(context or {})

    def bind(self, **context: Any) -> "StructuredLogger":
        """Return a child logger with additional bound identity fields."""
        merged = {**self._context, **context}
        return StructuredLogger(
            self._level_name, stream=self._stream, file_path=self._file_path, context=merged
        )

    def _emit(
        self,
        level_name: str,
        message: str,
        *,
        error: BaseException | None = None,
        fields: dict[str, Any] | None = None,
    ) -> None:
        if _LEVELS[level_name] < self._level:
            return
        record: dict[str, Any] = {
            "ts": _utcnow_iso(),
            "level": level_name,
            "message": message,
        }
        record.update(_redact({**self._context, **(fields or {})}))
        if error is not None:
            record["error"] = {"type": type(error).__name__, "message": str(error)}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            self._stream.write(line + "\n")
            try:
                self._stream.flush()
            except (ValueError, OSError):
                pass  # closed stream during shutdown; stderr still has the line
            if self._file_path is not None:
                self._file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._file_path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")

    def debug(self, message: str, *, error: BaseException | None = None, **fields: Any) -> None:
        self._emit("DEBUG", message, error=error, fields=fields)

    def info(self, message: str, *, error: BaseException | None = None, **fields: Any) -> None:
        self._emit("INFO", message, error=error, fields=fields)

    def warning(self, message: str, *, error: BaseException | None = None, **fields: Any) -> None:
        self._emit("WARNING", message, error=error, fields=fields)

    def error(self, message: str, *, error: BaseException | None = None, **fields: Any) -> None:
        self._emit("ERROR", message, error=error, fields=fields)


def create_logger(config: Config, **context: Any) -> StructuredLogger:
    """Create the standard engine logger (stderr + ``<data_dir>/logs/ayce.jsonl``)."""
    file_path = config.resolved_data_dir / "logs" / "ayce.jsonl"
    return StructuredLogger(
        level=config.log_level, file_path=file_path, context={"component": "ayce", **context}
    )
