"""logging_setup 单测：文本 / JSON 格式与 log context。"""

import io
import json
import logging
import uuid

import pytest

from agent_runtime.log_context import log_context
from agent_runtime.logging_setup import (
    JsonFormatter,
    configure_logging,
    reset_logging_for_tests,
    resolve_log_format,
)


@pytest.fixture(autouse=True)
def _clean_logging(monkeypatch):
    reset_logging_for_tests()
    monkeypatch.delenv("FIXLOOP_LOG", raising=False)
    monkeypatch.delenv("FIXLOOP_LOG_LEVEL", raising=False)
    yield
    reset_logging_for_tests()


def _capture_stderr_logger(level: str = "DEBUG", log_format: str = "text"):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"),
        )
    logger = logging.getLogger("fixloop")
    logger.handlers.clear()
    logger.setLevel(getattr(logging, level))
    logger.addHandler(handler)
    logger.propagate = False
    return logger, stream


class TestResolveLogFormat:
    def test_default_text(self, monkeypatch):
        monkeypatch.delenv("FIXLOOP_LOG", raising=False)
        assert resolve_log_format() == "text"

    def test_json_env(self, monkeypatch):
        monkeypatch.setenv("FIXLOOP_LOG", "json")
        assert resolve_log_format() == "json"


class TestTextLogging:
    def test_default_format_is_human_readable(self):
        logger, stream = _capture_stderr_logger(log_format="text")
        logger.info("hello")
        line = stream.getvalue().strip()
        assert "INFO" in line
        assert "hello" in line
        with pytest.raises(json.JSONDecodeError):
            json.loads(line)


class TestJsonLogging:
    def test_json_line_has_required_fields(self):
        logger, stream = _capture_stderr_logger(log_format="json")
        logger.warning("parse failed")
        record = json.loads(stream.getvalue().strip())
        assert record["level"] == "WARNING"
        assert record["logger"] == "fixloop"
        assert record["message"] == "parse failed"
        assert "ts" in record
        assert record["ts"].endswith("Z")

    def test_json_includes_run_id_from_context(self):
        logger, stream = _capture_stderr_logger(log_format="json")
        run_id = str(uuid.uuid4())
        with log_context(run_id=run_id, agent="patcher"):
            logger.warning("0 suspects")
        record = json.loads(stream.getvalue().strip())
        assert record["run_id"] == run_id
        assert record["agent"] == "patcher"

    def test_json_omits_run_id_without_context(self):
        logger, stream = _capture_stderr_logger(log_format="json")
        logger.info("no context")
        record = json.loads(stream.getvalue().strip())
        assert "run_id" not in record
        assert "agent" not in record

    def test_debug_filtered_at_warning_level(self):
        logger, stream = _capture_stderr_logger(level="WARNING", log_format="json")
        logger.debug("hidden")
        logger.warning("visible")
        lines = [ln for ln in stream.getvalue().splitlines() if ln.strip()]
        assert len(lines) == 1
        assert json.loads(lines[0])["message"] == "visible"


class TestConfigureLogging:
    def test_configure_json_via_env(self, monkeypatch):
        monkeypatch.setenv("FIXLOOP_LOG", "json")
        reset_logging_for_tests()
        configure_logging("INFO")
        root = logging.getLogger("fixloop")
        assert root.handlers
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
