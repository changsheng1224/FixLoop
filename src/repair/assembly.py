"""修复 ExecutionSession 工厂（repair / eval 共用）。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from agent_runtime.bootstrap import create_model_client, load_dotenv
from agent_runtime.linux_sandbox import GuardedWindowsWorkspaceBackend, SandboxTaskSpec
from agent_runtime.linux_sandbox.environment import SandboxEnvironmentManager
from agent_runtime.state_root import managed_storage_root
from agent_runtime.tool_context import ToolContext
from agent_runtime.warm_context import create_warm_context
from agent_runtime.workspace import WorkspaceContext
from src.agents.factory import load_repair_agent_config
from src.agents.patcher import create_patcher
from src.middleware import build_repair_gateway
from src.prompts.prefix import build_repair_l1_prefix
from src.repair.budget import RepairBudgetContext
from src.repair.service import RepairService
from src.repair.session import ExecutionSession
from src.tools.composite import build_repair_canonical_tools

__all__ = [
    "VerificationEnvironmentError",
    "create_model_client",
    "load_dotenv",
    "repair_service_factory",
    "probe_verification_backend",
    "create_repair_service",
]

SANDBOX_POLICY_VERSION = "wsl-bwrap-v1"


def _sandbox_policy_digest(environment, workspace_policy: str) -> str:
    """Freeze the controller profile into the task binding identity."""
    profile = {
        "backend": "wsl_bwrap",
        "workspace_policy": workspace_policy,
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


class VerificationEnvironmentError(RuntimeError):
    """The requested Verifier sandbox could not be created safely."""


def probe_verification_backend(*, required: bool = False):
    """Probe a verification backend; no model, prompt, session or budget is built."""
    import sys

    from src.harness.sandbox_health import probe_sandbox_health

    report = probe_sandbox_health(run_smoke=False)
    if not report.ready:
        detail = "; ".join(report.errors) or "sandbox not ready"
        print(f"[repair_factory] sandbox health probe failed: {detail}", file=sys.stderr)
        if required:
            raise VerificationEnvironmentError(
                f"required verification sandbox unavailable: {detail}"
            )
        return None
    from src.repair.verification.execution_policy import VerificationBackend

    return VerificationBackend()


def create_repair_service(
    client,
    repo_path: str,
    *,
    progress=None,
    skip_verify: bool = False,
    dry_run: bool = False,
    execution_tier: str = "auto",
    require_sandbox: bool = False,
    execution_backend: str = "legacy",
    code_exploration_mode: str = "text",
    code_exploration_server_argv: tuple[str, ...] | None = None,
    sandbox_environment_manager=None,
    sandbox_package_proxy: str = "",
    workspace_policy: str = "worktree",
    workspace_context=None,
    state_root: str = "",
    project_root: str = "",
    embedding_state_base: str = "",
    runtime_overrides: dict | None = None,
) -> RepairService:
    """装配正式修复服务及真实命令验证后端。"""
    if execution_backend not in {"legacy", "wsl_bwrap"}:
        raise ValueError("unknown execution backend")
    if execution_tier not in {"auto", "container", "host", "static"}:
        raise ValueError("unknown execution tier")
    from src.repair.config import load_repair_config
    from src.repair.critic import resolve_critic_mode
    from src.repair.verification.execution_policy import resolve_execution_policy

    resolve_critic_mode()
    repair_config = load_repair_config(workspace_root=repo_path)
    if skip_verify and (
        require_sandbox
        or execution_tier == "container"
        or repair_config.sandbox_policy == "required"
    ):
        raise ValueError("skip_verify conflicts with required verification")
    resolve_execution_policy(
        sandbox_policy=repair_config.sandbox_policy,
        execution_tier=execution_tier,
        require_sandbox=require_sandbox,
        sandbox=execution_backend == "wsl_bwrap",
        host=execution_tier in {"auto", "host"},
        static=execution_tier == "static",
    )
    if workspace_policy not in {"worktree", "provided"}:
        raise ValueError("unknown workspace policy")
    if (
        workspace_policy == "worktree"
        and project_root
        and (Path(project_root).resolve() != Path(repo_path).resolve())
    ):
        raise ValueError("worktree source must be the registered project root")
    sandbox_context = None
    sandbox_task_spec = None
    sandbox_backend_factory = None
    if execution_backend == "wsl_bwrap":
        if execution_tier != "auto" or require_sandbox:
            raise VerificationEnvironmentError(
                "wsl_bwrap conflicts with legacy tier/require_sandbox"
            )
        if code_exploration_mode != "text" or code_exploration_server_argv:
            raise VerificationEnvironmentError("wsl_bwrap does not support LSP")
        try:
            manager = sandbox_environment_manager or SandboxEnvironmentManager(repo_path)
            prepared = manager.ensure(package_proxy=sandbox_package_proxy)
            state_base = managed_storage_root(project_root or repo_path, state_root or None)
            sandbox_task_spec = SandboxTaskSpec(
                prepared.identity,
                SANDBOX_POLICY_VERSION,
                _sandbox_policy_digest(prepared.identity, workspace_policy),
                str(state_base),
                project_root,
            )
            sandbox_context = ToolContext(
                root=str(Path(repo_path).resolve()),
                exploration_mode="text",
                sandbox_mode=True,
            )
            sandbox_backend_factory = GuardedWindowsWorkspaceBackend
        except Exception as exc:
            raise VerificationEnvironmentError(f"wsl_bwrap sandbox unavailable: {exc}") from exc
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
    ctx.project_root = str(Path(project_root or repo).resolve())
    ctx.embedding_state_base = str(
        managed_storage_root(
            ctx.project_root,
            embedding_state_base or state_root or None,
            create=False,
        )
    )
    if state_root:
        ctx.state_root = state_root
    role_overrides = {
        "code_exploration": {
            "mode": code_exploration_mode,
            "server_argv": code_exploration_server_argv,
        },
        **(runtime_overrides or {}),
    }
    policies = {
        role: load_repair_agent_config(role, repo, **role_overrides) for role in ("patcher",)
    }
    policy = policies["patcher"]
    ctx.web_config = policy.web
    tools = build_repair_canonical_tools(ctx)
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
    budget_ctx = RepairBudgetContext.create(
        model=policy.model, provider=policy.provider, allocations={"patcher": policy.prompt_budget}
    )
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
        """传递已解析的角色策略与上下文预算，不增加额外配额。"""
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
        patcher_kw["sandbox_context"] = ctx
        patcher = create_patcher(client, ws, cwd=repo, **patcher_kw)
    normalized_tier = execution_tier
    use_pytest_verify = (not skip_verify) and normalized_tier in {"auto", "host"}
    allow_static_verify_fallback = (not skip_verify) and normalized_tier == "static"
    # The WSL profile verifies through the task-bound BwrapVerifyStrategy. A
    # host/Docker verification_backend would violate the single-backend task contract.
    should_probe_verification_backend = (
        sandbox_context is None
        and (not skip_verify)
        and normalized_tier in {"auto", "container"}
        and repair_config.sandbox_policy != "disabled"
    )
    verification_backend = (
        probe_verification_backend(
            required=require_sandbox
            or normalized_tier == "container"
            or repair_config.sandbox_policy == "required",
        )
        if should_probe_verification_backend
        else None
    )

    session = ExecutionSession(
        patcher,
        budget_context=budget_ctx,
        progress=progress,
        verification_backend=verification_backend,
        repair_config=repair_config,
        use_pytest_verify=use_pytest_verify if sandbox_context is None else False,
        require_sandbox=(require_sandbox or normalized_tier == "container")
        if sandbox_context is None
        else False,
        allow_static_verify_fallback=allow_static_verify_fallback,
        l1_prompt_cache_key=l1.hash,
        sandbox_context=sandbox_context,
        sandbox_task_spec=sandbox_task_spec,
        sandbox_backend_factory=sandbox_backend_factory,
        workspace_policy=workspace_policy,
        execution_tier=normalized_tier,
        execution_profile=(
            f"{execution_backend}:{execution_tier}:{bool(require_sandbox)}:"
            f"skip_verify={bool(skip_verify)}:dry_run={bool(dry_run)}"
        ),
        workspace_state_base=state_root,
        project_root=project_root,
    )
    patcher.workspace_pinned = True
    return RepairService(session)


def repair_service_factory(
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
    workspace_policy: str = "worktree",
    state_root: str = "",
    project_root: str = "",
    embedding_state_base: str = "",
    model_client=None,
) -> Callable[[str], RepairService]:
    """返回 `(repo_path) -> RepairService` 装配函数。"""

    def factory(repo_path: str) -> RepairService:
        return create_repair_service(
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
            workspace_policy=workspace_policy,
            state_root=state_root,
            project_root=project_root,
            embedding_state_base=embedding_state_base,
        )

    return factory
