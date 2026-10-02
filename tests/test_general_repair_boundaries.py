"""Repair decisions use public evidence, never evaluation-only artifacts."""

from types import SimpleNamespace

import pytest

from src.orchestrator import Orchestrator
from src.repair.failure_tags import allowed_patch_files
from src.state import RepairState


def test_evaluation_patch_is_rejected_at_repair_api_boundary():
    orch = Orchestrator(None)
    with pytest.raises(TypeError, match="verify_test_patch"):
        orch.repair("public issue", verify_test_patch="hidden test contents")


@pytest.mark.parametrize(
    "failure",
    [
        "ValueError: invalid literal for int() with base 10: 'N/A'",
        "TypeError: unsupported operand type(s) for +: 'int' and 'str'",
    ],
)
def test_error_text_is_evidence_without_prescribed_patch(failure):
    from src.state import VerificationResult

    feedback = Orchestrator(None)._build_feedback(
        VerificationResult(
            all_passed=False,
            total_tests=1,
            failed=1,
            failure_logs=[failure],
        )
    )
    assert failure in feedback
    assert "修复约束" not in feedback
    assert "raw.isdigit()" not in feedback
    assert "str(...)" not in feedback


def test_verifier_does_not_apply_hidden_patch_from_private_context(tmp_path, monkeypatch):
    target = tmp_path / "test_value.py"
    original = "def test_value():\n    assert True\n"
    target.write_text(original, encoding="utf-8")
    orch = Orchestrator(None)
    orch._repo_root = str(tmp_path)
    orch._repair_ctx = SimpleNamespace(
        cancel_token=None,
        verify_test_patch=(
            "--- a/test_value.py\n+++ b/test_value.py\n@@ -1,2 +1,2 @@\n"
            " def test_value():\n-    assert True\n+    assert False\n"
        ),
    )
    from src.state import VerificationResult

    def verify(state, **kwargs):
        assert target.read_text(encoding="utf-8") == original
        return VerificationResult(all_passed=True, total_tests=1, passed=1)

    monkeypatch.setattr(orch, "_run_verifier_python", verify)
    assert orch._run_verifier_impl(RepairState(issue_input="public report")).all_passed
    assert target.read_text(encoding="utf-8") == original


def test_hidden_test_patch_does_not_expand_allowed_files(tmp_path):
    (tmp_path / "secret.py").write_text("def work():\n    return 1\n", encoding="utf-8")
    state = RepairState(issue_input="public bug report")
    state.node_timings.update(
        _repo_root_hint=str(tmp_path),
        verify_test_patch=(
            "--- a/tests/test_hidden.py\n+++ b/tests/test_hidden.py\n"
            "@@ -0,0 +1 @@\n+from secret import work\n"
        ),
    )
    assert allowed_patch_files(state) == set()


def test_ambiguous_test_name_does_not_choose_first_file(tmp_path):
    for name in ("test_alpha.py", "test_beta.py"):
        (tmp_path / name).write_text("def test_value():\n    pass\n", encoding="utf-8")
    from src.state import RetrievedContext

    orch = Orchestrator(None)
    orch._repo_root = str(tmp_path)
    state = RepairState(
        issue_input="public bug", retrieved_context=RetrievedContext(related_tests=["test_value"])
    )
    assert orch._pick_test_path(state) == ""


def test_bare_test_name_is_resolved_with_class_scope(tmp_path):
    (tmp_path / "test_value.py").write_text(
        "class TestValue:\n    def test_value(self):\n        pass\n", encoding="utf-8"
    )
    from src.state import RetrievedContext

    orch = Orchestrator(None)
    orch._repo_root = str(tmp_path)
    state = RepairState(
        issue_input="public bug", retrieved_context=RetrievedContext(related_tests=["test_value"])
    )
    assert orch._pick_test_path(state) == "test_value.py::TestValue::test_value"
