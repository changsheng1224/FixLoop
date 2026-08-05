"""Sandbox 验证编排：create → pip → pytest → destroy。"""

from __future__ import annotations

import re
import time
from pathlib import Path

from src.harness.python_runner import PythonTestRunner
from src.harness.sandbox_manager import (
    BUILD_TIMEOUT_S,
    ExecResult,
    Sandbox,
    SandboxManager,
    sandbox_pip_install_command,
    sandbox_pythonpath_prefix,
)
from src.harness.sandbox_tar import SandboxArchiveError
from src.state import VerificationResult


class SandboxNotAvailableError(Exception):
    """Docker 不可用时抛出，供 Orchestrator 降级到 host pytest。"""

    def __init__(self, reason: str = ""):
        super().__init__(f"Docker sandbox 不可用: {reason}" if reason else "Docker sandbox 不可用")
        self.reason = reason


def assert_sandbox_available() -> None:
    """快速体检：Docker daemon 是否可达。

    Raises:
        SandboxNotAvailableError: Docker 不可达时抛出。
    """
    try:
        import docker

        client = docker.from_env()
        client.ping()
    except Exception as exc:
        raise SandboxNotAvailableError(str(exc)) from exc


def run_sandbox_verification_flow(
    context,
    repo: str,
    test_path: str,
    cancel_token=None,
) -> tuple[VerificationResult, dict]:
    """创建容器 → 可选 pip install → pytest → 销毁。"""
    from src.harness.sandbox_results import verification_result_for_user_cancel

    if cancel_token is not None and cancel_token.is_cancelled:
        return verification_result_for_user_cancel(), {"user_cancel": True}

    sandbox_id = getattr(context, "_sandbox_id", None)
    mgr = getattr(context, "_sandbox_mgr", None)
    timings: dict[str, int | str | bool] = {}
    sandbox = None
    created_here = False

    # container tier 体检：首次创建容器前验证 Docker 可达
    if sandbox_id is None:
        assert_sandbox_available()

    def _abort_if_cancelled() -> tuple[VerificationResult, dict] | None:
        if cancel_token is not None and cancel_token.is_cancelled:
            if sandbox is not None and mgr is not None and created_here:
                try:
                    mgr.destroy(sandbox)
                except Exception:
                    pass
                context._sandbox_id = None
                context._sandbox_mgr = None
            timings["user_cancel"] = True
            return verification_result_for_user_cancel(), timings
        return None

    try:
        if sandbox_id is None or mgr is None:
            aborted = _abort_if_cancelled()
            if aborted is not None:
                return aborted
            mgr = SandboxManager()
            try:
                sandbox = mgr.create(repo)
            except SandboxArchiveError as exc:
                return verification_result_for_tar_error(exc), timings_for_tar_error(exc)
            except RuntimeError as exc:
                # upload / tmpfs 权限等：结构化 ENV，避免裸异常串
                return verification_result_for_sandbox_error(exc), {
                    "sandbox_create_error": str(exc)[:200],
                }
            created_here = True
            context._sandbox_id = sandbox.id
            context._sandbox_mgr = mgr
            context._sandbox_repo = repo
            if sandbox.timings:
                timings.update(sandbox.timings)
            aborted = _abort_if_cancelled()
            if aborted is not None:
                return aborted
            build_result, pip_ms, pip_exec = maybe_pip_install(
                mgr, sandbox, repo, cancel_token=cancel_token
            )
            timings["pip_ms"] = pip_ms
            timings["build_result"] = build_result
            context._build_result = build_result
            context._sandbox_pythonpath = sandbox_pythonpath_prefix(repo)
            if pip_exec is not None and pip_exec.cancelled:
                timings["user_cancel"] = True
                return verification_result_for_user_cancel(), timings
            # 离线沙箱：pip -e 可能失败，但 PYTHONPATH 仍可使源码树可 import。
            # 软继续 pytest，避免吞掉真实 pip 失败却又硬拦收集。
            if pip_exec is not None and pip_exec.exit_code != 0:
                timings["pip_failed"] = True
                timings["pip_soft_continue"] = True
        else:
            sandbox = Sandbox(id=sandbox_id, profile="python")
            timings["build_result"] = getattr(context, "_build_result", "reused")

        aborted = _abort_if_cancelled()
        if aborted is not None:
            return aborted

        runner = PythonTestRunner(mgr)
        from src.harness.verify_env import build_verify_env_prefix, detect_verify_profile

        repo_for_env = getattr(context, "_sandbox_repo", repo)
        profile = detect_verify_profile(repo_for_env)
        path_prefix = build_verify_env_prefix(repo_for_env, profile)
        context._sandbox_pythonpath = path_prefix
        context._verify_profile = profile.kind
        timings["verify_profile"] = profile.kind
        if profile.settings_module:
            timings["django_settings"] = profile.settings_module
        t0 = time.time()
        result = runner.run(
            sandbox,
            test_path,
            cancel_token=cancel_token,
            env_prefix=path_prefix,
            profile=profile,
            repo_path=str(repo_for_env or ""),
        )
        timings["pytest_ms"] = int((time.time() - t0) * 1000)
        if cancel_token is not None and cancel_token.is_cancelled:
            timings["user_cancel"] = True
            return verification_result_for_user_cancel(), timings
        if timings.get("pip_failed") and result.total_tests == 0:
            pip_note = f"sandbox pip soft-continue after failure:\n{timings.get('build_result', '')}"
            result.failure_logs = list(result.failure_logs or []) + [pip_note[:800]]
        return result, timings
    finally:
        if sandbox is not None and mgr is not None and created_here:
            try:
                mgr.destroy(sandbox)
            except Exception:
                pass
            context._sandbox_id = None
            context._sandbox_mgr = None


