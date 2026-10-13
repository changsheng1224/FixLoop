from __future__ import annotations

import threading
from pathlib import Path

from agent_runtime.logging_setup import get_logger
from src.repair.run_context import RepairRunContext

log = get_logger("orchestrator")


class ExecutionSession:
    def __init__(
        self,
        patcher,
        verification_backend=None,
        *,
        use_pytest_verify: bool = False,
        require_sandbox: bool = False,
        sandbox_policy: str = "",
        allow_static_verify_fallback: bool = False,
        l1_prompt_cache_key: str = "",
        sandbox_context=None,
        sandbox_task_spec=None,
        sandbox_backend_factory=None,
        workspace_policy: str = "provided",
        execution_profile: str = "",
        execution_tier: str = "",
        workspace_state_base: str = "",
        project_root: str = "",
        repair_config=None,
        budget_context=None,
        progress=None,
    ):
        self.patcher = patcher
        self.verification_backend = verification_backend
        self.use_pytest_verify = use_pytest_verify
        self.execution_tier = execution_tier
        self.verify_service = None
        from src.repair.config import load_repair_config

        self.repair_config = repair_config or load_repair_config(
            workspace_root=getattr(patcher, "cwd", None),
            cli_overrides={"sandbox_policy": "required" if require_sandbox else sandbox_policy}
            if require_sandbox or sandbox_policy
            else None,
        )
        configured_policy = self.repair_config.sandbox_policy
        self.sandbox_policy = configured_policy
        self.require_sandbox = bool(require_sandbox or configured_policy == "required")
        self.allow_static_verify_fallback = allow_static_verify_fallback
        from src.repair.critic import resolve_critic_mode
        from src.repair.verification.execution_policy import resolve_execution_policy

        self.critic_mode = resolve_critic_mode()
        self.execution_policy = resolve_execution_policy(
            sandbox_policy=configured_policy,
            execution_tier=execution_tier,
            require_sandbox=require_sandbox,
            sandbox=sandbox_context is not None,
            verification_backend=verification_backend,
            host=use_pytest_verify,
            static=allow_static_verify_fallback,
        )
        if sandbox_context is not None and (
            verification_backend is not None
            or use_pytest_verify
            or allow_static_verify_fallback
            or require_sandbox
        ):
            raise ValueError("wsl_bwrap cannot mix with Docker/host/static verification")
        if (
            sandbox_context is not None
            and getattr(patcher, "tool_context", None) is not sandbox_context
        ):
            raise ValueError("wsl_bwrap patcher and verification_backend must share ToolContext")
        self.sandbox_context = sandbox_context
        if workspace_policy not in {"worktree", "provided"}:
            raise ValueError("unknown workspace policy")
        self.workspace_policy = workspace_policy
        self.execution_profile = execution_profile or (
            f"{('wsl_bwrap' if sandbox_context is not None else 'legacy')}:"
            f"pytest={bool(use_pytest_verify)}:static={bool(allow_static_verify_fallback)}"
        )
        self.workspace_state_base = workspace_state_base
        self.project_root = project_root or getattr(sandbox_task_spec, "project_root", "")
        self.sandbox_task_spec = sandbox_task_spec
        self.sandbox_backend_factory = sandbox_backend_factory
        self.sandbox_backend = None
        self.sandbox_worktree_handle = None
        self.sandbox_backend_close_attempted = False
        if sandbox_task_spec is not None and (
            sandbox_context is None or sandbox_backend_factory is None
        ):
            raise ValueError("sandbox task requires context and a binding-aware backend factory")
        if sandbox_context is not None and sandbox_task_spec is None:
            sandbox_context.sandbox_identity = {
                "backend": "wsl_bwrap",
                "policy_digest": "",
                "distribution_id": "wsl2",
                "mapping_id": str(getattr(sandbox_context, "root", "") or ""),
                "receipt_id": "",
                "receipt_checksum": "",
            }
        self.l1_prompt_cache_key = l1_prompt_cache_key or self.resolve_l1_prompt_cache_key()
        self.repair_ctx: RepairRunContext | None = None
        self.collaboration_runtime = None
        self.plan_binding = None
        self.log_run_id_token = None
        self.repo_root = str(Path.cwd())
        for agent in self.active_agents:
            workspace = getattr(agent, "workspace", None)
            candidate = (
                getattr(agent, "cwd", "")
                or getattr(workspace, "cwd", "")
                or getattr(workspace, "repo_root", "")
            )
            if candidate:
                self.repo_root = candidate
                break
        self.sandbox_repository_root = self.repo_root
        self.repair_gateways = self.collect_repair_gateways(*self.active_agents)
        self.budget_ctx = budget_context
        self.progress = progress
        self.exploration_client_factory = None
        self.exploration_limits = self.repair_config.exploration
        self.light_client = None
        self.model_budget = None
        self.model_deadline = None
        self.edit_lock = None
        self.entry_coordinator = None
        self.plan_fault = None
        self.request = None
        self.current_state = None
        self.run_lock = threading.Lock()
        from src.repair.agent_calls import AgentCalls
        from src.repair.coordination import RepairCoordination
        from src.repair.coordinator import RepairCoordinator
        from src.repair.events_service import RepairEvents
        from src.repair.evidence_service import EvidenceService
        from src.repair.execution.lifecycle import ExecutionLifecycle
        from src.repair.execution.transaction import PatchTransaction
        from src.repair.exploration_service import ExplorationService
        from src.repair.patcher_service import PatcherService
        from src.repair.verification.controller import VerificationController

        self.lifecycle = ExecutionLifecycle(self)
        self.transaction = PatchTransaction(self)
        self.verification = VerificationController(self)
        self.asks = AgentCalls(self)
        self.evidence = EvidenceService(self)
        self.coordination = RepairCoordination(self)
        self.exploration = ExplorationService(self)
        self.patcher_execution = PatcherService(self)
        self.events = RepairEvents(self)
        self.coordinator = RepairCoordinator(self)

    @property
    def active_agents(self) -> tuple:
        """Return configured repair agents without repeating null filtering."""
        return (self.patcher,) if self.patcher is not None else ()

    @staticmethod
    def collect_repair_gateways(*agents) -> tuple:
        gateways = []
        seen: set[int] = set()
        for agent in agents:
            gw = getattr(agent, "tool_gateway", None)
            if gw is None:
                continue
            marker = id(gw)
            if marker in seen:
                continue
            seen.add(marker)
            gateways.append(gw)
        return tuple(gateways)

    @staticmethod
    def resolve_l1_prompt_cache_key_from_agents(*agents) -> str:
        hashes = []
        for agent in agents:
            if agent is None:
                continue
            prefix = getattr(agent, "prefix", None)
            if prefix is not None and getattr(prefix, "hash", ""):
                hashes.append(prefix.hash)
        if hashes and len(set(hashes)) == 1:
            return hashes[0]
        return ""

    def resolve_l1_prompt_cache_key(self) -> str:
        return self.resolve_l1_prompt_cache_key_from_agents(*self.active_agents)
