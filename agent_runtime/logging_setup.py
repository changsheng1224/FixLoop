"""统一 logging 配置：stderr 文本日志 + --log-level / FIXLOOP_LOG_LEVEL。"""

from __future__ import annotations

import argparse
import logging
import os
import sys

LOGGER_ROOT = "fixloop"
_DEFAULT_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_DATE_FORMAT = "%H:%M:%S"


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


def configure_logging(level: str | int | None = None) -> None:
    """配置 fixloop 命名空间 logger（stderr，单例 handler）。"""
    if level is None:
        level = resolve_log_level()
    numeric = getattr(logging, str(level).upper(), logging.INFO)

    logger = logging.getLogger(LOGGER_ROOT)
    logger.setLevel(numeric)

    if not getattr(configure_logging, "_configured", False):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(_DEFAULT_FORMAT, datefmt=_DATE_FORMAT),
        )
        logger.addHandler(handler)
        logger.propagate = False
        configure_logging._configured = True  # type: ignore[attr-defined]
    else:
        for handler in logger.handlers:
            handler.setLevel(numeric)


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
