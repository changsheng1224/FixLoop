"""修复 Orchestrator 工厂（repair / eval 共用）。"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from agent_runtime.bootstrap import create_model_client, load_dotenv
from agent_runtime.prompt_prefix import build_repair_l1_prefix
from agent_runtime.repair_budget import RepairBudgetContext
from agent_runtime.tool_context import ToolContext
from agent_runtime.warm_context import create_warm_context
from agent_runtime.workspace import WorkspaceContext
from src.agents.localizer import create_localizer
from src.agents.patcher import create_patcher
from src.agents.retriever import create_retriever
from src.agents.verifier import create_verifier
from src.orchestrator import Orchestrator
from src.tools.composite import build_repair_canonical_tools

# 动态 Agent 裁剪：根据 issue_type 决定哪些 Agent 参与修复
# simple = 仅 Localizer + Patcher（跳过 Retriever）
# full    = 全部四个 Agent
_SIMPLE_ISSUE_TYPES = frozenset({"import_error", "syntax_error"})

@dataclass(frozen=True)
class AgentProfile:
    """问题类型对应的 Agent 参与配置。"""
    with_retriever: bool = True

    @classmethod
    def for_issue_type(cls, issue_type: str) -> "AgentProfile":
        if issue_type in _SIMPLE_ISSUE_TYPES:
            return cls(with_retriever=False)
        return cls(with_retriever=True)

O = TypeVar("O", bound=Orchestrator)

__all__ = [
    "AgentProfile",
    "create_model_client",
    "load_dotenv",
    "make_orchestrator_factory",
    "try_create_verifier",
    "wire_orchestrator",
]


def try_create_verifier(client, ws, repo: str, **agent_kw):
    """Docker 探针就绪时创建 Verifier Agent，否则返回 None。"""
    import sys

    from src.harness.sandbox_health import probe_sandbox_health

    report = probe_sandbox_health(run_smoke=False)
    if not report.ready:
        detail = "; ".join(report.errors) or "sandbox not ready"
        print(f"[repair_factory] sandbox health probe failed: {detail}", file=sys.stderr)
        return None
    try:
        return create_verifier(client, ws, cwd=repo, **agent_kw)
    except Exception:
        return None


def wire_orchestrator(
    client,
    repo_path: str,
    *,
    orch_class: type[O] = Orchestrator,
    with_retriever: bool | None = None,
    skip_verify: bool = False,
    dry_run: bool = False,
    agent_profile: AgentProfile | None = None,
) -> O:
    """装配 Localizer / Retriever / Patcher / 可选 Verifier（Agent 池化预热）。

    预热策略：
    1. WarmContext 预加载分词器到模块级缓存（首次 ~0.5–1.5s，后续命中缓存）
    2. ThreadPoolExecutor 并行创建 localizer + retriever（wall-clock 优化）
    3. warm_context 注入 Agent，供 ContextManager 复用
    """
    ws = WorkspaceContext.build(repo_path)
    repo = str(Path(repo_path).resolve())
    ctx = ToolContext(root=repo)
    tools = build_repair_canonical_tools(ctx)
    l1 = build_repair_l1_prefix(
        ws,
        tools,
        dry_run=dry_run,
        approval="auto",
        repo_root=repo,
    )

    # 动态 Agent 裁剪
    if with_retriever is None and agent_profile is not None:
        with_retriever = agent_profile.with_retriever
    elif with_retriever is None:
        with_retriever = True

    # 预热 tokenizer + 创建共享预算上下文
    wc = create_warm_context(model="deepseek-v4-pro", provider="deepseek")
    budget_ctx = RepairBudgetContext.create(model="deepseek-v4-pro", provider="deepseek")

    base_kw: dict = {"l1_prefix": l1, "dry_run": dry_run, "warm_context": wc}

    def _agent_kw(role: str) -> dict:
        """构建角色专属 kwargs（含子预算）。配额由 Agent._role_quota 按角色分配。"""
        kw = dict(base_kw)
        kw["budget"] = budget_ctx.sub_budget(role)
        return kw

    # 并行预建 localizer + retriever（ThreadPoolExecutor）
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_loc = pool.submit(create_localizer, client, ws, repo, **_agent_kw("localizer"))
        fut_ret = (
            pool.submit(create_retriever, client, ws, repo, **_agent_kw("retriever"))
            if with_retriever
            else None
        )
        localizer = fut_loc.result()
        retriever = fut_ret.result() if fut_ret else None

    patcher = create_patcher(client, ws, cwd=repo, **_agent_kw("patcher"))
    orch = orch_class(
        localizer,
        retriever,
        patcher,
        use_pytest_verify=not skip_verify,
        l1_prompt_cache_key=l1.hash,
    )
    orch._budget_ctx = budget_ctx
    if not skip_verify:
        verifier = try_create_verifier(client, ws, repo, **_agent_kw("verifier"))
        if verifier:
            orch.verifier = verifier
    return orch


def make_orchestrator_factory(
    *,
    skip_verify: bool = False,
    dry_run: bool = False,
    model_client=None,
) -> Callable[[str], Orchestrator]:
    """返回 `(repo_path) -> Orchestrator` 工厂。"""

    client = create_model_client(model_client)

    def factory(repo_path: str) -> Orchestrator:
        return wire_orchestrator(
            client,
            repo_path,
            skip_verify=skip_verify,
            dry_run=dry_run,
        )

    return factory
