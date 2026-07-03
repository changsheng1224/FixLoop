"""CLI repair 子命令冒烟测试（FakeClient + skip-verify）。"""

import sys

import pytest

from agent_runtime.providers.clients import FakeModelClient


class TestCliRepair:
    def test_repair_skip_verify_smoke(self, temp_workspace, monkeypatch, capsys):
        """repair --skip-verify 在 FakeClient 下可跑通。"""
        (temp_workspace / "app.py").write_text("x = 1\n")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.chdir(temp_workspace)

        shared = FakeModelClient(
            [
                '<final>[{"file_path":"app.py","start_line":1,"end_line":1,'
                '"reason":"堆栈","confidence":0.9}]</final>',
                '<final>{"related_tests":[]}</final>',
                '<final>[{"file_path":"app.py","original_lines":"x = 1",'
                '"patched_lines":"x = 2","explanation":"fix"}]</final>',
            ]
        )
        monkeypatch.setattr(
            "src.repair_factory.AnthropicCompatibleModelClient",
            lambda **kwargs: shared,
        )
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "src.cli",
                "repair",
                "--issue",
                "TypeError at app.py:1",
                "--repo",
                str(temp_workspace),
                "--skip-verify",
            ],
        )

        from src.cli import main

        assert main() == 0
        out = capsys.readouterr().out
        assert "patched" in out or "修复完成" in out

    def test_main_without_subcommand_returns_1(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["src.cli"])
        from src.cli import main

        assert main() == 1
