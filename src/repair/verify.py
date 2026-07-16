"""验证 Strategy：Docker 沙箱 vs 本地 pytest。"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agent_runtime.tools import TIER_CONTAINER, TIER_HOST
from src.state import VerificationResult


@dataclass
class VerifyRun:
    """单次验证运行结果。"""

    result: VerificationResult
    elapsed_ms: int
    internal: dict
    error: str | None = None


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
        except Exception as exc:
            elapsed_ms = int((time.time() - t0) * 1000)
            return VerifyRun(
                result=VerificationResult(all_passed=False, failure_logs=[str(exc)]),
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
        from agent_runtime.cancellation import CancelledError, run_with_cancellation
        from src.eval.runner import run_pytest

        del test_path

        def _run_pytest():
            return run_pytest(Path(repo_root))

        t0 = time.time()
        try:
            if cancel_token is not None:
                code, out = run_with_cancellation(_run_pytest, cancel_token)
            else:
                code, out = _run_pytest()
        except CancelledError:
            return _verify_cancelled_run(t0)
        elapsed_ms = int((time.time() - t0) * 1000)
        passed = code == 0
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
            ),
        )


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
        del test_path, cancel_token
        root = Path(repo_root)
        t0 = time.time()
        language = (language or "python").lower()
        if language != "python":
            return self._run_external_static_check(root, language, t0)

        failures: list[str] = []
        for path in root.rglob("*.py"):
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
            ),
            error=None if ok else "static_verify_failed",
        )

    def _run_external_static_check(
        self,
        root: Path,
        language: str,
        start: float,
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
        executable = shutil.which(tool)
        if not executable:
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
                ),
            )

        failures: list[str] = []
        with tempfile.TemporaryDirectory(prefix="fixloop_static_") as out_dir:
            for cmd in command_builder(root, files, out_dir):
                proc = subprocess.run(
                    cmd,
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if proc.returncode != 0:
                    output = (proc.stderr or proc.stdout or "").strip()
                    failures.append(output[:2000] or f"{tool} exited {proc.returncode}")

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
            ),
            error="static_verify_unsupported",
        )

    @staticmethod
    def _source_files(root: Path, extensions: tuple[str, ...]) -> list[Path]:
        files: list[Path] = []
        skip = {".git", ".pytest_cache", "__pycache__", "node_modules", "target", "dist"}
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            if any(part in skip for part in path.parts):
                continue
            files.append(path)
        return sorted(files)

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
        del root, out_dir
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


def record_verify_timings(state, run: VerifyRun, *, log_sandbox: bool = False) -> None:
    """写入 RepairState.node_timings 并可选打印 sandbox 耗时。"""
    from src.repair.timing_schema import set_phase_ms

    set_phase_ms(state.node_timings, "verify", run.elapsed_ms, internal=run.internal)
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
