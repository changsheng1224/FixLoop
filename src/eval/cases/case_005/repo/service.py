"""服务层（含错误的符号 import）。"""

from utils.helpers import hello  # BUG: 函数名为 greet


def message() -> str:
    return hello()
