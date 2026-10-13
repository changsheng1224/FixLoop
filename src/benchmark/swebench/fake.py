"""Offline provider smoke, using the same repair service as live evaluation."""

from agent_runtime.providers.clients import FakeModelClient
from src.repair.assembly import create_repair_service


def offline_service_factory(repo_path):
    return create_repair_service(
        FakeModelClient(
            [
                '{"conclusion":"No offline repair model is configured."}',
                '<final>{"status":"cannot_patch","reason":"offline smoke"}</final>',
            ]
        ),
        repo_path,
        workspace_policy="provided",
        execution_tier="host",
        runtime_overrides={"provider": "fake", "model": "gpt-4o", "auto_context_extraction": False},
    )
