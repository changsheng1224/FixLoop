"""修复 Orchestrator 工厂（repair / eval 共用）。"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from agent_runtime.providers.clients import AnthropicCompatibleModelClient
from agent_runtime.workspace import WorkspaceContext
from src.agents.localizer import create_localizer
from src.agents.patcher import create_patcher
from src.agents.retriever import create_retriever
from src.agents.verifier import create_verifier
from src.orchestrator import Orchestrator


def load_dotenv() -> None:
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
    if model_client is not None:
        return model_client
    load_dotenv()
    return AnthropicCompatibleModelClient(
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"),
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic/v1"),
        api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
    )


def try_create_verifier(client, ws, repo: str):
    try:
        import docker as _docker

        _docker.from_env().ping()
    except Exception:
        return None
    try:
        return create_verifier(client, ws, cwd=repo)
    except Exception:
        return None


def make_orchestrator_factory(
    *,
    skip_verify: bool = False,
    dry_run: bool = False,
    model_client=None,
) -> Callable[[str], Orchestrator]:
    """返回 `(repo_path) -> Orchestrator` 工厂。"""

    client = create_model_client(model_client)

    def factory(repo_path: str) -> Orchestrator:
        ws = WorkspaceContext.build(repo_path)
        repo = str(Path(repo_path).resolve())
        localizer = create_localizer(client, ws, cwd=repo)
        retriever = create_retriever(client, ws, cwd=repo)
        patcher = create_patcher(client, ws, cwd=repo)
        if dry_run:
            localizer.dry_run = True
            retriever.dry_run = True
            patcher.dry_run = True
        use_pytest = not skip_verify
        orch = Orchestrator(localizer, retriever, patcher, use_pytest_verify=use_pytest)
        if not skip_verify:
            verifier = try_create_verifier(client, ws, repo)
            if verifier:
                orch.verifier = verifier
        return orch

    return factory
