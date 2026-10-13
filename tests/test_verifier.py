"""Verifier Agent 工厂与 ToolGateway 权限测试。"""


class TestVerifyStrategyExecutionTier:
    """Verify strategy execution_tier 标记测试。"""

    def test_pytest_strategy_marks_host_tier(self, tmp_path):
        from src.repair.verification.verify import PytestVerifyStrategy

        (tmp_path / "test_x.py").write_text("def test_pass(): assert True\n", encoding="utf-8")
        run = PytestVerifyStrategy().run(str(tmp_path))
        assert run.internal.get("execution_tier") == "host"
        assert run.internal.get("requested_tier") == "host"
        assert run.internal.get("actual_tier") == "host"
        assert run.internal.get("isolation_level") == "trusted_local"
        assert run.internal.get("trusted_execution") is True
        assert "no sandbox isolation" in run.internal.get("warning", "").lower()

    def test_docker_strategy_returns_container_tier_on_success(self, monkeypatch, tmp_path):
        from src.repair.verification.verify import DockerVerifyStrategy

        monkeypatch.setattr("src.harness.sandbox_verify.assert_sandbox_available", lambda: None)
        from src.state import VerificationResult

        monkeypatch.setattr(
            "src.tools.sandbox_tools.run_sandbox_verification",
            lambda repo_path, test_path="", context=None, cancel_token=None: (
                VerificationResult(all_passed=True, total_tests=1, passed=1),
                {"pytest_ms": 10},
            ),
        )
        run = DockerVerifyStrategy().run(str(tmp_path))
        assert run.error is None
        assert run.internal.get("execution_tier") == "container"
        assert run.internal.get("requested_tier") == "container"
        assert run.internal.get("actual_tier") == "container"
        assert run.internal.get("isolation_level") == "container"
        assert run.internal.get("trusted_execution") is False

    def test_docker_strategy_unavailable_marks_no_actual_tier(self, monkeypatch, tmp_path):
        from src.harness.sandbox_verify import SandboxNotAvailableError
        from src.repair.verification.verify import DockerVerifyStrategy

        monkeypatch.setattr(
            "src.harness.sandbox_verify.assert_sandbox_available",
            lambda: (_ for _ in ()).throw(SandboxNotAvailableError("mock unavailable")),
        )
        run = DockerVerifyStrategy().run(str(tmp_path))
        assert run.error == "sandbox_unavailable"
        assert run.internal.get("execution_tier") == "container"
        assert run.internal.get("requested_tier") == "container"
        assert run.internal.get("actual_tier") == "none"
        assert run.internal.get("fallback_candidate") == "host"
        assert run.internal.get("isolation_level") == "none"
        assert run.internal.get("sandbox_unavailable") is True

    def test_static_strategy_checks_syntax_without_pytest(self, tmp_path):
        from src.repair.verification.verify import StaticVerifyStrategy

        (tmp_path / "app.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
        run = StaticVerifyStrategy().run(str(tmp_path))
        assert run.result.all_passed is True
        assert run.internal.get("execution_tier") == "static"
        assert run.internal.get("actual_tier") == "static"
        assert run.internal.get("isolation_level") == "non_executing"
        assert run.internal.get("trusted_execution") is False

    def test_static_strategy_reports_compile_error(self, tmp_path):
        from src.repair.verification.verify import StaticVerifyStrategy

        (tmp_path / "broken.py").write_text("def bad(:\n", encoding="utf-8")
        run = StaticVerifyStrategy().run(str(tmp_path))
        assert run.result.all_passed is False
        assert run.error == "static_verify_failed"
        assert any("broken.py" in log for log in run.result.failure_logs)

    def test_static_strategy_checks_java_with_javac(self, monkeypatch, tmp_path):
        import subprocess

        from src.repair.verification.verify import StaticVerifyStrategy

        (tmp_path / "Bar.java").write_text("class Bar { void x( }", encoding="utf-8")
        monkeypatch.setattr("shutil.which", lambda name: "javac" if name == "javac" else None)

        def fake_run(cmd, cwd, capture_output, text, timeout):
            assert cmd[:2] == ["javac", "-proc:none"]
            assert cwd == tmp_path
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="Bar.java:1: error: illegal start of expression"
            )

        monkeypatch.setattr(
            "src.repair.verification.verify.run_command",
            lambda cmd, root, **kw: (1, "Bar.java:1: error: illegal start of expression"),
        )
        run = StaticVerifyStrategy().run(str(tmp_path), language="java")
        assert run.result.all_passed is False
        assert run.error == "static_verify_failed"
        assert run.internal["language"] == "java"
        assert run.internal["checker"] == "javac"
        assert any("Bar.java" in log for log in run.result.failure_logs)

    def test_static_strategy_reports_unsupported_language_tool(self, monkeypatch, tmp_path):
        from src.repair.verification.verify import StaticVerifyStrategy

        (tmp_path / "Bar.java").write_text("class Bar {}\n", encoding="utf-8")
        monkeypatch.setattr("shutil.which", lambda name: None)
        run = StaticVerifyStrategy().run(str(tmp_path), language="java")
        assert run.result.all_passed is False
        assert run.error == "static_verify_unsupported"
        assert run.internal["unsupported_language"] == "java"
        assert "javac" in run.result.failure_logs[0]


