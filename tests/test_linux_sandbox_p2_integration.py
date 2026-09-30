"""Explicit WSL-native P2 route check; not part of ordinary Windows tests."""

import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

if sys.platform == "linux" and os.environ.get("FIXLOOP_P2_CHECKOUT_ROOT"):
    checkout = Path(os.environ["FIXLOOP_P2_CHECKOUT_ROOT"])
    sys.path.insert(0, str(checkout))
    package = types.ModuleType("agent_runtime")
    package.__path__ = [str(checkout / "agent_runtime")]
    sys.modules["agent_runtime"] = package

from agent_runtime.linux_sandbox import LinuxSandboxBackend, SandboxPolicy
from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import tool_quick_test, tool_run_shell
from src.repair.verification.verify import BwrapVerifyStrategy


@pytest.fixture
def sandbox():
    base = os.environ.get("FIXLOOP_P1_ROOT")
    controller = os.environ.get("FIXLOOP_P1_CONTROLLER_ROOT")
    if sys.platform != "linux" or not base or not controller:
        pytest.skip("explicit native WSL fixture required")
    with tempfile.TemporaryDirectory(dir=base, prefix="p2-route-") as temporary:
        workspace = Path(temporary) / "workspace"
        state = Path(temporary) / "state"
        workspace.mkdir(mode=0o700)
        state.mkdir(mode=0o700)
        backend = LinuxSandboxBackend(
            SandboxPolicy(
                workspace,
                state,
                Path(base) / "toolchain",
                Path(controller) / "agent_runtime/linux_sandbox/supervisor.py",
            )
        )
        yield ToolContext(root=str(workspace), sandbox_backend=backend), backend, workspace


def test_tool_and_final_verifier_use_real_receipts(sandbox):
    ctx, backend, workspace = sandbox
    (workspace / "test_small.py").write_text("def test_ok():\n    assert True\n")
    shell = tool_run_shell(ctx, {"command": "python -c 'print(42)'"})
    assert shell.ok and shell.metadata["actual_backend"] == "linux_sandbox", shell
    assert shell.metadata["sandbox_cleanup"] == "confirmed"
    quick = tool_quick_test(ctx, {"nodeid": "test_small.py::test_ok"})
    assert quick.ok and "1 passed" in quick.content, quick
    final = BwrapVerifyStrategy(ctx).run(str(workspace))
    assert final.result.all_passed, final
    assert final.internal["receipt_id"] not in {
        shell.metadata["sandbox_receipt_id"],
        quick.metadata["sandbox_receipt_id"],
    }
    assert (
        backend.store.reconcile(backend.policy.digest())["result"]["receipt_id"]
        == final.internal["receipt_id"]
    )
