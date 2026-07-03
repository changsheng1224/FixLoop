"""应用入口（含故意的 import 路径 bug）。"""

from utils.helper import greet  # BUG: 模块名为 helpers


def run() -> str:
    return greet()
