"""Regression coverage for operation scope, startup policy and typed receipts."""

import importlib.util
import json
import os
import sys
from types import SimpleNamespace

import pytest

from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import tool_apply_patch, tool_write_file
from src.repair.execution.edit_lock import EditLockState
from src.repair.execution.patch_applier import PatchApplier
from src.repair.execution_state import VerificationReceipt
from src.repair.language_capabilities import project_command
from src.repair.phase_fsm import RepairPhaseFSM
from src.repair.verification.execution_policy import VerificationBackend, resolve_execution_policy
from src.state import CandidatePatch, RepairState
from src.tools.repair_control import tool_expand_lock


def test_create_missing_module_then_modify_and_delete_require_own_grants(tmp_path):
    context = ToolContext(str(tmp_path))
    lock = context.edit_lock = EditLockState(repo_root=tmp_path, max_expand=5)
    lock.write_serial = False
    assert not tool_write_file(context, {"path": "pkg/new.py", "content": "x=1\n"}).ok
    assert tool_expand_lock(context, {"path": "pkg/new.py", "operation": "create"}).ok
    assert tool_write_file(context, {"path": "pkg/new.py", "content": "x=1\n"}).ok
    assert not tool_write_file(context, {"path": "pkg/new.py", "content": "x=2\n"}).ok
    assert tool_expand_lock(context, {"path": "pkg/new.py", "operation": "modify"}).ok
    assert lock.mark_read("pkg/new.py")
    assert tool_write_file(context, {"path": "pkg/new.py", "content": "x=2\n"}).ok
    patch = "*** Begin Patch\n*** Delete File: pkg/new.py\n*** End Patch\n"
    assert not tool_apply_patch(context, {"patch": patch}).ok
    assert tool_expand_lock(context, {"path": "pkg/new.py", "operation": "delete"}).ok
    assert not tool_apply_patch(context, {"patch": patch}).ok
    assert lock.mark_read("pkg/new.py")
    assert tool_apply_patch(context, {"patch": patch}).ok
    assert not (tmp_path / "pkg/new.py").exists()
    assert not lock.expand_lock("../escape.py", operation="create")[0]


@pytest.mark.parametrize("suffix", ["py", "ts", "js", "java", "go", "rs"])
def test_read_authorizes_supported_implementations_equally(tmp_path, suffix):
    path = "module." + suffix
    (tmp_path / path).write_text("code\n")
    lock = EditLockState(repo_root=tmp_path, max_expand=0)
    assert lock.mark_read(path, auto_allow_impl=True)
    assert lock.check_write(path)[0]
    assert lock.expand_count == 0


@pytest.mark.parametrize("host,static,expected", [(True, False, "host"), (False, True, "static")])
def test_disabled_auto_ignores_available_container(host, static, expected):
    policy = resolve_execution_policy(
        sandbox_policy="disabled",
        execution_tier="auto",
        verifier=VerificationBackend(),
        host=host,
        static=static,
    )
    assert policy.initial_tier("python") == expected
    assert not policy.container_available


@pytest.mark.parametrize(
    "args",
    [
        {"sandbox_policy": "disabled", "execution_tier": "container"},
        {"sandbox_policy": "required", "execution_tier": "host"},
        {"sandbox_policy": "required", "execution_tier": "static"},
        {"sandbox_policy": "disabled", "sandbox": True},
    ],
)
def test_conflicting_policy_rejected(args):
    with pytest.raises(ValueError, match="conflict"):
        resolve_execution_policy(**args)


def test_project_local_python_and_typescript_preferred(tmp_path, monkeypatch):
    python = (
        tmp_path / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    )
    python.parent.mkdir(parents=True)
    python.touch()
    assert project_command(tmp_path, ["pytest", "-q"])[0] == str(python.resolve())
    tsc = tmp_path / "node_modules" / ".bin" / ("tsc.cmd" if sys.platform == "win32" else "tsc")
    tsc.parent.mkdir(parents=True)
    tsc.touch()
    monkeypatch.setattr("shutil.which", lambda *_: None)
    command = project_command(tmp_path, ["tsc", "--noEmit"])
    assert str(tsc.resolve()) in " ".join(command)


