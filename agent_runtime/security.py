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
    for kw in sensitive:
        if kw in upper:
            # 避免子串误判：total_tokens 含 TOKEN 但不应匹配
            # 只有 kw 出现在边界位置才判定为敏感
            idx = upper.find(kw)
            before_ok = idx == 0 or not upper[idx - 1].isalpha()
            after_ok = (idx + len(kw) == len(upper)) or not upper[idx + len(kw)].isalpha()
            if before_ok and after_ok:
                return True
    return False


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


def redact_artifact(value, secret_values: list[str] | None = None):
    """递归脱敏 dict/list/str 中的敏感值。

    遇到 key 名含敏感词 → 值替换为 "<redacted>"。
    遇到 str 值匹配敏感值列表 → 替换为 "<redacted>"。

    Args:
        value: 任意值（dict/list/str/其他）。
        secret_values: 已知敏感值列表（从环境变量自动检测）。

    Returns:
        脱敏后的副本。
    """
    if secret_values is None:
        secret_values = _detect_secret_values()

    if isinstance(value, dict):
        safe_metric_keys = {
            "token_usage",
            "total_tokens",
            "input_tokens",
            "output_tokens",
            "estimated_total",
            "estimated_sections",
            "run_count",
            "api",
        }
        result = {}
        for k, v in value.items():
            if k in safe_metric_keys:
                result[k] = redact_artifact(v, secret_values)
            elif looks_sensitive_env_name(k):
                result[k] = "<redacted>"
            else:
                result[k] = redact_artifact(v, secret_values)
        return result
    elif isinstance(value, list):
        return [redact_artifact(v, secret_values) for v in value]
    elif isinstance(value, str):
        for sv in secret_values:
            if sv and len(sv) > 4 and sv in value:
                value = value.replace(sv, "<redacted>")
        return value
    return value
