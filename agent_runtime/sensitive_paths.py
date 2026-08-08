"""敏感路径策略：拦截 .env / 密钥 / 凭证等读写。"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

# 精确或通配（相对 workspace 的 path 字符串 / basename）
SENSITIVE_BASENAME_GLOBS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "credentials",
    "credentials.*",
    "credentials.json",
    "service-account*.json",
    "*.keystore",
    "secrets.yaml",
    "secrets.yml",
    "secrets.json",
)

SENSITIVE_PATH_SUBSTRINGS: tuple[str, ...] = (
    "/.ssh/",
    "\\.ssh\\",
    "/.aws/",
    "\\.aws\\",
    "/.gnupg/",
    "\\.gnupg\\",
)

# Repository control-plane and deployment credential files.  These are
# intentionally path-pattern based so nested CI/deployment files are covered
# without blocking ordinary source files named ``config.py``.
SENSITIVE_PATH_GLOBS: tuple[str, ...] = (
    ".git/config",
    ".gitmodules",
    ".npmrc",
    ".pypirc",
    ".docker/config.json",
    ".terraformrc",
    "*.tfvars",
    "*.tfvars.json",
    "kubeconfig",
    "*/kubeconfig",
    ".github/workflows/*",
    ".gitlab-ci.yml",
    ".circleci/config.yml",
    "azure-pipelines.yml",
    "Jenkinsfile",
    "docker-compose*.yml",
    "docker-compose*.yaml",
)

_WRITE_TOOLS = frozenset({"write_file", "patch_file"})
_READ_TOOLS = frozenset({"read_file", "grep", "search", "inspect_file", "ast_parse"})


def is_sensitive_path(path: str | Path) -> bool:
    """判断路径是否命中敏感策略（不抛异常）。"""
    text = str(path or "").replace("\\", "/")
    if not text:
        return False
    lower = text.lower()
    for sub in SENSITIVE_PATH_SUBSTRINGS:
        if sub.replace("\\", "/").lower() in lower:
            return True
    normalized = lower.lstrip("./")
    if any(
        fnmatch.fnmatch(normalized, pattern.lower())
        or fnmatch.fnmatch(normalized, pattern.lower().lstrip("./"))
        or fnmatch.fnmatch(normalized, f"*/{pattern.lower().lstrip('./')}")
        for pattern in SENSITIVE_PATH_GLOBS
    ):
        return True
    name = Path(text).name
    for pat in SENSITIVE_BASENAME_GLOBS:
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(name.lower(), pat.lower()):
            return True
    # 隐藏 env 变体：.env.local 已由 .env.* 覆盖；再兜底 env 文件
    if re.fullmatch(r"\.env(\..+)?", name, flags=re.I):
        return True
    return False


def check_sensitive_access(tool_name: str, path: str | Path) -> str | None:
    """若禁止访问返回错误码字符串，否则 None。

    Returns:
        ``sensitive_path`` 或 ``None``。
    """
    if not path:
        return None
    if not is_sensitive_path(path):
        return None
    if tool_name in _WRITE_TOOLS or tool_name in _READ_TOOLS or tool_name in (
        "list_files",
        "find_test",
    ):
        return "sensitive_path"
    # 未知带 path 的工具也拦截
    return "sensitive_path"


def sensitive_reject_message(path: str | Path) -> str:
    return f"Error: 敏感路径被安全策略拒绝: {path}"
