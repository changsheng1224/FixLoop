"""可跑环境：按仓库形态选择验证 runner / 环境变量（能力向，非单例）。

当前覆盖：
- 默认 pytest + PYTHONPATH
- Django 源码树（tests/runtests.py + django/）→ runtests runner
- 可选 DJANGO_SETTINGS_MODULE 探测（pytest 路径兜底）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from src.harness.sandbox_manager import sandbox_pythonpath_prefix

__all__ = [
    "VerifyProfile",
    "detect_verify_profile",
    "build_verify_env_prefix",
    "django_labels_from_target",
    "build_django_runtests_command",
    "parse_django_runtests_output",
]


@dataclass(frozen=True)
class VerifyProfile:
    kind: str = "pytest"  # pytest | django_runtests
    settings_module: str = ""
    reasons: tuple[str, ...] = ()
    runtests_path: str = ""


def detect_verify_profile(repo_root: str | Path | None) -> VerifyProfile:
    """根据仓库布局探测验证配置。"""
    if not repo_root:
        return VerifyProfile(kind="pytest", reasons=("no_repo",))
    root = Path(repo_root)
    reasons: list[str] = []

    runtests = root / "tests" / "runtests.py"
    django_pkg = root / "django" / "__init__.py"
    if runtests.is_file() and django_pkg.is_file():
        settings = _discover_django_settings_module(root)
        reasons.append("tests/runtests.py+django/")
        if settings:
            reasons.append(f"settings={settings}")
        return VerifyProfile(
            kind="django_runtests",
            settings_module=settings,
            reasons=tuple(reasons),
            runtests_path="tests/runtests.py",
        )

    # 非完整 django 树但存在 settings 线索 → 仍用 pytest，附加 settings
    settings = _discover_django_settings_module(root)
    if settings:
        return VerifyProfile(
            kind="pytest",
            settings_module=settings,
            reasons=("django_settings_hint", f"settings={settings}"),
        )
    return VerifyProfile(kind="pytest", reasons=("default_pytest",))


def _discover_django_settings_module(root: Path) -> str:
    """探测常见 Django 测试 settings 模块名。"""
    candidates = [
        ("tests/test_sqlite.py", "tests.test_sqlite"),
        ("tests/settings.py", "tests.settings"),
        ("test_sqlite.py", "test_sqlite"),
        ("settings/test.py", "settings.test"),
    ]
    for rel, mod in candidates:
        if (root / rel).is_file():
            return mod
    # 任意 tests/*settings*.py
    tests_dir = root / "tests"
    if tests_dir.is_dir():
        for path in sorted(tests_dir.glob("*settings*.py")):
            stem = path.stem
            if stem.startswith("_"):
                continue
            return f"tests.{stem}"
    return ""


def build_verify_env_prefix(
    repo_root: str | Path | None,
    profile: VerifyProfile | None = None,
) -> str:
    """组合 PYTHONPATH + 可选 DJANGO_SETTINGS_MODULE。"""
    profile = profile or detect_verify_profile(repo_root)
    parts = [sandbox_pythonpath_prefix(repo_root)]
    if profile.settings_module:
        # 仅允许安全模块路径字符
        mod = profile.settings_module.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", mod):
            parts.append(f'export DJANGO_SETTINGS_MODULE="{mod}"')
    return " && ".join(parts)


def django_labels_from_target(test_path: str) -> list[str]:
    """把 pytest 风格 target / 路径收成 django runtests labels。"""
    raw = (test_path or "").strip().replace("\\", "/")
    if not raw or raw in (".", "/code", "/code/"):
        return []
    if raw.startswith("/code/"):
        raw = raw[len("/code/") :]

    file_part, _, node = raw.partition("::")
    file_part = file_part.lstrip("./")
    if file_part.startswith("tests/"):
        file_part = file_part[len("tests/") :]
    if file_part.endswith(".py"):
        file_part = file_part[: -len(".py")]
    label = file_part.replace("/", ".").strip(".")
    if not label:
        # bare test name
        bare = raw.strip()
        if bare.isidentifier() and bare.startswith("test_"):
            return [bare]
        return []

    if node:
        node_parts = [p.split("[", 1)[0].strip() for p in node.split("::") if p.strip()]
        if node_parts:
            return [".".join([label, *node_parts])]
    return [label]


def build_django_runtests_command(
    labels: list[str],
    *,
    runtests_path: str = "tests/runtests.py",
    settings_module: str = "",
    verbosity: int = 1,
) -> str:
    """构造容器内 django runtests 命令（相对 /code）。"""
    script = runtests_path.replace("\\", "/").lstrip("./")
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", script):
        script = "tests/runtests.py"
    args: list[str] = [f"python {script}", f"--verbosity={int(verbosity)}"]
    if settings_module and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", settings_module):
        args.append(f"--settings={settings_module}")
    safe_labels: list[str] = []
    for lab in labels:
        lab = lab.strip()
        if lab and re.fullmatch(r"[A-Za-z0-9_.]+", lab):
            safe_labels.append(lab)
        if len(safe_labels) >= 8:
            break
    if safe_labels:
        args.extend(safe_labels)
    return " ".join(args)


_RAN_RE = re.compile(r"Ran\s+(\d+)\s+tests?", re.I)
_FAIL_RE = re.compile(r"FAILED\s*\(([^)]*)\)", re.I)
_OK_RE = re.compile(r"\bOK\b")
_ERRORS_RE = re.compile(r"errors?\s*=\s*(\d+)", re.I)
_FAILURES_RE = re.compile(r"failures?\s*=\s*(\d+)", re.I)


def parse_django_runtests_output(
    stdout: str,
    *,
    exit_code: int,
    labels: list[str] | None = None,
) -> "VerificationResult":
    """解析 runtests 文本输出为 VerificationResult。"""
    from src.state import VerificationResult

    text = stdout or ""
    ran = 0
    m = _RAN_RE.search(text)
    if m:
        ran = int(m.group(1))

    failures = 0
    errors = 0
    fail_m = _FAIL_RE.search(text)
    if fail_m:
        blob = fail_m.group(1)
        fm = _FAILURES_RE.search(blob)
        em = _ERRORS_RE.search(blob)
        if fm:
            failures = int(fm.group(1))
        if em:
            errors = int(em.group(1))
    elif exit_code != 0 and ran:
        failures = max(1, ran)  # 未知细分时至少记失败

    passed = max(0, ran - failures - errors) if ran else 0
    all_passed = exit_code == 0 and failures == 0 and errors == 0 and ran > 0

    logs: list[str] = []
    if ran == 0 and exit_code != 0:
        logs.append("verify_config: django runtests collected/ran 0 tests")
        if labels:
            logs.append("labels=" + ",".join(labels[:5]))
    elif not all_passed:
        logs.append(f"django_runtests: exit={exit_code} ran={ran} failures={failures} errors={errors}")
    # 附带 stderr/stdout 尾部
    tail = text.strip()[-1200:]
    if tail:
        logs.append(f"runtests_stdout_tail:\n{tail}")

    return VerificationResult(
        all_passed=all_passed,
        total_tests=ran,
        passed=passed if ran else 0,
        failed=failures,
        error=errors,
        failure_logs=logs,
    )
