"""修复 Orchestrator 工厂（repair / eval 共用）。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from agent_runtime.bootstrap import create_model_client, load_dotenv
from agent_runtime.prompt_prefix import build_repair_l1_prefix
from agent_runtime.repair_budget import RepairBudgetContext
from agent_runtime.tool_context import ToolContext
from agent_runtime.warm_context import create_warm_context
from agent_runtime.workspace import WorkspaceContext
from src.agents.patcher import create_patcher
from src.agents.verifier import create_verifier
from src.middleware import build_repair_gateway
from src.orchestrator import Orchestrator
from src.tools.composite import build_repair_canonical_tools

O = TypeVar("O", bound=Orchestrator)

__all__ = [
    "RequiredVerifierError",
    "create_model_client",
    "load_dotenv",
    "make_orchestrator_factory",
    "try_create_verifier",
    "wire_orchestrator",
]


class RequiredVerifierError(RuntimeError):
    """The requested Verifier sandbox could not be created safely."""


def try_create_verifier(client, ws, repo: str, *, required: bool = False, **agent_kw):
    """Docker 探针就绪时创建 Verifier Agent，否则返回 None。"""
    import sys

    from src.harness.sandbox_health import probe_sandbox_health

    report = probe_sandbox_health(run_smoke=False)
    if not report.ready:
        detail = "; ".join(report.errors) or "sandbox not ready"
        print(f"[repair_factory] sandbox health probe failed: {detail}", file=sys.stderr)
        if required:
            raise RequiredVerifierError(f"required verifier sandbox unavailable: {detail}")
        return None
    try:
        return create_verifier(client, ws, cwd=repo, **agent_kw)
    except Exception as exc:
        if required:
            raise RequiredVerifierError(f"required verifier creation failed: {exc}") from exc
        return None


def wire_orchestrator(
    client,
    repo_path: str,
    *,
    orch_class: type[O] = Orchestrator,
    skip_verify: bool = False,
    dry_run: bool = False,
    execution_tier: str = "auto",
    require_sandbox: bool = False,
    execution_backend: str = "legacy",
    code_exploration_mode: str = "text",
    code_exploration_server_argv: tuple[str, ...] | None = None,
) -> O:
    """装配唯一的 Patcher-primary runtime 与可选 Verifier。"""
    if execution_backend not in {"legacy", "wsl_bwrap"}:
        raise ValueError("unknown execution backend")
    if execution_backend == "wsl_bwrap":
        if execution_tier != "auto" or require_sandbox:
            raise RequiredVerifierError("wsl_bwrap conflicts with legacy tier/require_sandbox")
        if code_exploration_mode != "text" or code_exploration_server_argv:
            raise RequiredVerifierError("wsl_bwrap does not support LSP")
        raise RequiredVerifierError(
            "wsl_bwrap repair is gated until P3 external state_root isolation"
        )
    ws = WorkspaceContext.build(repo_path)
    repo = str(Path(repo_path).resolve())
    ctx = ToolContext(
        root=repo, exploration_mode=code_exploration_mode, lsp_argv=code_exploration_server_argv
    )
    tools = build_repair_canonical_tools(ctx)
    l1 = build_repair_l1_prefix(
        ws,
        tools,
        dry_run=dry_run,
        approval="auto",
        repo_root=repo,
    )

    # 预热 tokenizer + 创建共享预算上下文
    wc = create_warm_context(model="deepseek-v4-pro", provider="deepseek")
    budget_ctx = RepairBudgetContext.create(model="deepseek-v4-pro", provider="deepseek")
    gateway = build_repair_gateway(repo)

    base_kw: dict = {
        "l1_prefix": l1,
        "dry_run": dry_run,
        "warm_context": wc,
        "gateway": gateway,
        "code_exploration_mode": code_exploration_mode,
        "code_exploration_server_argv": code_exploration_server_argv,
    }

    def _agent_kw(role: str) -> dict:
        """构建角色专属 kwargs（含子预算）。配额由 Agent._role_quota 按角色分配。"""
        kw = dict(base_kw)
        kw["budget"] = budget_ctx.sub_budget(role)
        return kw

    patcher = create_patcher(client, ws, cwd=repo, **_agent_kw("patcher"))
    normalized_tier = (
        execution_tier if execution_tier in {"auto", "container", "host", "static"} else "auto"
    )
    use_pytest_verify = (not skip_verify) and normalized_tier in {"auto", "host"}
    allow_static_verify_fallback = (not skip_verify) and normalized_tier == "static"
    should_try_verifier = (not skip_verify) and normalized_tier in {"auto", "container"}

    orch = orch_class(
        patcher,
        use_pytest_verify=use_pytest_verify,
        require_sandbox=require_sandbox or normalized_tier == "container",
        allow_static_verify_fallback=allow_static_verify_fallback,
        l1_prompt_cache_key=l1.hash,
    )
    orch._budget_ctx = budget_ctx
    if should_try_verifier:
        verifier = try_create_verifier(
            client,
            ws,
            repo,
            required=require_sandbox or normalized_tier == "container",
            **_agent_kw("verifier"),
        )
        if verifier:
            orch.verifier = verifier
            orch._repair_gateways = orch._collect_repair_gateways(patcher, verifier)
    return orch


def make_orchestrator_factory(
    *,
    skip_verify: bool = False,
    dry_run: bool = False,
    execution_tier: str = "auto",
    require_sandbox: bool = False,
    execution_backend: str = "legacy",
    code_exploration_mode: str = "text",
    code_exploration_server_argv: tuple[str, ...] | None = None,
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
            execution_tier=execution_tier,
            require_sandbox=require_sandbox,
            execution_backend=execution_backend,
            code_exploration_mode=code_exploration_mode,
            code_exploration_server_argv=code_exploration_server_argv,
        )

    return factory
