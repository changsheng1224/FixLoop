"""修复 Orchestrator 工厂（repair / eval 共用）。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from agent_runtime.bootstrap import create_model_client, load_dotenv
from agent_runtime.linux_sandbox import GuardedWindowsWorkspaceBackend, SandboxTaskSpec
from agent_runtime.linux_sandbox.environment import SandboxEnvironmentManager
from agent_runtime.prompt_prefix import build_repair_l1_prefix
from agent_runtime.repair_budget import RepairBudgetContext
from agent_runtime.state_root import managed_state_base
from agent_runtime.tool_context import ToolContext
from agent_runtime.warm_context import create_warm_context
from agent_runtime.workspace import WorkspaceContext
from src.agents.factory import load_repair_agent_config
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

SANDBOX_POLICY_VERSION = "wsl-bwrap-v1"


def _sandbox_policy_digest(environment) -> str:
    """Freeze the controller profile into the task binding identity."""
    profile = {
        "backend": "wsl_bwrap",
        "policy_version": SANDBOX_POLICY_VERSION,
        "environment": {
            "distribution": environment.distribution,
            "distribution_version": environment.distribution_version,
            "runtime_version": environment.runtime_version,
            "runtime_digest": environment.runtime_digest,
        },
        "workspace_profile": "windows_guarded",
        "limits": {
            "timeout_s": 120,
            "tmpfs_bytes": 64 * 1024 * 1024,
            "output_bytes": 1024 * 1024,
        },
    }
    encoded = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


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
    sandbox_environment_manager=None,
    sandbox_package_proxy: str = "",
    workspace_context=None,
    state_root: str = "",
    runtime_overrides: dict | None = None,
) -> O:
    """装配唯一的 Patcher-primary runtime 与可选 Verifier。"""
    if execution_backend not in {"legacy", "wsl_bwrap"}:
        raise ValueError("unknown execution backend")
    sandbox_context = None
    sandbox_task_spec = None
    sandbox_backend_factory = None
    if execution_backend == "wsl_bwrap":
        if execution_tier != "auto" or require_sandbox:
            raise RequiredVerifierError("wsl_bwrap conflicts with legacy tier/require_sandbox")
        if code_exploration_mode != "text" or code_exploration_server_argv:
            raise RequiredVerifierError("wsl_bwrap does not support LSP")
        try:
            manager = sandbox_environment_manager or SandboxEnvironmentManager(repo_path)
            prepared = manager.ensure(package_proxy=sandbox_package_proxy)
            state_base = managed_state_base(repo_path)
            sandbox_task_spec = SandboxTaskSpec(
                prepared.identity,
                SANDBOX_POLICY_VERSION,
                _sandbox_policy_digest(prepared.identity),
                str(state_base),
            )
            sandbox_context = ToolContext(
                root=str(Path(repo_path).resolve()),
                exploration_mode="text",
                sandbox_mode=True,
            )
            sandbox_backend_factory = GuardedWindowsWorkspaceBackend
        except Exception as exc:
            raise RequiredVerifierError(f"wsl_bwrap sandbox unavailable: {exc}") from exc
    repo = str(Path(repo_path).resolve())
    # The final task binding loads metadata through the guarded workspace worker.
    ws = (
        WorkspaceContext(cwd=repo, repo_root=repo, doc_contents={}, dirty_file_hashes={})
        if sandbox_context is not None
        else workspace_context or WorkspaceContext.build(repo)
    )
    ctx = sandbox_context or ToolContext(
        root=repo, exploration_mode=code_exploration_mode, lsp_argv=code_exploration_server_argv
    )
    if state_root:
        ctx.state_root = state_root
    tools = build_repair_canonical_tools(ctx)
    policies = {
        role: load_repair_agent_config(
            role,
            repo,
            code_exploration={
                "mode": code_exploration_mode,
                "server_argv": code_exploration_server_argv,
            },
        )
        for role in ("patcher", "verifier")
    }
    policy = policies["patcher"]
    if runtime_overrides:
        policies = {
            role: config.model_copy(update=runtime_overrides) for role, config in policies.items()
        }
        policy = policies["patcher"]
    client = create_model_client(
        client,
        model=policy.model,
        provider=policy.provider,
        temperature=policy.temperature,
    )
    l1 = build_repair_l1_prefix(
        ws,
        tools,
        dry_run=dry_run,
        approval=policy.approval,
        repo_root=repo,
    )

    # 预热 tokenizer + 创建共享预算上下文
    wc = create_warm_context(model=policy.model, provider=policy.provider)
    budget_ctx = RepairBudgetContext.create(model=policy.model, provider=policy.provider)
    gateway = build_repair_gateway(repo, sandbox_mode=execution_backend == "wsl_bwrap")

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
        kw["runtime_policy"] = policies[role]
        return kw

    if sandbox_context is not None:
        base_kw = dict(_agent_kw("patcher"))
        base_kw["sandbox_context"] = sandbox_context
        patcher = create_patcher(client, ws, cwd=repo, **base_kw)
    else:
        patcher_kw = _agent_kw("patcher")
        if state_root:
            patcher_kw["sandbox_context"] = ctx
        patcher = create_patcher(client, ws, cwd=repo, **patcher_kw)
    normalized_tier = (
        execution_tier if execution_tier in {"auto", "container", "host", "static"} else "auto"
    )
    use_pytest_verify = (not skip_verify) and normalized_tier in {"auto", "host"}
    allow_static_verify_fallback = (not skip_verify) and normalized_tier == "static"
    # The WSL profile verifies through the task-bound BwrapVerifyStrategy. A
    # host/Docker verifier would violate the single-backend task contract.
    should_try_verifier = (
        sandbox_context is None and (not skip_verify) and normalized_tier in {"auto", "container"}
    )

    orch = orch_class(
        patcher,
        use_pytest_verify=use_pytest_verify if sandbox_context is None else False,
        require_sandbox=(require_sandbox or normalized_tier == "container")
        if sandbox_context is None
        else False,
        allow_static_verify_fallback=allow_static_verify_fallback,
        l1_prompt_cache_key=l1.hash,
        sandbox_context=sandbox_context,
        sandbox_task_spec=sandbox_task_spec,
        sandbox_backend_factory=sandbox_backend_factory,
    )
    orch._budget_ctx = budget_ctx
    if should_try_verifier:
        verifier_kw = _agent_kw("verifier")
        if state_root:
            verifier_kw["sandbox_context"] = ToolContext(root=repo, state_root=state_root)
        verifier = try_create_verifier(
            client,
            ws,
            repo,
            required=require_sandbox or normalized_tier == "container",
            **verifier_kw,
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
    sandbox_environment_manager=None,
    sandbox_package_proxy: str = "",
    model_client=None,
) -> Callable[[str], Orchestrator]:
    """返回 `(repo_path) -> Orchestrator` 工厂。"""

    def factory(repo_path: str) -> Orchestrator:
        return wire_orchestrator(
            model_client,
            repo_path,
            skip_verify=skip_verify,
            dry_run=dry_run,
            execution_tier=execution_tier,
            require_sandbox=require_sandbox,
            execution_backend=execution_backend,
            code_exploration_mode=code_exploration_mode,
            code_exploration_server_argv=code_exploration_server_argv,
            sandbox_environment_manager=sandbox_environment_manager,
            sandbox_package_proxy=sandbox_package_proxy,
        )

    return factory
