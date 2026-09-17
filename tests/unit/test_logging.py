import io
import json

import pytest

from ayce.logging import StructuredLogger


def make_logger(tmp_path, **kwargs):
    stream = io.StringIO()
    log_file = tmp_path / "logs" / "test.jsonl"
    logger = StructuredLogger(level="DEBUG", stream=stream, file_path=log_file, **kwargs)
    return logger, stream, log_file


def test_record_has_required_fields(tmp_path):
    logger, stream, log_file = make_logger(tmp_path)
    logger.info("hello", key="value")
    record = json.loads(stream.getvalue().splitlines()[-1])
    assert record["message"] == "hello"
    assert record["level"] == "INFO"
    assert "ts" in record
    assert record["key"] == "value"
    # file sink got the same line
    assert json.loads(log_file.read_text(encoding="utf-8").splitlines()[-1])["message"] == "hello"


def test_bind_adds_identity_context(tmp_path):
    logger, stream, _ = make_logger(tmp_path)
    logger.bind(run_id="run-1", stage="research").warning("step")
    record = json.loads(stream.getvalue())
    assert record["run_id"] == "run-1"
    assert record["stage"] == "research"


def test_error_serialization(tmp_path):
    logger, stream, _ = make_logger(tmp_path)
    try:
        raise ValueError("boom")
    except ValueError as exc:
        logger.error("failed", error=exc)
    record = json.loads(stream.getvalue())
    assert record["error"] == {"type": "ValueError", "message": "boom"}


def test_secrets_are_redacted(tmp_path):
    logger, stream, _ = make_logger(tmp_path)
    logger.info("config", api_key="super-secret", password="hunter2", safe="plain")
    record = json.loads(stream.getvalue())
    assert record["api_key"] == "[REDACTED]"
    assert record["password"] == "[REDACTED]"
    assert record["safe"] == "plain"
    assert "super-secret" not in stream.getvalue()


def test_level_filtering(tmp_path):
    stream = io.StringIO()
    logger = StructuredLogger(level="WARNING", stream=stream)
    logger.debug("noisy")
    logger.info("chatty")
    logger.error("important")
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["level"] == "ERROR"


def test_invalid_level_raises(tmp_path):
    with pytest.raises(ValueError, match="unknown log level"):
        StructuredLogger(level="LOUD", stream=io.StringIO())
