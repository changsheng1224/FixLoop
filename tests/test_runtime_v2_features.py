from __future__ import annotations

import subprocess


def test_provider_finish_normalization():
    from agent_runtime.model_turn import (
        FinishKind,
        normalize_anthropic_finish,
        normalize_openai_finish,
    )

    assert normalize_anthropic_finish("tool_use", has_tools=True, has_text=False) == FinishKind.TOOL_CALLS
    assert normalize_anthropic_finish("max_tokens", has_tools=False, has_text=True) == FinishKind.MAX_OUTPUT_TOKENS
    assert normalize_openai_finish("length", has_text=True) == FinishKind.MAX_OUTPUT_TOKENS
    assert normalize_openai_finish("stop", has_text=False) == FinishKind.EMPTY_OUTPUT


def test_grouped_budget_preserves_write_and_verify_capacity():
    from agent_runtime.tool_budget import ToolBudgetGroup, ToolBudgetLedger

    ledger = ToolBudgetLedger({"read": 1, "write": 1, "verify": 1, "recovery": 0})
    ledger.record(ToolBudgetGroup.READ)
    assert not ledger.check(ToolBudgetGroup.READ).allowed
    assert ledger.check(ToolBudgetGroup.WRITE).allowed
    assert ledger.check(ToolBudgetGroup.VERIFY).allowed
    assert ledger.summary()["read"]["remaining"] == 0


def test_manifest_profile_runs_structured_steps(monkeypatch, tmp_path):
    from src.repair.verification.verification_profiles import select_verification_profile
    from src.repair.verification.verification_runner import run_profile

    (tmp_path / "go.mod").write_text("module example\n", encoding="utf-8")
    profile = select_verification_profile(tmp_path, "go")
    assert profile is not None
    monkeypatch.setattr("shutil.which", lambda name: name)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "ok", ""),
    )
    result = run_profile(tmp_path, profile)
    assert result["all_passed"] is True
    assert result["category"] == "target_tests_passed"
    assert result["steps"][0]["command"][0] == "go"
