"""探索证据与强制探索回归。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from src.orchestrator import Orchestrator
from src.repair.explore_evidence import (
    explore_has_anchor,
    explore_quality,
    merge_retrieved_context,
    record_explore_quality,
)
from src.state import (
    RepairPlan,
    RepairState,
    RetrievedContext,
    SuspectLocation,
)


class TestExploreEvidence:
    def test_anchor_from_suspects(self):
        suspects = [SuspectLocation(file_path="a.py", start_line=1, end_line=1)]
        assert explore_has_anchor(suspects, None, None)

    def test_anchor_from_tests(self):
        ctx = RetrievedContext(related_tests=["tests/test_a.py"])
        assert explore_has_anchor([], ctx, None)

    def test_no_anchor(self):
        assert not explore_has_anchor([], RetrievedContext(), None)

    def test_merge_dedupes(self):
        a = RetrievedContext(related_tests=["t1"], similar_snippets=[{"file": "a.py"}])
        b = RetrievedContext(
            related_tests=["t1", "t2"],
            similar_snippets=[{"file": "a.py"}, {"file": "b.py"}],
        )
        m = merge_retrieved_context(a, b)
        assert m.related_tests == ["t1", "t2"]
        assert len(m.similar_snippets) == 2

    def test_record_sets_error(self):
        state = RepairState(issue_input="x")
        q = record_explore_quality(state, [], RetrievedContext())
        assert q["ok"] is False
        assert state.agent_errors.get("explore_insufficient")


class TestForceExploreRecovery:
    def test_recover_uses_force_then_rule(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "mod.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
        tests = repo / "tests"
        tests.mkdir()
        (tests / "test_mod.py").write_text("def test_foo():\n    pass\n", encoding="utf-8")

        orch = Orchestrator(None, None, MagicMock(), None)
        orch._repo_root = str(repo)
        orch.retriever = MagicMock()
        orch._force_explore_enabled = lambda: True
        orch._force_tool_explore = MagicMock(
            return_value=(
                RetrievedContext(related_tests=["tests/test_mod.py"]),
                {"total_ms": 5, "retrieval_path": "llm→force_explore"},
            )
        )
        orch._rule_retrieve = MagicMock(
            return_value=(
                RetrievedContext(
                    related_tests=["tests/test_mod.py"],
                    similar_snippets=[{"file": "mod.py", "text": "def foo"}],
                ),
                {"total_ms": 2, "retrieval_path": "rule"},
            )
        )

        state = RepairState(
            issue_input="foo broken",
            repair_plan=RepairPlan(issue_type="logic_error", suspect_files=["mod.py"]),
        )
        suspects = [
            SuspectLocation(
                file_path="mod.py",
                start_line=1,
                end_line=2,
                function_name="foo",
                reason="r",
            )
        ]
        ctx, timing = orch._recover_retrieval(
            state, suspects, "foo broken", state.repair_plan, prior=None
        )
        assert "tests/test_mod.py" in ctx.related_tests
        assert state.node_timings["retrieval_path"] == "llm→force_explore"
        orch._force_tool_explore.assert_called_once()

    def test_rule_retrieve_adds_snippets(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "mod.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
        (repo / "tests").mkdir()
        (repo / "tests" / "test_mod.py").write_text("def test_foo():\n    pass\n", encoding="utf-8")

        orch = Orchestrator(None, None, None, None)
        orch._repo_root = str(repo)
        suspects = [
            SuspectLocation(
                file_path="mod.py",
                start_line=1,
                end_line=2,
                function_name="foo",
                reason="r",
            )
        ]
        ctx, _ = orch._rule_retrieve(suspects, "def foo fail")
        assert any(s.get("file") == "mod.py" for s in ctx.similar_snippets)
        assert any("test_mod" in t for t in ctx.related_tests)