class TestVerifierFallbackPolicy:
    def test_docker_unavailable_falls_back_to_host_when_allowed(self, monkeypatch, tmp_path):
        from src.harness.sandbox_verify import SandboxNotAvailableError
        from src.repair.session import ExecutionSession
        from src.state import RepairState

        monkeypatch.setattr(
            "src.harness.sandbox_verify.assert_sandbox_available",
            lambda: (_ for _ in ()).throw(SandboxNotAvailableError("mock unavailable")),
        )
        (tmp_path / "test_x.py").write_text("def test_pass(): assert True\n", encoding="utf-8")
        orch = ExecutionSession(None, verification_backend=object(), use_pytest_verify=True)
        orch.repo_root = str(tmp_path)
        result = orch.verification.run_verifier(RepairState(issue_input="x"))
        assert result.all_passed is True

    def test_docker_unavailable_uses_static_when_host_not_allowed(self, monkeypatch, tmp_path):
        from src.harness.sandbox_verify import SandboxNotAvailableError
        from src.repair.session import ExecutionSession
        from src.state import RepairState

        monkeypatch.setattr(
            "src.harness.sandbox_verify.assert_sandbox_available",
            lambda: (_ for _ in ()).throw(SandboxNotAvailableError("mock unavailable")),
        )
        (tmp_path / "app.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
        orch = ExecutionSession(
            None,
            verification_backend=object(),
            use_pytest_verify=False,
            allow_static_verify_fallback=True,
        )
        orch.repo_root = str(tmp_path)
        state = RepairState(issue_input="x")
        result = orch.verification.run_verifier(state)
        assert result.all_passed is False
        assert result.execution_status == "unverified"
        assert state.node_timings["phases_internal"]["verify"]["actual_tier"] == "static"

    def test_require_sandbox_blocks_fallback(self, monkeypatch, tmp_path):
        from src.harness.sandbox_verify import SandboxNotAvailableError
        from src.repair.session import ExecutionSession
        from src.state import RepairState

        monkeypatch.setattr(
            "src.harness.sandbox_verify.assert_sandbox_available",
            lambda: (_ for _ in ()).throw(SandboxNotAvailableError("mock unavailable")),
        )
        orch = ExecutionSession(
            None, verification_backend=object(), use_pytest_verify=True, require_sandbox=True
        )
        orch.repo_root = str(tmp_path)
        state = RepairState(issue_input="x")
        result = orch.verification.run_verifier(state)
        assert result.all_passed is False
        assert state.node_timings["phases_internal"]["verify"]["actual_tier"] == "none"
        assert state.agent_errors["verifier"] == "sandbox_unavailable"

    def test_static_strategy_runs_without_verifier_agent(self, tmp_path):
        from src.repair.session import ExecutionSession
        from src.state import RepairState

        (tmp_path / "app.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
        orch = ExecutionSession(
            None,
            verification_backend=None,
            use_pytest_verify=False,
            allow_static_verify_fallback=True,
        )
        orch.repo_root = str(tmp_path)
        state = RepairState(issue_input="x")
        result = orch.verification.run_verifier(state)
        assert result.all_passed is False
        assert result.execution_status == "unverified"
        assert state.node_timings["phases_internal"]["verify"]["actual_tier"] == "static"

    def test_non_python_plan_uses_language_aware_static_verifier(self, monkeypatch, tmp_path):
        from src.repair.session import ExecutionSession
        from src.repair.verification.verify import VerifyRun
        from src.state import RepairPlan, RepairState, VerificationResult

        seen: dict[str, str] = {}

        def fake_static_run(self, repo_root, test_path="", cancel_token=None, language="python"):
            seen["language"] = language
            return VerifyRun(
                result=VerificationResult(all_passed=True),
                elapsed_ms=1,
                internal={"actual_tier": "static", "language": language},
            )

        monkeypatch.setattr("shutil.which", lambda _command: None)
        monkeypatch.setattr(
            "src.repair.verification.verify.StaticVerifyStrategy.run", fake_static_run
        )
        orch = ExecutionSession(None, allow_static_verify_fallback=True)
        orch.repo_root = str(tmp_path)
        state = RepairState(
            issue_input="java.lang.NullPointerException at Bar.java:10",
            repair_plan=RepairPlan(language="java", language_source="extension:.java"),
        )
        result = orch.verification.run_verifier(state)
        assert result.all_passed is False
        assert result.execution_status == "unverified"
        assert seen["language"] == "java"
        assert state.node_timings["phases_internal"]["verify"]["language"] == "java"
