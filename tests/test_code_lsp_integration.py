"""Real pylsp smoke test; a skip does not satisfy P2 acceptance."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent_runtime.code_exploration.service import CodeExplorationService
from agent_runtime.tool_context import ToolContext


def test_real_pylsp_definition_and_references():
    executable = Path(shutil.which("pylsp") or Path.home() / "anaconda3/Scripts/pylsp.exe")
    if not executable.is_file():
        pytest.skip("pylsp unavailable; local P2 acceptance requires a real server run")
    root = Path(__file__).parent / "fixtures/code_exploration/repos/alias_reference"
    ctx = ToolContext(root=str(root), exploration_mode="lsp")
    service = CodeExplorationService(ctx, mode="lsp", server_argv=(str(executable),))
    try:
        definition = service.lookup({"path": "runner.py", "line": 5, "column": 12})
        facts = definition.metadata["retrieval_result"]
        assert facts["degradation_reason"] is None, definition.content
        assert any(hit["path"] == "operations.py" for hit in facts["hits"]), definition.content
        references = service.lookup(
            {"path": "operations.py", "line": 1, "column": 5, "operation": "references"}
        )
        ref_facts = references.metadata["retrieval_result"]
        assert ref_facts["degradation_reason"] is None, references.content
        assert any(hit["path"] == "runner.py" for hit in ref_facts["hits"]), references.content
    finally:
        process = service.client.process if service.client else None
        service.close()
        assert process is None or process.poll() is not None


def test_real_pylsp_distinguishes_same_named_definitions():
    executable = Path(shutil.which("pylsp") or Path.home() / "anaconda3/Scripts/pylsp.exe")
    if not executable.is_file():
        pytest.skip("pylsp unavailable; local P2 acceptance requires a real server run")
    root = Path(__file__).parent / "fixtures/code_exploration/repos/same_name"
    ctx = ToolContext(root=str(root), exploration_mode="lsp")
    service = CodeExplorationService(ctx, mode="lsp", server_argv=(str(executable),))
    try:
        response = service.lookup({"path": "caller.py", "line": 5, "column": 12})
        facts = response.metadata["retrieval_result"]
        assert facts["degradation_reason"] is None, response.content
        assert {hit["path"] for hit in facts["hits"]} == {"beta.py"}, response.content
    finally:
        process = service.client.process if service.client else None
        service.close()
        assert process is None or process.poll() is not None
