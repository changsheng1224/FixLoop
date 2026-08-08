"""安全模块：Shell 环境白名单 + 敏感信息脱敏。

三层防护：
L1 (运行时): Shell 环境变量白名单——只传安全变量给子进程
L2 (输出前): trace/report 中的 API Key / Token 正则替换为 <redacted>
L3 (持久化前): .env 不入索引
"""

import os
import shlex

# Shell 子进程允许透传的环境变量白名单（严格固定，不可运行时扩展）
SHELL_ENV_WHITELIST = frozenset(
    {
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
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "WINDIR",
    }
)


def shell_env(allowlist: set[str] | frozenset[str] | None = None, root: str = "") -> dict:
    """返回经过白名单过滤的安全环境变量。

    仅透传 allowlist 中的键；键名命中敏感词规则的一律剔除；永不复制全量 os.environ。
    """
    allowed = allowlist or SHELL_ENV_WHITELIST
    result: dict[str, str] = {}
    for key in allowed:
        if looks_sensitive_env_name(key):
            continue
        val = os.environ.get(key, "")
        if val:
            result[key] = val
    if root:
        result["PWD"] = root
    result["PYTHONIOENCODING"] = "utf-8"
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


# Shell 命令白名单/黑名单（仅匹配命令名，不含参数）
SHELL_COMMAND_WHITELIST = frozenset(
    {
        "pytest",
        "python",
        "python3",
        "py",
        "git",
        "rg",
        "grep",
        "find",
        "ls",
        "dir",
        "cat",
        "head",
        "tail",
        "wc",
        "echo",
        "env",
        "set",
        "test",
        "ruff",
        "mypy",
        "black",
        "isort",
        "pip",
        "poetry",
        "npm",
        "yarn",
        "cp",
        "mv",
        "rm",
        "mkdir",
        "rmdir",
        "curl",
        "wget",
    }
)

SHELL_COMMAND_BLOCKLIST = frozenset(
    {
        "sudo",
        "su",
        "chmod",
        "chown",
        "mount",
        "umount",
        "reboot",
        "shutdown",
        "halt",
        "poweroff",
        "dd",
        "mkfs",
        "fdisk",
        "parted",
        "iptables",
        "ufw",
        "firewall-cmd",
        "kill",
        "pkill",
        "killall",
        "docker",
        "podman",
        "kubectl",
        "ssh",
        "scp",
        "rsync",
        "nc",
        "wget",  # 比 curl 更可能下载恶意 payload
    }
)


def check_shell_command(command: str) -> tuple[bool, str]:
    """检查 Shell 命令是否在白名单内/黑名单外。

    Returns:
        (allowed, reason): allowed=True 表示允许执行。
    """
    if not command or not command.strip():
        return False, "空命令"
    try:
        argv = parse_shell_argv(command)
    except ValueError as exc:
        return False, str(exc)
    if not argv:
        return False, "空命令"
    cmd_name = argv[0].split("/")[-1].split("\\")[-1].lower()
    if cmd_name in SHELL_COMMAND_BLOCKLIST:
        return False, f"blocked: {cmd_name}"
    if cmd_name in SHELL_COMMAND_WHITELIST:
        return True, f"whitelist: {cmd_name}"
    # 不在任一列表中 → 保守拒绝
    return False, f"not in whitelist: {cmd_name}"


def parse_shell_argv(command: str) -> list[str]:
    """Parse a single executable command without invoking a shell.

    Shell composition is deliberately rejected. Callers should pass the
    returned argv to ``subprocess`` with ``shell=False``.
    """
    text = str(command or "").strip()
    if not text or "\x00" in text:
        raise ValueError("空命令或非法 NUL 字符")
    # ``shell=False`` treats a plain semicolon as an argument (and common
    # ``python -c`` snippets rely on it); reject operators that compose
    # commands or redirect streams.
    if any(operator in text for operator in ("&&", "||", "|", ">", "<", "`", "$(", "${")):
        raise ValueError("禁止 Shell 运算符/命令替换；请使用单一 argv 命令")
    try:
        argv = shlex.split(text, posix=os.name != "nt")
    except ValueError as exc:
        raise ValueError(f"Shell 参数解析失败: {exc}") from exc
    if not argv:
        raise ValueError("空命令")
    if os.name == "nt":
        argv = [
            token[1:-1]
            if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}
            else token
            for token in argv
        ]
    # Python -c remains useful for deterministic probes, but it must not become
    # a second shell or an unrestricted file/network side-effect escape hatch.
    if argv[0].split("/")[-1].split("\\")[-1].lower() in {"python", "python3", "py"}:
        if "-c" in argv:
            code = argv[argv.index("-c") + 1] if argv.index("-c") + 1 < len(argv) else ""
            if any(token in code for token in ("os.system", "subprocess", "shutil.rmtree", "socket.", "urllib.")):
                raise ValueError("python -c 包含被禁止的进程/网络副作用")
    return argv


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
            "token_usage_by_agent",
            "tool_usage_by_agent",
            "total_tool_steps",
            "total_tokens",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "cache_hit_rate",
            "estimated_total",
            "estimated_sections",
            "run_count",
            "api",
            "api_calls",
            "tool_steps",
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
