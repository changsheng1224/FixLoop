"""修复 Orchestrator 工厂（repair / eval 共用）。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from agent_runtime.bootstrap import create_model_client, load_dotenv
from agent_runtime.prompt_prefix import build_repair_l1_prefix
from agent_runtime.tool_context import ToolContext
from agent_runtime.workspace import WorkspaceContext
from src.agents.localizer import create_localizer
from src.agents.patcher import create_patcher
from src.agents.retriever import create_retriever
from src.agents.verifier import create_verifier
from src.orchestrator import Orchestrator
from src.tools.composite import build_repair_canonical_tools

O = TypeVar("O", bound=Orchestrator)

__all__ = [
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
    with_retriever: bool = True,
    skip_verify: bool = False,
    dry_run: bool = False,
) -> O:
    """装配 Localizer / Retriever / Patcher / 可选 Verifier。"""
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
    agent_kw = {"l1_prefix": l1, "dry_run": dry_run}
    localizer = create_localizer(client, ws, cwd=repo, **agent_kw)
    retriever = create_retriever(client, ws, cwd=repo, **agent_kw) if with_retriever else None
    patcher = create_patcher(client, ws, cwd=repo, **agent_kw)
    orch = orch_class(
        localizer,
        retriever,
        patcher,
        use_pytest_verify=not skip_verify,
        l1_prompt_cache_key=l1.hash,
    )
    if not skip_verify:
        verifier = try_create_verifier(client, ws, repo, **agent_kw)
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
