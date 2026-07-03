"""Agent system prompt 加载。"""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


def load_system_prompt(name: str) -> str:
    """读取 ``src/prompts/{name}.txt``，不存在时返回空字符串。"""
    path = _PROMPTS_DIR / f"{name}.txt"
    return path.read_text(encoding="utf-8") if path.is_file() else ""
