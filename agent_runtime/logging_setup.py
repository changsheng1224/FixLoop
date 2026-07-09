"""统一 logging 配置：stderr 文本/JSON 日志 + --log-level / FIXLOOP_LOG_LEVEL。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime

from agent_runtime.log_context import get_log_context

LOGGER_ROOT = "fixloop"
_DEFAULT_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_DATE_FORMAT = "%H:%M:%S"
_RECORD_BUILTIN_KEYS = frozenset(
    logging.makeLogRecord({}).__dict__.keys()
) | frozenset({"message", "asctime"})


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line (JSONL-style stderr)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).strftime(
                "%Y-%m-%dT%H:%M:%S."
            )
            + f"{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(get_log_context())
        for key, value in record.__dict__.items():
            if key in _RECORD_BUILTIN_KEYS or key.startswith("_"):
                continue
            if key not in payload:
                payload[key] = value
        if record.exc_info:
            exc_type, exc_value, _ = record.exc_info
            exc_body: dict = {
                "type": exc_type.__name__ if exc_type else "Exception",
                "message": str(exc_value),
            }
            if record.levelno <= logging.DEBUG:
                exc_body["traceback"] = self.formatException(record.exc_info)
            payload["exception"] = exc_body
        return json.dumps(payload, ensure_ascii=False, default=str)


def resolve_log_format() -> str:
    """Return ``text`` or ``json`` (``FIXLOOP_LOG=json`` enables JSON)."""
    val = (os.environ.get("FIXLOOP_LOG") or "text").strip().lower()
    return "json" if val == "json" else "text"


def resolve_log_level(
    *,
    log_level: str | None = None,
    verbose: bool = False,
) -> str:
    """解析有效日志级别：CLI --log-level > --verbose > FIXLOOP_LOG_LEVEL > INFO。"""
    if log_level:
        return log_level.upper()
    if verbose:
        return "DEBUG"
    return (os.environ.get("FIXLOOP_LOG_LEVEL") or "INFO").upper()


def _build_formatter(log_format: str) -> logging.Formatter:
    if log_format == "json":
        return JsonFormatter()
    return logging.Formatter(_DEFAULT_FORMAT, datefmt=_DATE_FORMAT)


def configure_logging(level: str | int | None = None) -> None:
    """配置 fixloop 命名空间 logger（stderr，单例 handler）。"""
    if level is None:
        level = resolve_log_level()
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    log_format = resolve_log_format()
    formatter = _build_formatter(log_format)

    logger = logging.getLogger(LOGGER_ROOT)
    logger.setLevel(numeric)

    if not getattr(configure_logging, "_configured", False):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.propagate = False
        configure_logging._configured = True  # type: ignore[attr-defined]
    else:
        for handler in logger.handlers:
            handler.setLevel(numeric)
            handler.setFormatter(formatter)


def reset_logging_for_tests() -> None:
    """Clear fixloop logging handlers (tests only)."""
    logger = logging.getLogger(LOGGER_ROOT)
    logger.handlers.clear()
    configure_logging._configured = False  # type: ignore[attr-defined]


def get_logger(name: str) -> logging.Logger:
    """返回 fixloop.<name> logger。"""
    if name.startswith(f"{LOGGER_ROOT}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_ROOT}.{name}")


def add_log_level_argument(parser: argparse.ArgumentParser) -> None:
    """向 argparse 注册 --log-level（可选，默认由 resolve_log_level 决定）。"""
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别（--verbose 等价 DEBUG；显式 --log-level 优先）",
    )


def setup_logging_from_args(args) -> None:
    """从 CLI namespace（含可选 verbose / log_level）初始化 logging。"""
    level = resolve_log_level(
        log_level=getattr(args, "log_level", None),
        verbose=bool(getattr(args, "verbose", False)),
    )
    configure_logging(level)
