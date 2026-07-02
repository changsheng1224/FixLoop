"""进度回调：AgentLoop 向 CLI/REPL 报告实时进度。

Protocol 定义了 on_step_start / on_tool_executed / on_final_answer 三个事件。
CLIProgressCallback 是默认的终端友好实现（含 ANSI 彩色输出）。
"""

import sys
from typing import Protocol

# ANSI 颜色码
_RESET = "\033[0m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_BLUE = "\033[34m"
_YELLOW = "\033[33m"


class ProgressCallback(Protocol):
    """进度回调接口。"""

    def on_step_start(self, step: int, max_steps: int): ...
    def on_tool_executed(self, name: str, result_preview: str): ...
    def on_final_answer(self, text: str): ...


class CLIProgressCallback:
    """终端友好的进度回调实现（ANSI 彩色）。"""

    def __init__(self, output=sys.stderr):
        self._output = output
        self._step = 0

    def on_step_start(self, step: int, max_steps: int):
        self._step = step

    def on_tool_executed(self, name: str, result_preview: str):
        preview = result_preview[:80].replace("\n", " ")
        if "[DRY RUN]" in preview:
            color = _BLUE
            status = "DRY"
        elif "Error" in preview:
            color = _RED
            status = "FAIL"
        else:
            color = _GREEN
            status = "OK"
        print(
            f"  {color}[{self._step}] {name} {status}{_RESET} ({len(result_preview)} chars)",
            file=self._output,
        )

    def on_final_answer(self, text: str):
        preview = text[:100].replace("\n", " ")
        print(f"  {_GREEN}[done]{_RESET} → {preview}...", file=self._output)
