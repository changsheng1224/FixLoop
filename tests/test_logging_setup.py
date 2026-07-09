"""统一 logging 配置测试。"""

import logging

from agent_runtime.logging_setup import (
    add_log_level_argument,
    configure_logging,
    get_logger,
    resolve_log_level,
)


def _capture_logger(name: str, level: str) -> tuple[logging.Logger, list[str]]:
    """配置并返回带内存 handler 的 logger。"""
    configure_logging(level)
    log = get_logger(name)
    records: list[str] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    log.handlers.clear()
    log.propagate = False
    handler = _ListHandler()
    handler.setLevel(logging.DEBUG)
    log.addHandler(handler)
    log.setLevel(getattr(logging, level))
    return log, records


class TestResolveLogLevel:
    def test_default_info(self):
        assert resolve_log_level() == "INFO"

    def test_verbose_maps_to_debug(self):
        assert resolve_log_level(verbose=True) == "DEBUG"

    def test_explicit_overrides_verbose(self):
        assert resolve_log_level(log_level="WARNING", verbose=True) == "WARNING"

    def test_env_fallback(self, monkeypatch):
        monkeypatch.setenv("FIXLOOP_LOG_LEVEL", "ERROR")
        assert resolve_log_level() == "ERROR"

    def test_cli_overrides_env(self, monkeypatch):
        monkeypatch.setenv("FIXLOOP_LOG_LEVEL", "ERROR")
        assert resolve_log_level(log_level="DEBUG") == "DEBUG"


class TestConfigureLogging:
    def test_warning_filters_info(self):
        log, records = _capture_logger("test.filter", "WARNING")
        log.info("hidden")
        log.warning("shown")
        assert records == ["shown"]

    def test_debug_includes_debug(self):
        log, records = _capture_logger("agent_loop", "DEBUG")
        log.debug("step detail")
        assert records == ["step detail"]


class TestGetLogger:
    def test_prefixes_fixloop(self):
        assert get_logger("repair.pipeline").name == "fixloop.repair.pipeline"

    def test_idempotent_prefix(self):
        assert get_logger("fixloop.orchestrator").name == "fixloop.orchestrator"


class TestAddLogLevelArgument:
    def test_registers_optional_flag(self):
        import argparse

        p = argparse.ArgumentParser()
        add_log_level_argument(p)
        args = p.parse_args(["--log-level", "ERROR"])
        assert args.log_level == "ERROR"
