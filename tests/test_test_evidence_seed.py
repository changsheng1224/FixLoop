"""Localization uses observed or public test evidence, without dataset hints."""

from src.repair.localization.localize_fastpath import (
    rule_first_suspects,
    seed_rule_first_suspects,
    suspects_from_test_evidence,
)
from src.state import RepairState


def _repo(root):
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "worker.py").write_text("def work(x):\n    return x + 1\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_worker.py").write_text(
        "from pkg.worker import work\n\ndef test_work():\n    assert work(1) == 2\n",
        encoding="utf-8",
    )
    return "tests/test_worker.py::test_work"


def test_observed_test_maps_to_impl(tmp_path):
    nodeid = _repo(tmp_path)
    suspects = suspects_from_test_evidence([nodeid], tmp_path)
    assert any(s.file_path == "pkg/worker.py" and s.reason == "测试覆盖边" for s in suspects)


def test_related_test_and_failure_feedback_seed_impl(tmp_path):
    nodeid = _repo(tmp_path)
    suspects = rule_first_suspects("wrong result", tmp_path, fail_nodeids=[nodeid])
    assert "pkg/worker.py" in [s.file_path for s in suspects]


def test_hidden_patch_in_timings_is_not_localization_evidence(tmp_path):
    _repo(tmp_path)
    state = RepairState(issue_input="wrong result")
    state.node_timings["verify_test_patch"] = (
        "--- a/tests/test_hidden.py\n+++ b/tests/test_hidden.py\n"
        "@@ -0,0 +1 @@\n+from pkg.worker import work\n"
    )
    assert seed_rule_first_suspects(state, tmp_path, enable_semantic_expand=False) == []


def test_empty_budget_does_not_build_suspects(tmp_path):
    nodeid = _repo(tmp_path)
    assert suspects_from_test_evidence([nodeid], tmp_path, max_keep=0) == []
