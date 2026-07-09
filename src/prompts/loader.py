"""Agent system prompt 加载。"""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent
_SUFFIX_DIR = _PROMPTS_DIR / "patcher_suffix"

_VALID_PATCHER_SUFFIXES = frozenset(
    {
        "type_error",
        "import_error",
        "logic_error",
        "attribute_error",
        "config_error",
        "composite",
        "default",
    }
)


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


def _load_patcher_suffix(issue_type: str) -> str:
    key = (issue_type or "").strip().lower()
    if not key:
        return ""
    if key not in _VALID_PATCHER_SUFFIXES:
        key = "default"
    path = _SUFFIX_DIR / f"{key}.txt"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    fallback = _SUFFIX_DIR / "default.txt"
    if fallback.is_file():
        return fallback.read_text(encoding="utf-8").strip()
    return ""
