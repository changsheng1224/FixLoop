"""Explicit create authorization reaches Plan, Agent tools and diff export."""

import json

from src.repair.execution.edit_lock import EditLockState
from tests.plan_l2_support import repair_fixture


def test_agent_creates_missing_module_and_test(tmp_path):
    owner, state, client = repair_fixture(tmp_path)
    lock = EditLockState(repo_root=tmp_path, allowed_edit={"value.py"})
    lock.write_serial = False
    owner._edit_lock = owner.patcher.tool_context.edit_lock = lock
    client._outputs = [
        '{"conclusion":"Create a missing module and its test through explicit creation grants."}',
        '<tool>{"name":"read_file","args":{"path":"value.py"}}</tool>',
        '<tool>{"name":"expand_lock","args":{"path":"new.py","operation":"create"}}</tool>',
        '<tool>{"name":"expand_lock","args":{"path":"tests/test_new.py","operation":"create"}}</tool>',
        "<tool>" + json.dumps({"name": "apply_patch", "args": {"patch":
            "*** Begin Patch\n*** Add File: new.py\n+def answer():\n+    return 2\n"
            "*** Add File: tests/test_new.py\n+from new import answer\n"
            "+def test_answer():\n+    assert answer() == 2\n*** End Patch\n"}}) + "</tool>",
        "<final>Created the module and test.</final>",
    ]
    try:
        patches, meta = owner._run_patcher_toolized(state, state.issue_input, {})
        assert {p.file_path for p in patches} == {"new.py", "tests/test_new.py"}, (meta, state.agent_errors)
        assert state.control.allowed_create == ["new.py", "tests/test_new.py"]
        assert owner._plan_binding.session.plan.node("edit").status == "succeeded"
        assert (tmp_path / "tests/test_new.py").is_file()
    finally:
        if owner._plan_binding:
            owner._plan_binding.close()
