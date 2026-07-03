"""从 pyproject.toml 读取项目配置（含故意缺失的 tool 段）。"""

from pathlib import Path

import tomllib


def load_multiplier() -> int:
    """读取 [tool.eval] multiplier，用于业务缩放。"""
    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    return int(data["tool"]["eval"]["multiplier"])  # BUG: pyproject 缺少 [tool.eval]
