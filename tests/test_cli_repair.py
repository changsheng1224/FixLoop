"""CLI repair 子命令冒烟测试（FakeClient + skip-verify）。"""

import sys

from agent_runtime.providers.clients import FakeModelClient
from src.cli_exit_codes import REPAIR_EXIT_CONFIG, REPAIR_EXIT_FAIL, REPAIR_EXIT_TIMEOUT
from src.state import RepairState


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
        def _fake_create(model_client=None, **kwargs):
            if model_client is not None:
                return model_client
            return shared

        monkeypatch.setattr("agent_runtime.bootstrap.create_model_client", _fake_create)
        monkeypatch.setattr("src.repair_factory.create_model_client", _fake_create)
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
        assert "fixed" in out or "修复完成" in out

    def test_main_without_subcommand_returns_1(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["src.cli"])
        from src.cli import main

        assert main() == 1

    def test_repair_missing_api_key_returns_config_exit(self, temp_workspace, monkeypatch, capsys):
        monkeypatch.chdir(temp_workspace)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
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

        assert main() == REPAIR_EXIT_CONFIG
        assert "DEEPSEEK_API_KEY" in capsys.readouterr().err

    def test_repair_missing_repo_returns_config_exit(self, temp_workspace, monkeypatch, capsys):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.chdir(temp_workspace)
        missing = temp_workspace / "missing-repo"
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "src.cli",
                "repair",
                "--issue",
                "TypeError at app.py:1",
                "--repo",
                str(missing),
                "--skip-verify",
            ],
        )
        from src.cli import main

        assert main() == REPAIR_EXIT_CONFIG
        assert "不存在" in capsys.readouterr().err

    def test_repair_failed_returns_fail_exit(self, temp_workspace, monkeypatch, capsys):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.chdir(temp_workspace)

        class _FailOrch:
            def repair(self, issue, **kwargs):
                return RepairState(issue_input=issue, status="failed")

            verifier = None

        def _fake_factory(**kwargs):
            def factory(repo):
                return _FailOrch()

            return factory

        monkeypatch.setattr("src.cli.make_orchestrator_factory", _fake_factory)
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

        assert main() == REPAIR_EXIT_FAIL
        assert "未完成" in capsys.readouterr().out

    def test_repair_timeout_returns_timeout_exit(self, temp_workspace, monkeypatch, capsys):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.chdir(temp_workspace)

        class _TimeoutOrch:
            def repair(self, issue, **kwargs):
                state = RepairState(issue_input=issue, status="failed")
                state.node_timings["repair_timeout"] = 180
                state.agent_errors["orchestrator"] = "repair timeout (180s)"
                return state

            verifier = None

        def _fake_factory(**kwargs):
            def factory(repo):
                return _TimeoutOrch()

            return factory

        monkeypatch.setattr("src.cli.make_orchestrator_factory", _fake_factory)
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

        assert main() == REPAIR_EXIT_TIMEOUT
