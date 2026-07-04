"""修复 Orchestrator 工厂（repair / eval 共用）。"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from agent_runtime.providers.clients import AnthropicCompatibleModelClient
from agent_runtime.workspace import WorkspaceContext
from src.agents.localizer import create_localizer
from src.agents.patcher import create_patcher
from src.agents.retriever import create_retriever
from src.agents.verifier import create_verifier
from src.orchestrator import Orchestrator

O = TypeVar("O", bound=Orchestrator)


def load_dotenv() -> None:
    """从 cwd/.env 加载 KEY=VAL 到 os.environ（不覆盖已有变量）。"""
    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if key and key not in os.environ:
                os.environ[key] = val


def create_model_client(model_client=None):
    """返回传入 client 或从环境变量构造 AnthropicCompatibleModelClient。"""
    if model_client is not None:
        return model_client
    load_dotenv()
    return AnthropicCompatibleModelClient(
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"),
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic/v1"),
        api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
    )


def try_create_verifier(client, ws, repo: str):
    """Docker 可用时创建 Verifier Agent，否则返回 None。"""
    try:
        import docker as _docker

        _docker.from_env().ping()
    except Exception:
        return None
    try:
        return create_verifier(client, ws, cwd=repo)
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
    localizer = create_localizer(client, ws, cwd=repo)
    retriever = create_retriever(client, ws, cwd=repo) if with_retriever else None
    patcher = create_patcher(client, ws, cwd=repo)
    if dry_run:
        localizer.dry_run = True
        if retriever is not None:
            retriever.dry_run = True
        patcher.dry_run = True
    orch = orch_class(localizer, retriever, patcher, use_pytest_verify=not skip_verify)
    if not skip_verify:
        verifier = try_create_verifier(client, ws, repo)
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
