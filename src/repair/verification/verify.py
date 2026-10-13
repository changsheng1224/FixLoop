"""验证 Strategy：Docker 沙箱 vs 本地 pytest。"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agent_runtime.test_execution import (
    ProcessCleanupUnconfirmedError,
    project_python,
    run_command,
    run_pytest,
)
from agent_runtime.tools import TIER_CONTAINER, TIER_HOST
from src.repair.verification.verification_profiles import select_verification_profile
from src.repair.verification.verification_runner import run_profile
from src.state import VerificationResult


@dataclass
class VerifyRun:
    """单次验证运行结果。"""

    result: VerificationResult
    elapsed_ms: int
    internal: dict
    error: str | None = None

    def __post_init__(self):
        mode = "static" if self.internal.get("actual_tier") == TIER_STATIC else "tests"
        self.internal.setdefault("mode", mode)
        self.result.verification_mode = mode
        self.result.execution_status = "unverified"


class VerifyStrategy(Protocol):
    """验证后端协议。"""

    def run(
        self,
        repo_root: str,
        test_path: str = "",
        *,
        language: str = "python",
    ) -> VerifyRun: ...


TIER_NONE = "none"
TIER_STATIC = "static"
ISOLATION_CONTAINER = "container"
ISOLATION_TRUSTED_LOCAL = "trusted_local"
ISOLATION_NON_EXECUTING = "non_executing"
ISOLATION_NONE = "none"
HOST_FALLBACK_WARNING = "Host fallback has no sandbox isolation; run only trusted code."
STATIC_FALLBACK_WARNING = "Static verification does not execute tests."


def _tier_internal(
    *,
    requested_tier: str,
    actual_tier: str,
    isolation_level: str,
    trusted_execution: bool,
    warning: str = "",
    **extra,
) -> dict:
    data = {
        "execution_tier": actual_tier if actual_tier != TIER_NONE else requested_tier,
        "requested_tier": requested_tier,
        "actual_tier": actual_tier,
        "isolation_level": isolation_level,
        "trusted_execution": trusted_execution,
    }
    if warning:
        data["warning"] = warning
    data.update(extra)
    return data


_COUNT_KINDS = ("passed", "failed", "error", "skipped")


def _pytest_counts(output: str) -> dict[str, int]:
    """pytest 的计数只出现在汇总行；取最后一个含计数的行。

    不能只看输出尾部：``run_command`` 把 stdout 与 stderr 顺序拼接，stderr
    较长时尾部窗口里根本没有汇总行，一次真实通过的运行会被判成 0 个测试。
    只解析单行也避免把多行里的同名计数重复相加。
    """
    counts = dict.fromkeys(_COUNT_KINDS, 0)
    for line in reversed(output.splitlines()):
        found = {
            kind: sum(int(n) for n in re.findall(rf"(\d+) {kind}\b", line)) for kind in _COUNT_KINDS
        }
        if any(found.values()):
            return found
    return counts


def _verify_cancelled_run(start: float) -> VerifyRun:
    from src.harness.sandbox_results import verification_result_for_user_cancel

    return VerifyRun(
        result=verification_result_for_user_cancel(),
        elapsed_ms=int((time.time() - start) * 1000),
        internal=_tier_internal(
            requested_tier=TIER_NONE,
            actual_tier=TIER_NONE,
            isolation_level=ISOLATION_NONE,
            trusted_execution=False,
            user_cancel=True,
            cleanup="confirmed",
            execution_stopped=True,
            completed=False,
            category="interrupted",
        ),
        error="user_cancel",
    )


def _verify_from_sandbox_result(
    result: VerificationResult,
    internal: dict,
    start: float,
) -> VerifyRun:
    error = "user_cancel" if internal.get("user_cancel") else None
    return VerifyRun(
        result=result,
        elapsed_ms=int((time.time() - start) * 1000),
        internal=internal,
        error=error,
    )


class DockerVerifyStrategy:
    """Docker sandbox build + pytest。"""

    def run(self, repo_root: str, test_path: str = "", cancel_token=None) -> VerifyRun:
        from src.harness.sandbox_verify import SandboxNotAvailableError, assert_sandbox_available
        from src.tools.sandbox_tools import run_sandbox_verification

        t0 = time.time()
        try:
            assert_sandbox_available()
        except SandboxNotAvailableError as exc:
            elapsed_ms = int((time.time() - t0) * 1000)
            return VerifyRun(
                result=VerificationResult(
                    all_passed=False,
                    failure_logs=[f"Docker sandbox 不可用，请使用 --pytest 降级: {exc}"],
                ),
                elapsed_ms=elapsed_ms,
                internal=_tier_internal(
                    requested_tier=TIER_CONTAINER,
                    actual_tier=TIER_NONE,
                    isolation_level=ISOLATION_NONE,
                    trusted_execution=False,
                    sandbox_unavailable=True,
                    cleanup="confirmed",
                    not_started=True,
                    fallback_candidate=TIER_HOST,
                    error=str(exc),
                ),
                error="sandbox_unavailable",
            )
        try:
            result, internal = run_sandbox_verification(
                repo_root,
                test_path=test_path,
                cancel_token=cancel_token,
            )
            internal.update(
                _tier_internal(
                    requested_tier=TIER_CONTAINER,
                    actual_tier=TIER_CONTAINER,
                    isolation_level=ISOLATION_CONTAINER,
                    trusted_execution=False,
                )
            )
            return _verify_from_sandbox_result(result, internal, t0)
        except ProcessCleanupUnconfirmedError:
            raise
        except Exception as exc:
            elapsed_ms = int((time.time() - t0) * 1000)
            from src.harness.sandbox_verify import verification_result_for_sandbox_error

            msg = str(exc)
            if "sandbox" in msg.lower() or "upload" in msg.lower() or "docker" in msg.lower():
                result = verification_result_for_sandbox_error(exc)
            else:
                result = VerificationResult(all_passed=False, failure_logs=[msg])
            return VerifyRun(
                result=result,
                elapsed_ms=elapsed_ms,
                internal=_tier_internal(
                    requested_tier=TIER_CONTAINER,
                    actual_tier=TIER_CONTAINER,
                    isolation_level=ISOLATION_CONTAINER,
                    trusted_execution=False,
                ),
                error=str(exc),
            )


class PytestVerifyStrategy:
    """宿主机 subprocess pytest（execution_tier=host）。"""

    def run(self, repo_root: str, test_path: str = "", cancel_token=None) -> VerifyRun:
        from agent_runtime.cancellation import CancelledError

        t0 = time.time()
        try:
            code, out = run_pytest(
                Path(repo_root), [test_path] if test_path else None, cancel_token=cancel_token
            )
        except CancelledError:
            return _verify_cancelled_run(t0)
        except subprocess.TimeoutExpired as exc:
            return VerifyRun(
                VerificationResult(failure_logs=[str(exc)]),
                int((time.time() - t0) * 1000),
                {
                    "actual_tier": "host",
                    "completed": False,
                    "cleanup": "confirmed",
                    "category": "timeout",
                },
                "timeout",
            )
        except OSError as exc:
            return VerifyRun(
                VerificationResult(failure_logs=[str(exc)]),
                int((time.time() - t0) * 1000),
                {
                    "actual_tier": "host",
                    "completed": False,
                    "cleanup": "confirmed",
                    "not_started": True,
                    "category": "verification_environment_failed",
                },
                str(exc),
            )
        elapsed_ms = int((time.time() - t0) * 1000)
        passed = code == 0
        counts = _pytest_counts(out)
        if not passed:
            print(
                f"  [verifier] pytest 失败 (exit={code})\n",
                end="",
                file=sys.stderr,
                flush=True,
            )
        return VerifyRun(
            result=VerificationResult(
                all_passed=passed,
                total_tests=sum(counts.values()),
                passed=counts["passed"],
                failed=counts["failed"],
                error=counts["error"],
                failure_logs=[out[-2000:]] if out and not passed else [],
            ),
            elapsed_ms=elapsed_ms,
            internal=_tier_internal(
                requested_tier=TIER_HOST,
                actual_tier=TIER_HOST,
                isolation_level=ISOLATION_TRUSTED_LOCAL,
                trusted_execution=True,
                warning=HOST_FALLBACK_WARNING,
                pytest_ms=elapsed_ms,
                command=[
                    project_python(repo_root),
                    "-m",
                    "pytest",
                    "-q",
                    *([test_path] if test_path else []),
                ],
                completed=True,
                cleanup="confirmed",
                pytest_exit_code=code,
                category="passed"
                if code == 0
                else "failed"
                if code == 1
                else "no-tests"
                if code == 5
                else "environment",
            ),
        )


class BwrapVerifyStrategy:
    """Use the fixed P1 backend for every Python pytest invocation."""

    def __init__(self, context):
        self.context = context

    def run(
        self,
        repo_root: str,
        test_path: str = "",
        cancel_token=None,
        *,
        language: str = "python",
    ) -> VerifyRun:
        from agent_runtime.linux_sandbox.routing import execute_sandbox, pytest_target

        if Path(repo_root).resolve() != Path(self.context.root).resolve():
            raise ValueError("workspace_mapping_rejected: verifier root mismatch")
        if language != "python":
            return VerifyRun(
                VerificationResult(all_passed=False, failure_logs=["unsupported sandbox language"]),
                0,
                {"category": "environment", "execution_tier": "none"},
                "verification_environment_failed",
            )
        started = time.time()
        try:
            if getattr(self.context.sandbox_backend, "guarded_commands", False):
                from agent_runtime.linux_sandbox.command_profile import test_target

                target = test_target(test_path) if test_path else ""
            else:
                target = pytest_target(repo_root, test_path) if test_path else ""
        except ValueError as exc:
            return VerifyRun(
                VerificationResult(all_passed=False, failure_logs=[str(exc)]),
                0,
                {"category": "environment", "execution_tier": "none"},
                "verification_environment_failed",
            )
        previous_token = self.context.cancel_token
        self.context.cancel_token = cancel_token
        try:
            result = execute_sandbox(
                self.context,
                "pytest",
                (
                    "/toolchain/bin/python",
                    "-I",
                    "-m",
                    "pytest",
                    *((target,) if target else ()),
                    "-q",
                    "--tb=line",
                ),
                120,
            )
        finally:
            self.context.cancel_token = previous_token
        if (
            result.execution_status == "completed"
            and result.cleanup == "confirmed"
            and result.actual_backend == "linux_sandbox"
            and bool(result.receipt_id)
        ):
            category = {0: "passed", 1: "failed", 5: "no-tests"}.get(
                result.exit_code, "environment"
            )
        elif result.execution_status == "cancelled":
            category = "interrupted"
        else:
            category = "environment"
        output = (result.stdout_excerpt + "\n" + result.stderr_excerpt).strip()
        binding = {}
        if getattr(self.context.sandbox_backend, "guarded_commands", False):
            from agent_runtime.linux_sandbox.workspace_contract import tool_result_from_wire

            typed = tool_result_from_wire(result.tool_result) if result.tool_result else None
            if typed is not None:
                binding = typed.metadata.get("verification_binding", {})
            if not binding or typed is None or not typed.metadata.get("verification_current"):
                if category == "passed":
                    category = "environment"
            if result.error_code == "deadline_exceeded":
                category = "timeout"
            elif typed is not None and typed.status == "cancelled":
                category = "interrupted"
        counts = {
            name: int(count)
            for count, name in re.findall(r"(\d+) (passed|failed|skipped|error)s?\b", output)
        }
        total = sum(counts.values())
        if (
            category == "passed"
            and total == 0
            and getattr(self.context.sandbox_backend, "guarded_commands", False)
        ):
            category = "no-tests"
        run = VerifyRun(
            result=VerificationResult(
                all_passed=category == "passed",
                total_tests=total,
                passed=counts.get("passed", 0),
                failed=1 if category == "failed" else 0,
                failure_logs=[output or result.error_code or category]
                if category != "passed"
                else [],
            ),
            elapsed_ms=int((time.time() - started) * 1000),
            internal=_tier_internal(
                requested_tier="linux_sandbox",
                actual_tier="linux_sandbox"
                if result.actual_backend == "linux_sandbox"
                else TIER_NONE,
                isolation_level="wsl_bwrap"
                if result.actual_backend == "linux_sandbox"
                else ISOLATION_NONE,
                trusted_execution=False,
                category=category,
                receipt_id=result.receipt_id,
                policy_digest=result.policy_digest,
                cleanup=result.cleanup,
                sandbox_status=result.execution_status,
                pytest_exit_code=result.exit_code,
                completed=result.execution_status == "completed"
                and result.cleanup == "confirmed"
                and result.actual_backend == "linux_sandbox"
                and bool(result.receipt_id),
                command=["python", "-m", "pytest", target],
                verification_binding=binding,
            ),
            error=None
            if category == "passed"
            else (
                "verification_failed"
                if category == "failed"
                else "verification_timeout"
                if category == "timeout"
                else "user_cancel"
                if category == "interrupted"
                else "verification_environment_failed"
            ),
        )
        if result.actual_backend != "linux_sandbox":
            run.internal["actual_tier"] = "none"
            run.internal["execution_tier"] = "none"
        return run


class StaticVerifyStrategy:
    """非执行静态验证：按语言做语法/编译检查，不运行项目测试。"""

    def run(
        self,
        repo_root: str,
        test_path: str = "",
        cancel_token=None,
        *,
        language: str = "python",
    ) -> VerifyRun:
        del test_path
        root = Path(repo_root)
        t0 = time.time()
        language = (language or "python").lower()
        if language != "python":
            return self._run_external_static_check(root, language, t0, cancel_token)

        failures: list[str] = []
        for path in self._source_files(root, (".py",)):
            if cancel_token is not None:
                cancel_token.check()
            if any(part in {".git", ".pytest_cache", "__pycache__"} for part in path.parts):
                continue
            try:
                source = path.read_text(encoding="utf-8")
                compile(source, str(path), "exec")
            except (OSError, SyntaxError, UnicodeDecodeError) as exc:
                try:
                    rel = path.relative_to(root)
                except ValueError:
                    rel = path
                failures.append(f"{rel}: {exc}")
        elapsed_ms = int((time.time() - t0) * 1000)
        ok = not failures
        return VerifyRun(
            result=VerificationResult(
                all_passed=ok,
                total_tests=0,
                passed=0,
                failed=0 if ok else 1,
                failure_logs=failures,
            ),
            elapsed_ms=elapsed_ms,
            internal=_tier_internal(
                requested_tier=TIER_STATIC,
                actual_tier=TIER_STATIC,
                isolation_level=ISOLATION_NON_EXECUTING,
                trusted_execution=False,
                warning=STATIC_FALLBACK_WARNING,
                static_ms=elapsed_ms,
                language=language,
                checked_files=sum(1 for _ in root.rglob("*.py")),
                command=["python.compile"],
                operation="python.compile",
                completed=True,
                cleanup="confirmed",
                exit_code=0 if ok else 1,
                category="static_passed" if ok else "static_failed",
            ),
            error=None if ok else "static_verify_failed",
        )

    def _run_external_static_check(
        self,
        root: Path,
        language: str,
        start: float,
        cancel_token=None,
    ) -> VerifyRun:
        specs = {
            "java": ("javac", (".java",), self._java_check_command),
            "javascript": ("node", (".js", ".mjs", ".cjs"), self._node_check_commands),
            "typescript": ("tsc", (".ts", ".tsx"), self._tsc_check_command),
            "go": ("gofmt", (".go",), self._gofmt_check_command),
            "ruby": ("ruby", (".rb",), self._ruby_check_commands),
            "php": ("php", (".php",), self._php_check_commands),
            "rust": ("rustc", (".rs",), self._rustc_check_commands),
        }
        spec = specs.get(language)
        if spec is None:
            return self._unsupported_static_language(root, language, start, "no checker configured")

        tool, extensions, command_builder = spec
        from src.repair.language_capabilities import project_command

        if project_command(root, [tool]) is None:
            return self._unsupported_static_language(root, language, start, f"missing {tool}")

        files = self._source_files(root, extensions)
        if not files:
            elapsed_ms = int((time.time() - start) * 1000)
            return VerifyRun(
                result=VerificationResult(all_passed=True),
                elapsed_ms=elapsed_ms,
                internal=_tier_internal(
                    requested_tier=TIER_STATIC,
                    actual_tier=TIER_STATIC,
                    isolation_level=ISOLATION_NON_EXECUTING,
                    trusted_execution=False,
                    warning=STATIC_FALLBACK_WARNING,
                    static_ms=elapsed_ms,
                    language=language,
                    checker=tool,
                    checked_files=0,
                    operation=tool,
                    command=[],
                    steps=[],
                    completed=True,
                    cleanup="confirmed",
                    exit_code=0,
                    category="static_passed",
                ),
            )

        failures: list[str] = []
        steps = []
        with tempfile.TemporaryDirectory(prefix="fixloop_static_") as out_dir:
            for cmd in command_builder(root, files, out_dir):
                cmd = project_command(root, cmd)
                try:
                    code, output = run_command(cmd, root, cancel_token=cancel_token, timeout_s=30)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    return self._unsupported_static_language(root, language, start, str(exc))
                if code != 0:
                    failures.append(output.strip()[:2000] or f"{tool} exited {code}")
                steps.append({"command": cmd, "exit_code": code, "output": output[-2000:]})

        elapsed_ms = int((time.time() - start) * 1000)
        ok = not failures
        return VerifyRun(
            result=VerificationResult(
                all_passed=ok,
                total_tests=0,
                passed=0,
                failed=0 if ok else 1,
                failure_logs=failures,
            ),
            elapsed_ms=elapsed_ms,
            internal=_tier_internal(
                requested_tier=TIER_STATIC,
                actual_tier=TIER_STATIC,
                isolation_level=ISOLATION_NON_EXECUTING,
                trusted_execution=False,
                warning=STATIC_FALLBACK_WARNING,
                static_ms=elapsed_ms,
                language=language,
                checker=tool,
                checked_files=len(files),
                operation=tool,
                command=[step["command"] for step in steps],
                steps=steps,
                completed=True,
                cleanup="confirmed",
                exit_code=0 if ok else 1,
                category="static_passed" if ok else "static_failed",
            ),
            error=None if ok else "static_verify_failed",
        )

    def _unsupported_static_language(
        self,
        root: Path,
        language: str,
        start: float,
        reason: str,
    ) -> VerifyRun:
        elapsed_ms = int((time.time() - start) * 1000)
        return VerifyRun(
            result=VerificationResult(
                all_passed=False,
                failure_logs=[f"static verifier unsupported for {language}: {reason}"],
            ),
            elapsed_ms=elapsed_ms,
            internal=_tier_internal(
                requested_tier=TIER_STATIC,
                actual_tier=TIER_STATIC,
                isolation_level=ISOLATION_NON_EXECUTING,
                trusted_execution=False,
                warning=STATIC_FALLBACK_WARNING,
                static_ms=elapsed_ms,
                language=language,
                unsupported_language=language,
                root=str(root),
                command=[],
                completed=False,
                cleanup="confirmed",
                category="environment",
            ),
            error="static_verify_unsupported",
        )

    @staticmethod
    def _source_files(root: Path, extensions: tuple[str, ...]) -> list[Path]:
        from src.repair.language_capabilities import source_files

        return source_files(root, extensions)

    @staticmethod
    def _rel(root: Path, path: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return str(path)

    @classmethod
    def _java_check_command(cls, root: Path, files: list[Path], out_dir: str) -> list[list[str]]:
        return [
            [
                "javac",
                "-proc:none",
                "-d",
                out_dir,
                *[cls._rel(root, path) for path in files],
            ]
        ]

    @classmethod
    def _node_check_commands(cls, root: Path, files: list[Path], out_dir: str) -> list[list[str]]:
        del out_dir
        return [["node", "--check", cls._rel(root, path)] for path in files]

    @classmethod
    def _tsc_check_command(cls, root: Path, files: list[Path], out_dir: str) -> list[list[str]]:
        del out_dir
        if (root / "tsconfig.json").is_file():
            return [["tsc", "--noEmit", "--pretty", "false", "-p", str(root.resolve())]]
        return [["tsc", "--noEmit", "--pretty", "false", *[str(path) for path in files]]]

    @classmethod
    def _gofmt_check_command(cls, root: Path, files: list[Path], out_dir: str) -> list[list[str]]:
        del out_dir
        return [["gofmt", "-e", "-l", *[cls._rel(root, path) for path in files]]]

    @classmethod
    def _ruby_check_commands(cls, root: Path, files: list[Path], out_dir: str) -> list[list[str]]:
        del out_dir
        return [["ruby", "-c", cls._rel(root, path)] for path in files]

    @classmethod
    def _php_check_commands(cls, root: Path, files: list[Path], out_dir: str) -> list[list[str]]:
        del out_dir
        return [["php", "-l", cls._rel(root, path)] for path in files]

    @classmethod
    def _rustc_check_commands(cls, root: Path, files: list[Path], out_dir: str) -> list[list[str]]:
        return [
            [
                "rustc",
                "--emit=metadata",
                "-o",
                str(Path(out_dir) / f"{path.stem}.rmeta"),
                cls._rel(root, path),
            ]
            for path in files
        ]


class ProfileVerifyStrategy:
    """Manifest-aware dynamic verification for common non-Python ecosystems."""

    def run(
        self,
        repo_root: str,
        test_path: str = "",
        cancel_token=None,
        *,
        language: str = "python",
    ) -> VerifyRun:
        started = time.time()
        profile = select_verification_profile(repo_root, language)
        if profile is None:
            return VerifyRun(
                result=VerificationResult(
                    all_passed=False,
                    failed=1,
                    failure_logs=[f"no verification profile for {language}"],
                ),
                elapsed_ms=int((time.time() - started) * 1000),
                internal={
                    "language": language,
                    "category": "verification_environment_failed",
                    "completed": False,
                    "not_started": True,
                    "cleanup": "confirmed",
                },
                error="verification_profile_missing",
            )
        payload = run_profile(repo_root, profile, target=test_path, cancel_token=cancel_token)
        passed = bool(payload.get("all_passed"))
        category = payload.get("category", "")
        logs = [
            str(step.get("output", "")) for step in payload.get("steps", []) if step.get("output")
        ]
        if not passed and payload.get("error"):
            logs.append(str(payload["error"]))
        return VerifyRun(
            result=VerificationResult(
                all_passed=passed,
                total_tests=payload.get("total_tests", 0),
                passed=payload.get("passed", 0),
                failed=0 if passed else 1,
                failure_logs=logs,
            ),
            elapsed_ms=int((time.time() - started) * 1000),
            internal={
                "execution_tier": "host",
                "actual_tier": "host",
                "language": profile.language,
                "ecosystem": profile.ecosystem,
                "category": category,
                "steps": payload.get("steps", []),
                "command": [step["command"] for step in payload.get("steps", [])],
                "completed": payload.get("completed", False),
                "exit_code": payload.get("exit_code"),
                "cleanup": "confirmed",
            },
            error=None if passed else category,
        )


def record_verify_timings(state, run: VerifyRun, *, log_sandbox: bool = False) -> None:
    """写入 RepairState.node_timings 并可选打印 sandbox 耗时。"""
    from src.repair.execution_state import VerificationReceipt
    from src.repair.timing_schema import set_phase_ms

    set_phase_ms(state.node_timings, "verify", run.elapsed_ms, internal=run.internal)
    state.execution.verification_receipt = VerificationReceipt.model_validate(
        {
            "command": run.internal.get("command", []),
            "mode": run.internal.get("mode", "tests"),
            "operation": run.internal.get("operation", ""),
            "checked_files": run.internal.get("checked_files", 0),
            "steps": run.internal.get("steps", []),
            "cleanup": run.internal.get(
                "cleanup", "confirmed" if run.internal.get("completed") else "unknown"
            ),
            "completed": run.internal.get("completed", False),
            "not_started": run.internal.get("not_started", False),
            "exit_code": run.internal.get("exit_code", run.internal.get("pytest_exit_code")),
            "category": run.internal.get("category", run.error or "unknown"),
            "receipt_id": run.internal.get("receipt_id", ""),
            "verification_binding": run.internal.get("verification_binding", {}),
        }
    )
    state.node_timings["plan_verification_receipt"] = (
        state.execution.verification_receipt.model_dump()
    )
    if run.error:
        state.agent_errors["verifier"] = run.error
    if log_sandbox and run.internal:
        print(
            f"  [verifier] sandbox: create={run.internal.get('container_create_ms', '?')}ms "
            f"tar={run.internal.get('tar_copy_ms', '?')}ms "
            f"pip={run.internal.get('pip_ms', '?')}ms "
            f"pytest={run.internal.get('pytest_ms', '?')}ms",
            file=sys.stderr,
            flush=True,
        )
