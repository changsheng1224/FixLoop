"""五层能力：规则优先定位、信息增益、env 失败面去误导。"""

from __future__ import annotations

from pathlib import Path

from src.repair.fail_surface import FailSurface, build_fail_surface_prompt_block
from src.repair.info_gain import InfoGainTracker
from src.repair.localize_fastpath import merge_llm_with_rule_first, rule_first_suspects
from src.repair.phase_clock import DEFAULT_LOCALIZE_TIMEOUT_S
from src.repair.verify_diagnose import VerifyBucket, diagnose_verification
from src.state import CandidatePatch, RepairPlan, SuspectLocation, VerificationResult


class TestInfraLocalizeBudget:
    def test_localize_budget_raised(self):
        assert DEFAULT_LOCALIZE_TIMEOUT_S >= 90


class TestLocalizeFastpath:
    def test_rule_first_from_plan_file(self, tmp_path: Path):
        target = tmp_path / "pkg" / "mod.py"
        target.parent.mkdir(parents=True)
        target.write_text("def foo():\n    return 1\n", encoding="utf-8")
        plan = RepairPlan(issue_type="bug", suspect_files=["pkg/mod.py"])
        suspects = rule_first_suspects(
            "bug in pkg/mod.py",
            tmp_path,
            plan,
            fallback_from_plan=lambda p, _i: [
                SuspectLocation(
                    file_path=f,
                    start_line=1,
                    end_line=1,
                    reason="plan",
                    confidence=0.7,
                )
                for f in p.suspect_files
            ],
        )
        assert any(s.file_path.replace("\\", "/") == "pkg/mod.py" for s in suspects)

    def test_merge_keeps_rule_when_llm_empty(self, tmp_path: Path):
        (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")
        rule = [
            SuspectLocation(
                file_path="a.py",
                start_line=1,
                end_line=1,
                reason="rule",
                confidence=0.8,
            )
        ]
        merged = merge_llm_with_rule_first(
            [],
            rule,
            issue="x",
            repo_root=tmp_path,
        )
        assert merged
        assert merged[0].file_path.replace("\\", "/") == "a.py"


class TestVerifyEnvUpload:
    def test_sandbox_upload_is_env_not_logic(self):
        result = VerificationResult(
            all_passed=False,
            total_tests=0,
            failure_logs=["sandbox upload did not complete"],
        )
        diag = diagnose_verification(result)
        assert diag.bucket == VerifyBucket.ENV
        assert "业务补丁" in diag.guidance or "环境" in diag.guidance
        assert "读失败测试" not in diag.guidance or "不要" in diag.guidance

    def test_structured_sandbox_error_helper(self):
        from src.harness.sandbox_verify import verification_result_for_sandbox_error

        vr = verification_result_for_sandbox_error(
            RuntimeError("sandbox upload did not complete")
        )
        assert vr.total_tests == 0
        assert any("verify_config:" in x for x in vr.failure_logs)
        diag = diagnose_verification(vr)
        assert diag.bucket == VerifyBucket.ENV


class TestFailSurfaceEnvBucket:
    def test_env_block_forbids_business_patch(self):
        surface = FailSurface(assertions=["sandbox upload did not complete"])
        block = build_fail_surface_prompt_block(surface, bucket="env")
        assert "ENV" in block
        assert "禁止" in block
        assert "read_file 打开上列测试" not in block


class TestInfoGain:
    def test_zero_gain_forces_shift(self):
        tr = InfoGainTracker(zero_gain_threshold=2)
        vr = VerificationResult(
            all_passed=False,
            failure_logs=["AssertionError: x"],
            total_tests=1,
            failed=1,
        )
        patches = [CandidatePatch(file_path="a.py", diff="+1", original_lines=["a"], patched_lines=["b"])]
        assert tr.record(vr, patches) is True
        assert tr.record(vr, patches) is False
        assert tr.record(vr, patches) is False
        assert tr.should_force_shift()
