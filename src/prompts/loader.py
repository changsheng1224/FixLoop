"""Agent system prompt 加载。"""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent
_SUFFIX_DIR = _PROMPTS_DIR / "patcher_suffix"
_LOCALIZER_HINTS_DIR = _PROMPTS_DIR / "localizer_hints"
_PATCHER_USER_HINTS_DIR = _PROMPTS_DIR / "patcher_user_hints"
_SKILL_MISS_DIR = _PROMPTS_DIR / "skill_miss"
_DEFAULT_SUFFIX = "default"
_DEFAULT_LOCALIZER_HINTS = "stack_first"


def load_system_prompt(name: str) -> str:
    """读取 ``src/prompts/{name}.txt``，不存在时返回空字符串。"""
    path = _PROMPTS_DIR / f"{name}.txt"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _load_variant_text(variants_dir: Path, key: str, default_key: str) -> str:
    normalized = (key or "").strip().lower() or default_key
    path = variants_dir / f"{normalized}.txt"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    fallback = variants_dir / f"{default_key}.txt"
    return fallback.read_text(encoding="utf-8").strip() if fallback.is_file() else ""


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
    return _load_variant_text(_LOCALIZER_HINTS_DIR, hints_key, _DEFAULT_LOCALIZER_HINTS)


def load_patcher_user_hint(name: str, **format_vars: object) -> str:
    """读取 patcher user 侧单条启发式提示，支持 ``{file_count}`` 等占位。"""
    text = _load_variant_text(_PATCHER_USER_HINTS_DIR, name, name)
    if format_vars:
        return text.format(**format_vars)
    return text


def load_skill_miss_hint(role: str) -> str:
    """读取 Skill 未命中时注入的通用 user 提示（localizer/retriever/patcher）。"""
    key = (role or "").strip().lower() or "patcher"
    return _load_variant_text(_SKILL_MISS_DIR, key, "patcher")
