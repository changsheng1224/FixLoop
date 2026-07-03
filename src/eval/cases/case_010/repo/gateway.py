"""网关层（含错误的 backend import）。"""

from backend.service import run_task  # BUG: 模块在 backend.tasks


def invoke(a, b):
    return run_task(a, b)
