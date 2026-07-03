"""应用入口（含一个故意的 ImportError bug）。"""

from utils.helper import greet  # BUG: 模块名应为 helpers


def main() -> str:
    """返回问候语。"""
    return greet()
