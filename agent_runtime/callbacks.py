"""进度回调：AgentLoop 向 CLI/REPL 报告实时进度。

Protocol 定义了 on_step_start / on_tool_executed / on_final_answer 三个事件。
CLIProgressCallback 是默认的终端友好实现。
"""

import sys
from typing import Protocol


class ProgressCallback(Protocol):
    """进度回调接口。"""

    def on_step_start(self, step: int, max_steps: int): ...
    def on_tool_executed(self, name: str, result_preview: str): ...
    def on_final_answer(self, text: str): ...


class CLIProgressCallback:
    """终端友好的进度回调实现。

    输出格式：
        [1/6] list_files(".")... ✅ (320 chars)
        [2/6] read_file("config.py")... ✅ (1100 chars)
    """

    def __init__(self, output=sys.stderr):
        self._output = output
        self._step = 0

    def on_step_start(self, step: int, max_steps: int):
        self._step = step

    def on_tool_executed(self, name: str, result_preview: str):
        preview = result_preview[:80].replace("\n", " ")
        status = "❌" if "Error" in preview else "✅"
        print(
            f"  [{self._step}] {name}(...) {status} ({len(result_preview)} chars)",
            file=self._output,
        )

    def on_final_answer(self, text: str):
        preview = text[:100].replace("\n", " ")
        print(f"  [done] → {preview}...", file=self._output)
