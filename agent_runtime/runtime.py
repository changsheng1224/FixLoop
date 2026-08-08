"""Agent 运行时：Agent 类 + 模型输出解析 + ask() 入口。

Agent 是最外层的用户接口，封装了模型客户端、工具注册表、工作区和上下文管理器。
"""

import json
from pathlib import Path
from typing import Literal

from agent_runtime.config import AgentConfig
from agent_runtime.prompt_prefix import build_prompt_prefix
from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import build_tool_registry

PrefixMode = Literal["default", "repair"]


def _role_quota(agent_name: str = ""):
    """按 Agent 角色返回差异化配额。

    Patcher:   写文件宽松 → writes=8 shell=2 total=15
    Verifier:  sandbox 仅容器操作 → shell=3 total=6
    """
    from agent_runtime.tool_executor import QuotaEnforcer

    role = (agent_name or "").lower()
    if role == "patcher":
        return QuotaEnforcer(
            max_writes=8,
            max_shell=2,
            max_total=24,
            group_limits={"read": 12, "write": 8, "verify": 2, "recovery": 2},
        )
    if role == "verifier":
        return QuotaEnforcer(
            max_writes=0,
            max_shell=3,
            max_total=6,
            group_limits={"read": 0, "write": 0, "verify": 6, "recovery": 0},
        )
    return QuotaEnforcer()