@pytest.mark.parametrize("exit_code", [0, 2])
def test_static_typescript_executes_project_local_checker(tmp_path, monkeypatch, exit_code):
    from src.repair.verification.verify import StaticVerifyStrategy

    root = tmp_path / "project with spaces"
    root.mkdir()
    (root / "main.ts").write_text("export const value = 1;\n")
    (root / "tsconfig.json").write_text('{}')
    checker = root / "checker.py"
    checker.write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "Path('checker-args.json').write_text(json.dumps(sys.argv[1:]))\n"
        f"sys.exit({exit_code})\n"
    )
    wrapper = root / "node_modules" / ".bin" / ("tsc.cmd" if os.name == "nt" else "tsc")
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(
        f'@"{sys.executable}" "{checker}" %*\n'
        if os.name == "nt"
        else f'#!/bin/sh\nexec "{sys.executable}" "{checker}" "$@"\n'
    )
    wrapper.chmod(0o755)
    monkeypatch.setattr("shutil.which", lambda *_: pytest.fail("used global toolchain"))
    result = StaticVerifyStrategy().run(str(root), language="typescript")
    assert result.result.all_passed == (exit_code == 0)
    assert result.internal["cleanup"] == "confirmed"
    assert result.internal["checked_files"] == 1
    assert json.loads((root / "checker-args.json").read_text()) == [
        "--noEmit", "--pretty", "false", "-p", str(root.resolve())
    ]
    assert result.internal["steps"][0]["exit_code"] == exit_code


def test_metrics_cannot_expand_patch_scope():
    from src.benchmark.swebench.patch_export import suspect_paths_from_state
    from src.repair.failure_tags import allowed_patch_files

    state = RepairState("bug")
    state.node_timings["allowed_patch_extra"] = ["forged.py"]
    state.control.allowed_patch_extra = ["grounded.py"]
    assert allowed_patch_files(state) == {"grounded.py"}
    assert suspect_paths_from_state(state) == ["grounded.py"]


def test_receipts_and_fsm_restore_ignore_forged_metrics():
    state = RepairState("bug", phase="patch")
    state.execution.verification_receipt = VerificationReceipt(completed=False, cleanup="unknown")
    state.node_timings["plan_verification_receipt"] = {"completed": True, "cleanup": "confirmed"}
    state.node_timings["phase_fsm"] = {"revision": 999, "phase": "done"}
    restored = RepairState.from_dict(state.to_dict())
    assert not restored.execution.verification_receipt.completed
    assert RepairPhaseFSM.from_state(restored).revision == 0
    assert RepairPhaseFSM.from_state(restored).phase == "patch"


def test_explicit_import_patch_leaves_calls_methods_and_strings_alone(tmp_path):
    text = "from module import old\nold()\nobj.old()\nmessage = 'old()'\n"
    path = tmp_path / "main.py"
    path.write_text(text)
    patch = CandidatePatch(
        file_path="main.py",
        original_lines="from module import old",
        patched_lines="from module import new",
    )
    assert PatchApplier(str(tmp_path)).apply_patches([patch])
    assert path.read_text() == text.replace("from module import old", "from module import new")


def test_semantic_dependency_absence_degrades_with_install_guidance(monkeypatch, capsys):
    from agent_runtime.features.memory import semantic

    monkeypatch.setattr(importlib.util, "find_spec", lambda *_: None)
    monkeypatch.setattr(semantic, "_SEMANTIC_MODELS", {})
    monkeypatch.setattr(semantic, "_SEMANTIC_INIT_LOGGED", False)
    config = SimpleNamespace(model_id="missing", revision="test", max_seq_length=20)
    assert semantic._get_semantic_model(config) is None
    assert "[semantic]" in capsys.readouterr().err
