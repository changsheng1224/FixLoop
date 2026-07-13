"""Agent 生命周期回调：统一 Middleware 链 + CLI 进度输出。

AgentCallback 基类定义 8 个生命周期钩子（全部默认 no-op），
子类按需覆盖。agent_loop 通过 ``_notify()`` 统一入口调用，
XML 与 Native 两条路径共用。

钩子列表（按执行序）:
1. on_step_start      — 每步开始
2. on_pre_model       — 模型调用前
3. on_post_model      — 模型返回后
4. on_pre_tool        — 工具执行前
5. on_post_tool       — 工具执行后（含结果摘要）
6. on_tool_executed   — 工具执行完成（含耗时和状态）
7. on_react_phase     — ReAct 阶段切换
8. on_final_answer    — 最终答案生成
"""

import sys
from typing import Any

# ANSI 颜色码
_RESET = "\033[0m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_BLUE = "\033[34m"
_YELLOW = "\033[33m"


class AgentCallback:
    """Agent 生命周期回调基类。

    所有方法默认 no-op，子类按需覆盖。agent_loop 在每次生命周期事件
    发生时通过 ``_notify()`` 统一调用，XML 与 Native 路径共用。
    """

    # ---- 步进 ----

    def on_step_start(self, step: int, max_steps: int, *, path: str = "") -> None:
        """每步开始（模型调用或工具执行前）。"""

    def on_final_answer(self, text: str) -> None:
        """最终答案生成（循环终止时）。"""

    # ---- 模型调用 ----

    def on_pre_model(
        self, step: int, prompt_preview: str, *, path: str = ""
    ) -> None:
        """模型调用前。prompt_preview 为 prompt 前 200 字符。"""

    def on_post_model(
        self, step: int, raw_preview: str, elapsed_ms: int, *, path: str = ""
    ) -> None:
        """模型返回后。raw_preview 为原始输出前 200 字符。"""

    # ---- 工具执行 ----

    def on_pre_tool(
        self, step: int, tool_name: str, tool_args: dict, *, path: str = ""
    ) -> None:
        """工具执行前（Gateway 权限检查通过后，Executor 闸口前）。"""

    def on_post_tool(
        self,
        step: int,
        tool_name: str,
        result_preview: str,
        elapsed_ms: int,
        *,
        path: str = "",
    ) -> None:
        """工具执行后。result_preview 为结果前 200 字符。"""

    def on_tool_executed(
        self, step: int, name: str, result_preview: str, elapsed_ms: int, status: str
    ) -> None:
        """工具执行完成（含耗时和状态）。status ∈ {OK, FAIL, DRY}。"""

    # ---- ReAct 阶段 ----

    def on_react_phase(
        self, phase: str, step: int, max_steps: int, *, tool: str = ""
    ) -> None:
        """ReAct 阶段切换。phase ∈ {reasoning, acting, observation, recording}。"""


# ---- 向后兼容别名 ----

ProgressCallback = AgentCallback  # 旧名可用


# ---- Callback 链 ----

class CallbackChain(AgentCallback):
    """回调链：按序调用多个 AgentCallback，任一异常 log + 继续。

    AgentLoop 只持有 Chain；CLIProgressCallback 固定为链末。
    """

    def __init__(self, callbacks: list[AgentCallback], *, fail_fast: bool = False):
        self._callbacks = list(callbacks)
        self._fail_fast = fail_fast

    def add(self, cb: AgentCallback) -> None:
        self._callbacks.append(cb)

    def _notify_chain(self, method: str, **kwargs: object) -> None:
        import logging

        logger = logging.getLogger("fixloop.callbacks")
        for cb in self._callbacks:
            try:
                fn = getattr(cb, method, None)
                if fn is not None:
                    fn(**kwargs)
            except Exception:
                logger.warning("CallbackChain: %s in %s failed", method, type(cb).__name__)
                if self._fail_fast:
                    raise

    # 自动代理所有钩子
    def on_step_start(self, step: int, max_steps: int, *, path: str = "") -> None:
        self._notify_chain("on_step_start", step=step, max_steps=max_steps, path=path)

    def on_final_answer(self, text: str) -> None:
        self._notify_chain("on_final_answer", text=text)

    def on_pre_model(self, step: int, prompt_preview: str, *, path: str = "") -> None:
        self._notify_chain("on_pre_model", step=step, prompt_preview=prompt_preview, path=path)

    def on_post_model(self, step: int, raw_preview: str, elapsed_ms: int, *, path: str = "") -> None:
        self._notify_chain("on_post_model", step=step, raw_preview=raw_preview, elapsed_ms=elapsed_ms, path=path)

    def on_pre_tool(self, step: int, tool_name: str, tool_args: dict, *, path: str = "") -> None:
        self._notify_chain("on_pre_tool", step=step, tool_name=tool_name, tool_args=tool_args, path=path)

    def on_post_tool(self, step: int, tool_name: str, result_preview: str, elapsed_ms: int, *, path: str = "") -> None:
        self._notify_chain("on_post_tool", step=step, tool_name=tool_name, result_preview=result_preview, elapsed_ms=elapsed_ms, path=path)

    def on_tool_executed(self, step: int, name: str, result_preview: str, elapsed_ms: int, status: str) -> None:
        self._notify_chain("on_tool_executed", step=step, name=name, result_preview=result_preview, elapsed_ms=elapsed_ms, status=status)

    def on_react_phase(self, phase: str, step: int, max_steps: int, *, tool: str = "") -> None:
        self._notify_chain("on_react_phase", phase=phase, step=step, max_steps=max_steps, tool=tool)


# ---- CLI 终端实现 ----

class CLIProgressCallback(AgentCallback):
    """终端友好的进度回调（ANSI 彩色输出到 stderr）。

    仅覆盖 on_step_start / on_tool_executed / on_react_phase / on_final_answer。
    """

    def __init__(self, output=sys.stderr):
        self._output = output
        self._step = 0
        self._t0: float | None = None

    # ---- 覆盖的方法 ----

    def on_step_start(self, step: int, max_steps: int, *, path: str = "") -> None:
        """记录当前步数。"""
        self._step = step

    def on_react_phase(
        self, phase: str, step: int, max_steps: int, *, tool: str = ""
    ) -> None:
        """向 stderr 打印 ReAct 阶段行。"""
        suffix = f" {tool}" if tool else ""
        print(
            f"  {_YELLOW}[{step}/{max_steps}] {phase}{suffix}{_RESET}",
            file=self._output,
        )

    def on_tool_executed(
        self, step: int, name: str, result_preview: str, elapsed_ms: int, status: str
    ) -> None:
        """向 stderr 打印彩色工具执行行。"""
        import time

        if self._t0 is None:
            self._t0 = time.time()
        elapsed = time.time() - self._t0
        self._t0 = time.time()

        preview = result_preview[:80].replace("\n", " ")
        if status == "DRY":
            color = _BLUE
        elif status == "FAIL":
            color = _RED
        else:
            color = _GREEN
        print(
            f"  {color}[{self._step}] {name} {status} ({elapsed:.1f}s){_RESET}",
            file=self._output,
        )

    def on_final_answer(self, text: str) -> None:
        """向 stderr 打印最终回答摘要。"""
        preview = text[:100].replace("\n", " ")
        print(f"  {_GREEN}[done]{_RESET} → {preview}...", file=self._output)