class Agent:
    """手写的 LLM Agent。

    封装模型客户端、工具注册、工作区上下文和 prompt 组装。
    """

    def __init__(
        self,
        config: AgentConfig,
        model_client,
        workspace,
        cwd: str | None = None,
        light_client=None,
        dry_run: bool = False,
        tools: dict | None = None,
        system_prompt: str = "",
        agent_name: str = "",
        tool_dispatch=None,
        prefix_mode: PrefixMode = "default",
        l1_prefix=None,
        warm_context=None,
    ):
        self.config = config
        self.model_client = model_client
        self.light_client = light_client
        self.dry_run = dry_run
        self.workspace = workspace
        self._cwd = cwd or workspace.repo_root or str(Path.cwd())
        self._system_prompt = system_prompt
        self._agent_name = agent_name
        self._tool_dispatch = tool_dispatch
        self._prefix_mode: PrefixMode = prefix_mode
        self._l1_prefix = l1_prefix
        self._warm_context = warm_context  # 预热上下文（可选），供 ContextManager 复用
        self._budget = None  # 共享 TokenBudget（可选），RepairBudgetContext 注入
        self.shared_run_id: str | None = None
        self._last_budget_meta: dict = {}
        self.cancel_token = None
        self._active_cancel_token = None

        # 构建工具上下文和注册表（允许外部注入）
        self.tool_context = ToolContext(root=self._cwd)
        self.tools = tools if tools is not None else build_tool_registry(self.tool_context)
        self._tool_names = tuple(sorted(self.tools.keys()))

        # 会话状态 + 记忆
        self.session: dict = self._new_session()

        # 缓存 prefix（工具不变时复用，支持自定义 prompt）
        self._prefix = self._build_prefix(system_prompt)

        # M4 模块：配额 + 熔断 + 语义记忆（懒加载，避免多 Agent 构造时重复阻塞）
        from agent_runtime.providers.circuit_breaker import CircuitBreaker

        self._semantic_memory = None
        self.circuit_breaker = CircuitBreaker()
        self.quota = _role_quota(agent_name)

    @property
    def agent_name(self) -> str:
        """角色名（patcher/verifier）；供 AgentLoop stall 等判定。"""
        return self._agent_name or ""

    @property
    def semantic_memory(self):
        """首次访问时加载语义模型（全局单例，线程安全）。"""
        if self._semantic_memory is None:
            from agent_runtime.features.memory import SemanticMemory

            self._semantic_memory = SemanticMemory()
        return self._semantic_memory

    # ---- 公开方法 ----

    def ask(
        self, user_message: str, callback=None, *, skip_plan: bool = False, stream: bool = False
    ) -> str:
        """执行一次用户请求，返回最终答案。

        Args:
            user_message: 用户输入。
            callback: 可选的 ProgressCallback 实例（streaming 时需含 on_chunk）。
            skip_plan: L2 repair 等场景跳过 plan 阶段（避免额外 LLM 调用）。
            stream: 启用流式输出（REPL --stream 模式）。

        Returns:
            模型返回的最终答案文本。
        """
        from agent_runtime.agent_loop import AgentLoop
        from agent_runtime.cancellation import CancellationToken

        # workspace 切换检测：cwd 变更时重建 workspace + invalidate prefix + 清空 memory
        self._detect_workspace_switch()

        if self.cancel_token is None or self.cancel_token.is_cancelled:
            self.cancel_token = CancellationToken()
        self._active_cancel_token = self.cancel_token

        loop = AgentLoop(agent=self, stream=stream)
        return loop.run(user_message, callback=callback, skip_plan=skip_plan)

    def _detect_workspace_switch(self) -> None:
        """检测 cwd/root_hash 是否变更；变更则重建 workspace、prefix、清空 working memory。

        比较 WorkspaceContext.fingerprint (SHA256 of HEAD + dirty files + docs)
        与上次记录的 _last_root_hash，变更时 invalidate prefix hash 并清空 recent_files。
        """
        import os

        current = os.getcwd()
        current_hash = (
            self.workspace.fingerprint() if hasattr(self.workspace, "fingerprint") else ""
        )
        last_cwd = getattr(self, "_last_cwd", None)
        last_hash = getattr(self, "_last_root_hash", None)

        # 首次初始化：记录基线
        if last_cwd is None:
            self._last_cwd = current
            self._last_root_hash = current_hash
            return

        # 无变更
        if current == last_cwd and current_hash == last_hash:
            return

        # cwd/root_hash 变更 → 重建
        from agent_runtime.workspace import WorkspaceContext

        self._last_cwd = current
        self._last_root_hash = current_hash
        self._cwd = current
        self.workspace = WorkspaceContext.build(current)
        self.tool_context = ToolContext(root=current)
        # 清空 working memory（旧 workspace 的文件已失效）
        session = getattr(self, "session", {}) or {}
        mem = session.get("memory", {})
        working = mem.get("working", {})
        if isinstance(working, dict):
            working["recent_files"] = []
            working["evidence_ledger"] = []
            working["read_cache"] = {}
        if isinstance(mem, dict):
            mem["file_summaries"] = {}
        # 重建 prefix（workspace snapshot 变更影响 hash → prompt cache 失效）
        try:
            sp = self._system_prompt if hasattr(self, "_system_prompt") else ""
            self._prefix = self._build_prefix(sp)
        except Exception:
            pass

    def complete_once(self, user_message: str, *, system_prompt: str | None = None) -> str:
        """单次 LLM completion，不进入 AgentLoop。

        默认使用构造时的 ``system_prompt``；传入 ``system_prompt`` 可 per-call 覆盖
        （如 Patcher 按 issue_type 注入变体，且故意不含 L1 repair prefix）。
        """
        user_message, budget_meta = self.fit_user_message(
            user_message, system_override=system_prompt
        )
        self._last_budget_meta = budget_meta
        prefix = system_prompt if system_prompt is not None else self._system_prompt
        full_prompt = f"{prefix}\n\n{user_message}" if prefix else user_message
        from agent_runtime.cancellation import run_with_cancellation

        return run_with_cancellation(
            lambda: self.model_client.complete(
                full_prompt,
                max_new_tokens=self.config.max_new_tokens or 4096,
            ),
            self.cancel_token,
        )

    def fit_user_message(
        self, user_message: str, *, system_override: str | None = None
    ) -> tuple[str, dict]:
        """用统一 TokenBudget 裁剪 user 段，保留 system/prefix 优先。"""
        from agent_runtime.context_manager import TOTAL_BUDGET, fit_prompt_to_budget

        if system_override is not None:
            system = system_override
        else:
            system = self._system_text_for_budget()
        _, fitted_user, meta = fit_prompt_to_budget(
            system,
            user_message,
            model=self.config.model,
            provider=self.config.provider,
            total_limit=self.config.prompt_budget or TOTAL_BUDGET,
        )
        meta["prompt_budget"] = self.config.prompt_budget
        return fitted_user, meta

    def _system_text_for_budget(self) -> str:
        """预算计算用的 system 文本（cache 段：system + tools，不含 skills/workspace）。"""
        from agent_runtime.prompt_prefix import cache_stable_text

        prefix = getattr(self, "_prefix", None)
        if prefix is not None:
            cache = cache_stable_text(
                getattr(prefix, "stable_system_text", "") or "",
                getattr(prefix, "stable_tools_text", "") or "",
            )
            if cache:
                return cache
            stable = getattr(prefix, "stable_text", "") or ""
            if stable:
                return stable
        return self._system_prompt or ""

    def build_dynamic_context(self, user_message: str) -> tuple[str, dict]:
        """动态上下文：workspace → memory → relevant → history。"""
        from agent_runtime.context_manager import ContextManager

        return ContextManager(self).build_dynamic_context(user_message)

    def build_for_native(self, user_message: str) -> tuple[str, str, dict]:
        """Native API 路径的 system + user 拆分。"""
        from agent_runtime.context_manager import ContextManager

        return ContextManager(self).build_for_native(user_message)

    def prompt(self, user_message: str) -> str:
        """组装完整 prompt 文本（经 ContextManager token 预算控制）。"""
        prompt_text, _ = self._build_prompt_with_meta(user_message)
        return prompt_text

    def _build_prompt_with_meta(self, user_message: str) -> tuple[str, dict]:
        """同 prompt()，但返回 (text, metadata)。"""
        from agent_runtime.context_manager import ContextManager

        cm = ContextManager(self)
        return cm.build(user_message)

    def record(self, item: dict):
        """向会话历史追加一条记录（canonical JSONL）。

        Args:
            item: 包含 role 和 content 的字典；user 消息会递增 turn_id。
        """
        from pathlib import Path

        from agent_runtime.turn_tracking import stamp_turn_id

        stamped = stamp_turn_id(self.session, item)
        # 双写：session 内存 + history.jsonl 追加
        self.session.setdefault("history", [])
        self.session.setdefault("_turn_counter", 0)
        self.session["history"].append(stamped)
        # JSONL 追加
        try:
            path = Path(self._cwd) / ".agent" / "history.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(stamped, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

    def read_history(self) -> list[dict]:
        """从 history.jsonl 读取历史投影（回退 session 内存）。"""
        from pathlib import Path

        path = Path(self._cwd) / ".agent" / "history.jsonl"
        if path.is_file():
            try:
                return [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            except Exception:
                pass
        return self.session.get("history", [])

    def execute_tool(self, name: str, args: dict):
        """执行指定工具：经 ToolGateway.dispatch（若配置）→ ToolExecutor 闸口。"""
        executor = self._get_tool_executor()

        def run():
            return executor.execute_gated(name, args)

        if self._tool_dispatch is not None:
            return self._tool_dispatch(self._agent_name, name, run)
        return run()

    def _get_tool_executor(self):
        from agent_runtime.tool_executor import ToolExecutor

        if not hasattr(self, "_tool_executor"):
            self._tool_executor = ToolExecutor(
                agent=self,
                approval_policy=self.config.approval,
                dry_run=self.dry_run,
                quota=self.quota,
            )
        return self._tool_executor

    # ---- 类方法 ----

    @classmethod
    def from_session(
        cls,
        model_client,
        workspace,
        session_store,
        session_id: str,
        **kwargs,
    ):
        """从持久化的 session 恢复 Agent 实例。

        Args:
            model_client: 模型客户端。
            workspace: WorkspaceContext。
            session_store: SessionStore 实例。
            session_id: 要恢复的 session id。
            **kwargs: 传递给 __init__ 的额外参数。

        Returns:
            恢复的 Agent 实例，如果 session 不存在则返回 None。
        """
        user_id = str(kwargs.pop("user_id", "") or "")
        workspace_id = str(kwargs.pop("workspace_id", "") or "")
        session = session_store.load(
            session_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        if session is None:
            return None

        config = kwargs.pop("config", None)
        if config is None:
            from agent_runtime.config import AgentConfig

            config = AgentConfig()

        cwd = kwargs.pop("cwd", None) or workspace.repo_root

        # 创建 Agent（先不注入 session）
        agent = cls(
            config=config,
            model_client=model_client,
            workspace=workspace,
            cwd=cwd,
            **kwargs,
        )

        # 恢复 session 数据
        agent.session = session
        # 恢复 memory 状态
        from agent_runtime.features.memory import default_memory_state

        agent.session.setdefault("memory", default_memory_state())
        agent.session.setdefault("checkpoints", [])

        return agent

    # ---- 静态方法 ----

    # ---- 内部方法 ----

    def _build_prefix(self, system_prompt: str = ""):
        """构建 System Prompt 前缀。system_prompt 非空时用它替代默认前缀。"""
        if system_prompt:
            if self._prefix_mode == "repair":
                if self._l1_prefix is not None:
                    from agent_runtime.prompt_prefix import compose_repair_prefix

                    return compose_repair_prefix(self._l1_prefix, system_prompt)
                from agent_runtime.prompt_prefix import build_repair_agent_prefix

                return build_repair_agent_prefix(
                    system_prompt,
                    self.workspace,
                    self.tools,
                    dry_run=self.dry_run,
                    approval=self.config.approval,
                    tool_names=self._tool_names,
                    repo_root=self._cwd,
                )
            from agent_runtime.prompt_prefix import build_custom_system_prefix

            return build_custom_system_prefix(system_prompt, self.workspace)
        return build_prompt_prefix(
            self.workspace,
            self.tools,
            dry_run=self.dry_run,
            approval=self.config.approval,
            tool_names=self._tool_names,
            repo_root=self._cwd,
        )

    def _new_session_id(self) -> str:
        """生成新的会话 ID。"""
        import uuid

        return str(uuid.uuid4())[:8]

    def _new_session(self) -> dict:
        """创建新会话（含记忆状态）。"""
        import hashlib

        from agent_runtime.features.memory import default_memory_state

        session_id = self._new_session_id()
        workspace_id = hashlib.sha256(str(Path(self._cwd).resolve()).encode()).hexdigest()[:16]
        return {
            "id": session_id,
            "schema_version": "2.0",
            "revision": 0,
            "session_scope": {
                "session_id": session_id,
                "user_id": "",
                "workspace_id": workspace_id,
            },
            "session_identity": {
                "session_id": session_id,
                "workspace_id": workspace_id,
            },
            "history": [],
            "memory": default_memory_state(),
            "checkpoints": [],
            "action_ledger": [],
            "side_effects": [],
            "_turn_counter": 0,
        }

    def update_memory_after_tool(self, name: str, args: dict, result_text: str):
        """工具执行后更新记忆（Working + Episodic）。

        由 AgentLoop 在每个工具执行成功后调用。
        """
        from agent_runtime.features.memory import (
            append_note,
            invalidate_file_summary,
            record_read_evidence,
            record_search_evidence,
            remember_file,
            set_file_summary,
        )
        from agent_runtime.repair_context import get_repair_context, update_repair_context

        mem = self.session["memory"]
        recalled_ids = list(mem.get("recalled_memory_ids") or [])
        if recalled_ids:
            from agent_runtime.features.memory.governance import MemoryGovernanceService

            identity = mem.get("memory_identity") or {}
            governance = MemoryGovernanceService(
                mem,
                repo_root=str(self._cwd or ""),
                user_id=str(identity.get("user_id", "") or ""),
                task_id=str(identity.get("task_id", "") or ""),
            )
            stage = (
                "patch"
                if name in {"write_file", "patch_file", "apply_patch"}
                else "tool"
            )
            for memory_id in recalled_ids:
                governance.record_usage_stage(
                    memory_id,
                    usage="applied",
                    stage=stage,
                    task_id=str(identity.get("task_id", "") or ""),
                )
        context = get_repair_context(mem)
        path = args.get("path", "")

        if name == "read_file" and path:
            remember_file(mem, path)
            # 从结果中取前 180 字符作为摘要
            summary = result_text[:180]
            set_file_summary(mem, path, summary)
            record_read_evidence(
                mem,
                path=path,
                start=int(args.get("start", 1) or 1),
                end=int(args.get("end", 200) or 200),
                result_text=result_text,
            )
            context["candidate_files"] = list(
                dict.fromkeys(context["candidate_files"] + [path])
            )[-12:]

        elif name in ("write_file", "patch_file") and path:
            remember_file(mem, path)
            invalidate_file_summary(mem, path)
            context["changed_files"] = list(dict.fromkeys(context["changed_files"] + [path]))[-12:]
            context["next_action"] = "run targeted verification"

        elif name == "run_shell":
            command = args.get("command", "")
            passed = not ("Error" in result_text or "exit_code: 1" in result_text)
            if not passed:
                append_note(
                    mem,
                    f"Shell 命令失败: {command[:100]} — {result_text[:100]}",
                    tags=["shell", "error"],
                    source=command[:80],
                    kind="error",
                )
            else:
                append_note(
                    mem,
                    f"Shell 命令成功: {command[:100]}",
                    tags=["shell"],
                    source=command[:80],
                    kind="observation",
                )
            context["verification"] = {
                "last_command": command[:160],
                "passed": passed,
                "result_excerpt": result_text[-500:],
            }

        elif name == "search":
            pattern = args.get("pattern", "")
            if pattern:
                append_note(
                    mem,
                    f"搜索 '{pattern}': {result_text[:150]}",
                    tags=["search"],
                    source=pattern,
                    kind="observation",
                )
                record_search_evidence(
                    mem,
                    pattern=pattern,
                    path=str(args.get("path", "") or "."),
                    result_text=result_text,
                )

        update_repair_context(
            mem,
            changed_files=context["changed_files"],
            candidate_files=context["candidate_files"],
            verification=context.get("verification", {}),
            next_action=context.get("next_action", ""),
        )

        # Candidate 抽取 hook（after_tool）
        self._extract_memory_candidates_from_tool(name, args, result_text)

    def _extract_memory_candidates_from_tool(self, name: str, args: dict, result_text: str) -> None:
        """从工具结果中规则抽取 Candidate 并写入 session 暂存区。"""
        try:
            from agent_runtime.features.memory.candidate import candidates_from_tool

            candidates = candidates_from_tool(name, args, result_text)
            if candidates:
                self.session.setdefault("_memory_candidates", []).extend(candidates)
        except Exception:
            pass