def ensure_sandbox(context, repo_path: str) -> dict:
    """确保容器已创建并完成构建（幂等）。"""
    sandbox_id = getattr(context, "_sandbox_id", None)

    if sandbox_id is None:
        mgr = SandboxManager()
        try:
            sandbox = mgr.create(repo_path)
        except SandboxArchiveError as exc:
            return {"status": "error", "build_result": str(exc), "tar_error_code": exc.code}
        context._sandbox_id = sandbox.id
        context._sandbox_mgr = mgr
        context._sandbox_repo = repo_path
        context._sandbox_pythonpath = sandbox_pythonpath_prefix(repo_path)

        build_result, _pip_ms, _pip_exec = maybe_pip_install(mgr, sandbox, repo_path)
        context._build_result = build_result
        return {"status": "created", "build_result": build_result}

    return {"status": "reused", "build_result": getattr(context, "_build_result", "")}


def verification_result_for_tar_error(exc: SandboxArchiveError) -> VerificationResult:
    return VerificationResult(
        all_passed=False,
        total_tests=0,
        failure_logs=[
            f"verify_config: sandbox tar failed: {exc}",
        ],
    )


def verification_result_for_sandbox_error(exc: BaseException) -> VerificationResult:
    """create/upload 失败 → 明确 ENV 标记，供 diagnose / fail_surface 使用。"""
    msg = str(exc).strip() or type(exc).__name__
    lower = msg.lower()
    if "upload" in lower:
        tag = "sandbox upload did not complete"
    else:
        tag = "sandbox create failed"
    return VerificationResult(
        all_passed=False,
        total_tests=0,
        failure_logs=[
            f"verify_config: {tag}",
            msg[:800],
        ],
    )


def timings_for_tar_error(exc: SandboxArchiveError) -> dict:
    return {
        "tar_error_code": exc.code,
        "tar_bytes": exc.total_bytes,
        "tar_max_bytes": exc.max_bytes,
        "tar_file_count": exc.file_count,
    }


_REQ_LINE_RE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9_.+-]*)\s*(?:[<>=!~]=?|[;]|$)",
)


def collect_declared_pip_packages(repo_path: str | Path) -> list[str]:
    """从 requirements / pyproject / setup.cfg / setup.py 提取可 pip 的包名。"""
    root = Path(repo_path)
    found: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        key = name.lower().replace("_", "-")
        if key in seen or key in {"python", "pip", "setuptools", "wheel"}:
            return
        seen.add(key)
        found.append(name)

    for req_name in (
        "requirements.txt",
        "requirements-dev.txt",
        "requirements/test.txt",
        "requirements/tests.txt",
    ):
        path = root / req_name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith(("#", "-")):
                continue
            m = _REQ_LINE_RE.match(s)
            if m:
                _add(m.group(1))

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = pyproject.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        for m in re.finditer(
            r'["\']([A-Za-z0-9][A-Za-z0-9_.+-]*)\s*(?:[<>=!~][^"\']*)?["\']',
            text,
        ):
            name = m.group(1)
            if name.lower() in {
                "build-system",
                "requires",
                "project",
                "tool",
                "optional-dependencies",
            }:
                continue
            window = text[max(0, m.start() - 80) : m.start()].lower()
            if "dependencies" in window or "requires" in window:
                _add(name)

    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file():
        try:
            text = setup_cfg.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        in_requires = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                in_requires = stripped.lower() in {"[options]", "[metadata]"}
                continue
            if stripped.lower().startswith("install_requires"):
                in_requires = True
                rest = stripped.split("=", 1)[-1].strip() if "=" in stripped else ""
                if rest:
                    m = _REQ_LINE_RE.match(rest)
                    if m:
                        _add(m.group(1))
                continue
            if in_requires and stripped and not stripped.startswith("["):
                m = _REQ_LINE_RE.match(stripped.lstrip("=").strip())
                if m:
                    _add(m.group(1))

    setup_py = root / "setup.py"
    if setup_py.is_file():
        try:
            text = setup_py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        block = re.search(
            r"install_requires\s*=\s*\[(.*?)\]",
            text,
            flags=re.DOTALL,
        )
        if block:
            for m in re.finditer(r"""['"]([A-Za-z0-9][A-Za-z0-9_.+-]*)""", block.group(1)):
                _add(m.group(1))

    return found


def repo_needs_pip_install(repo_path: str | Path) -> bool:
    """有包装声明或 requirements 时尝试 pip（含仅 setup.py 存在）。"""
    root = Path(repo_path)
    if (root / "setup.py").is_file() or (root / "setup.cfg").is_file():
        return True
    if (root / "pyproject.toml").is_file():
        return True
    for req_name in ("requirements.txt", "requirements-dev.txt"):
        if (root / req_name).is_file():
            return True
    return False


def maybe_pip_install(
    mgr: SandboxManager,
    sandbox: Sandbox,
    repo_path: str,
    cancel_token=None,
) -> tuple[str, int, ExecResult | None]:
    """有声明依赖时 pip install -e，并尽力补装 runtime deps。"""
    if not repo_needs_pip_install(repo_path):
        return "skipped (no project dependencies detected)", 0, None

    extras = collect_declared_pip_packages(repo_path)
    t0 = time.time()
    result = mgr.execute(
        sandbox,
        sandbox_pip_install_command(extra_packages=extras, repo_path=repo_path),
        timeout=BUILD_TIMEOUT_S,
        cancel_token=cancel_token,
    )
    pip_ms = int((time.time() - t0) * 1000)
    extra_note = f" extras={extras[:12]}" if extras else ""
    build_result = (
        f"pip install: exit_code={result.exit_code}{extra_note}\n{result.stdout}"
    )
    return build_result, pip_ms, result
