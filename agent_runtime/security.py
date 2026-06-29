"""安全模块：Shell 环境白名单 + 敏感信息脱敏。

三层防护：
L1 (运行时): Shell 环境变量白名单——只传安全变量给子进程
L2 (输出前): trace/report 中的 API Key / Token 正则替换为 <redacted>
L3 (持久化前): .env 不入索引
"""

import os

# Shell 子进程允许透传的环境变量白名单
SHELL_ENV_WHITELIST = {
    "HOME",
    "PATH",
    "PWD",
    "TEMP",
    "TMP",
    "USER",
    "USERNAME",
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
}


def shell_env(allowlist: set[str] | None = None, root: str = "") -> dict:
    """返回经过白名单过滤的安全环境变量。

    Args:
        allowlist: 允许透传的变量名集合，默认使用 SHELL_ENV_WHITELIST。
        root: workspace 根目录，会覆盖 PWD。

    Returns:
        白名单过滤后的环境变量字典。
    """
    allowed = allowlist or SHELL_ENV_WHITELIST
    result = {}
    for key in allowed:
        val = os.environ.get(key, "")
        if val:
            result[key] = val
    # 覆盖 PWD 为 workspace 根目录
    if root:
        result["PWD"] = root
    return result


def looks_sensitive_env_name(name: str) -> bool:
    """检测变量名是否疑似敏感（含 API_KEY/TOKEN/SECRET/PASSWORD 等关键词）。

    Args:
        name: 环境变量名。

    Returns:
        True 表示疑似敏感变量。
    """
    sensitive = {"API_KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "CREDENTIAL"}
    upper = name.upper()
    return any(kw in upper for kw in sensitive)


def redact_text(text: str, secret_values: list[str] | None = None) -> str:
    """将文本中的敏感值替换为 <redacted>。

    Args:
        text: 原始文本。
        secret_values: 额外的敏感值列表（从环境变量自动检测）。

    Returns:
        脱敏后的文本。
    """
    if secret_values is None:
        secret_values = _detect_secret_values()

    result = text
    for val in secret_values:
        if val and len(val) > 4:  # 跳过太短的值（容易误匹配）
            result = result.replace(val, "<redacted>")
    return result


def _detect_secret_values() -> list[str]:
    """从当前环境变量中自动检测敏感值。"""
    values = []
    for key, val in os.environ.items():
        if looks_sensitive_env_name(key):
            values.append(val)
    return values
