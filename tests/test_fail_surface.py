"""失败面利用：断言截取、测试原文、verify target 收窄。"""

from __future__ import annotations

from pathlib import Path

from src.orchestrator import Orchestrator
from src.repair.verification.fail_surface import (
    FailSurface,
    apply_verify_feedback_to_state,
    build_fail_surface,
    build_fail_surface_prompt_block,
    build_verify_feedback_payload,
    preferred_verify_targets,
    prioritize_failed_nodeids,
    read_test_function_excerpt,
    render_verify_feedback_block,
)
from src.state import CandidatePatch, RepairState, RetrievedContext, VerificationResult


def _write_test(repo: Path) -> str:
    rel = "pkg/tests/test_sample.py"
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "import pytest",
                "",
                "@pytest.mark.xfail",
                "def test_add():",
                "    assert add(1, 2) == 3",
                "",
                "def test_other():",
                "    assert True",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return rel


class TestReadTestFunctionExcerpt:
    def test_extracts_decorated_function(self, tmp_path: Path):
        rel = _write_test(tmp_path)
        nodeid = f"{rel}::test_add"
        excerpt = read_test_function_excerpt(tmp_path, nodeid)
        assert "@pytest.mark.xfail" in excerpt
        assert "def test_add" in excerpt
        assert "assert add(1, 2) == 3" in excerpt
        assert "def test_other" not in excerpt


class TestBuildFailSurface:
    def test_prompt_block_includes_assertion_and_excerpt(self, tmp_path: Path):
        rel = _write_test(tmp_path)
        nodeid = f"{rel}::test_add"
        state = RepairState(issue_input="bug")
        state.node_timings["verify_failed_nodeids"] = [nodeid]
        state.verification_result = VerificationResult(
            all_passed=False,
            total_tests=1,
            failed=1,
            failure_logs=[
                f"FAILED {nodeid} - AssertionError: assert 4 == 3",
                "E       assert 4 == 3",
            ],
        )
        surface = build_fail_surface(state, repo_root=str(tmp_path))
        assert surface.verify_target == nodeid
        assert any("assert 4 == 3" in a for a in surface.assertions)
        assert nodeid in surface.test_excerpts
        block = build_fail_surface_prompt_block(surface)
        assert "FAIL SURFACE" in block
        assert "test_add" in block
        assert "read_file" in block

    def test_env_bucket_skips_read_test_call_to_action(self):
        surface = FailSurface(
            assertions=["sandbox upload did not complete"],
            nodeids=["tests/x.py::test_y"],
        )
        block = build_fail_surface_prompt_block(surface, bucket="env")
        assert "ENV" in block
        assert "禁止" in block


class TestPrioritizeAndPick:
    def test_prioritize_puts_failed_first(self):
        state = RepairState(issue_input="x")
        state.retrieved_context = RetrievedContext(related_tests=["old.py::t"])
        prioritize_failed_nodeids(state, ["new/test_a.py::test_b"])
        assert state.retrieved_context.related_tests[0] == "new/test_a.py::test_b"
        assert "old.py::t" in state.retrieved_context.related_tests

    def test_preferred_targets_order(self):
        state = RepairState(issue_input="x")
        state.node_timings["verify_failed_nodeids"] = ["fail.py::t1"]
        state.retrieved_context = RetrievedContext(related_tests=["other.py::t2"])
        targets = preferred_verify_targets(state)
        assert targets[0] == "fail.py::t1"
        assert "other.py::t2" in targets

    def test_pick_test_path_prefers_failed_nodeid(self, tmp_path: Path):
        rel = _write_test(tmp_path)
        nodeid = f"{rel}::test_add"
        orch = Orchestrator.__new__(Orchestrator)
        orch._repo_root = str(tmp_path)
        orch._repair_ctx = None
        state = RepairState(issue_input="x")
        state.node_timings["verify_failed_nodeids"] = [nodeid]
        state.retrieved_context = RetrievedContext(related_tests=["does/not/exist.py"])
        picked = orch._pick_test_path(state)
        assert picked == nodeid


class TestFeedbackIncludesFailSurface:
    def test_build_feedback_has_fail_surface_section(self, tmp_path: Path):
        rel = _write_test(tmp_path)
        nodeid = f"{rel}::test_add"
        orch = Orchestrator.__new__(Orchestrator)
        orch._repo_root = str(tmp_path)
        result = VerificationResult(
            all_passed=False,
            total_tests=1,
            failed=1,
            failure_logs=[f"FAILED {nodeid} - AssertionError: assert 0"],
        )
        state = RepairState(issue_input="x")
        state.candidate_patches = [
            CandidatePatch(file_path="a.py", diff="+x", original_lines="a", patched_lines="b")
        ]
        feedback = orch._build_feedback(result, state=state)
        assert "失败面" in feedback or "FAIL SURFACE" in feedback
        assert "test_add" in feedback

    def test_structured_feedback_payload_is_persisted(self, tmp_path: Path):
        rel = _write_test(tmp_path)
        nodeid = f"{rel}::test_add"
        state = RepairState(issue_input="x")
        state.node_timings["verify_failed_nodeids"] = [nodeid]
        state.candidate_patches = [CandidatePatch(file_path="pkg/mod.py", diff="+x")]
        result = VerificationResult(
            all_passed=False,
            failed=1,
            failure_logs=[f"FAILED {nodeid} - AssertionError: assert 4 == 3"],
        )

        payload = build_verify_feedback_payload(
            state,
            repo_root=str(tmp_path),
            result=result,
        )
        apply_verify_feedback_to_state(state, payload)
        block = render_verify_feedback_block(payload)

        assert payload.verify_target == nodeid
        assert payload.patch_files == ["pkg/mod.py"]
        assert "read_failed_test" in payload.next_action
        assert state.node_timings["structured_verify_feedback"]["verify_target"] == nodeid
        assert "结构化验证反馈" in block


class TestFailureDecision:
    def test_logic_failure_replans_against_same_test(self):
        from src.repair.verification.failure_decision import decide_verification_failure

        result = VerificationResult(
            all_passed=False,
            failed=1,
            failure_logs=["FAILED tests/test_calc.py::test_add - AssertionError"],
        )
        decision = decide_verification_failure(result)
        assert decision.failure_class == "verify_logic"
        assert decision.retryable is True
        assert "reverify_same_target" in decision.next_action
        assert decision.evidence_refs == ["tests/test_calc.py::test_add"]

    def test_environment_failure_blocks_business_patch(self):
        from src.repair.verification.failure_decision import decide_verification_failure

        result = VerificationResult(
            all_passed=False,
            failure_logs=["sandbox pip install failed: network unavailable"],
        )
        decision = decide_verification_failure(result)
        assert decision.failure_class == "verify_environment"
        assert decision.retryable is False
        assert decision.next_action == "repair_verification_environment"
