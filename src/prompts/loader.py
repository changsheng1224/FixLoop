"""Agent system prompt 加载。"""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent
_SUFFIX_DIR = _PROMPTS_DIR / "patcher_suffix"
_LOCALIZER_HINTS_DIR = _PROMPTS_DIR / "localizer_hints"
_DEFAULT_SUFFIX = "default"
_DEFAULT_LOCALIZER_HINTS = "stack_first"


def load_system_prompt(name: str) -> str:
    """读取 ``src/prompts/{name}.txt``，不存在时返回空字符串。"""
    path = _PROMPTS_DIR / f"{name}.txt"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def load_role_prompt(role: str, issue_type: str = "") -> str:
    """加载角色 base prompt；patcher 可追加 issue_type 后缀。"""
    base = load_system_prompt(role).strip()
    if role != "patcher":
        return base
    suffix = _load_patcher_suffix(issue_type)
    if not suffix:
        return base
    return f"{base}\n\n{suffix}"


def _read_suffix_file(name: str) -> str:
    path = _SUFFIX_DIR / f"{name}.txt"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


def _load_patcher_suffix(issue_type: str) -> str:
    key = (issue_type or "").strip().lower()
    if not key:
        return ""
    text = _read_suffix_file(key)
    if text:
        return text
    return _read_suffix_file(_DEFAULT_SUFFIX)


def load_localizer_hints(hints_key: str = "") -> str:
    """读取 localizer user 模板中的 ``$issue_type_hints`` 文案。"""
    key = (hints_key or "").strip().lower() or _DEFAULT_LOCALIZER_HINTS
    path = _LOCALIZER_HINTS_DIR / f"{key}.txt"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    fallback = _LOCALIZER_HINTS_DIR / f"{_DEFAULT_LOCALIZER_HINTS}.txt"
    return fallback.read_text(encoding="utf-8").strip() if fallback.is_file() else ""
