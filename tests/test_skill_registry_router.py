"""Executable Skill Registry / Router / runners / offline eval."""

from __future__ import annotations

import json

from src.skills.executable import (
    run_baseline_verify,
    run_draft_pr_prepare,
    run_github_issue_ingestion,
    run_patch_apply_check,
    run_regression_test_selection,
    run_repo_code_search,
    run_stacktrace_localization,
)
from src.skills.registry import (
    SkillRegistry,
    get_default_executable_registry,
    reset_default_executable_registry_for_tests,
)
from src.skills.router import SkillRouter
from src.skills.router_eval import evaluate_router, load_router_cases


def setup_function() -> None:
    reset_default_executable_registry_for_tests()


class TestRegistry:
    def test_loads_seven_skills(self):
        reg = SkillRegistry.from_default_specs()
        names = [s.name for s in reg.list()]
        assert names == [
            "baseline_verify",
            "draft_pr_prepare",
            "github_issue_ingestion",
            "patch_apply_check",
            "regression_test_selection",
            "repo_code_search",
            "stacktrace_localization",
        ]
        assert reg.resolve_version("stacktrace_localization")["version"] == "1.0.0"

    def test_spec_has_triggers_and_tools(self):
        spec = get_default_executable_registry().require("github_issue_ingestion")
        assert spec.positive_triggers
        assert "github_get_issue" in spec.allowed_tools
        assert spec.input_schema and spec.output_schema


class TestRunners:
    def test_github_ingestion_from_url(self):
        out = run_github_issue_ingestion(
            {"text": "see https://github.com/acme/demo/issues/12"}
        )
        assert out["ok"]
        assert out["issue_spec"]["owner"] == "acme"
        assert out["issue_spec"]["number"] == 12

    def test_github_ingestion_with_tool(self):
        def fake_get(args: dict) -> str:
            return json.dumps(
                {
                    "number": args["number"],
                    "title": "TypeError in calc",
                    "body": "repro",
                    "state": "open",
                    "html_url": "https://github.com/acme/demo/issues/1",
                    "labels": ["bug"],
                }
            )

        out = run_github_issue_ingestion(
            {"owner": "acme", "repo": "demo", "number": 1},
            github_get_issue=fake_get,
        )
        assert out["issue_spec"]["title"] == "TypeError in calc"
        assert out["issue_spec"]["source"] == "github_get_issue"

    def test_stack_localization(self):
        tb = (
            'Traceback (most recent call last):\n'
            '  File "calc.py", line 12, in add\n'
            "TypeError: bad"
        )
        out = run_stacktrace_localization({"traceback": tb})
        assert out["ok"]
        assert out["localization"]["exception_type"] == "TypeError"
        assert out["localization"]["frames"]

    def test_regression_selection_from_diff(self):
        diff = (
            "diff --git a/calc.py b/calc.py\n"
            "--- a/calc.py\n"
            "+++ b/calc.py\n"
            "@@ -1 +1 @@\n"
            "+x\n"
        )
        out = run_regression_test_selection({"diff": diff})
        assert out["ok"]
        assert "calc.py" in out["selection"]["changed_files"]
        assert out["selection"]["test_files"]

    def test_repo_code_search(self):
        out = run_repo_code_search({"text": "grep for parse_config definition"})
        assert out["ok"]
        assert "parse_config" in out["search"]["query"]

    def test_baseline_verify(self):
        out = run_baseline_verify({"text": "Run baseline tests before the fix"})
        assert out["ok"]
        assert out["baseline"]["expect_fail"] is True

    def test_patch_apply_check(self):
        out = run_patch_apply_check(
            {
                "text": "apply the patch\n"
                "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
            }
        )
        assert out["ok"]
        assert out["apply"]["status"] in ("ready", "dry_run")

    def test_draft_pr_prepare_forces_draft(self):
        out = run_draft_pr_prepare({"text": "create draft pr title: fix calc"})
        assert out["draft_pr"]["draft"] is True
        assert out["ok"]


class TestRouter:
    def test_route_github(self):
        d = SkillRouter().route(
            "Please ingest GitHub issue https://github.com/acme/demo/issues/12"
        )
        assert d.selected == "github_issue_ingestion"
        assert d.selection_reason in ("rule_short_circuit", "top1_margin")
        assert d.skill_version == "1.0.0"

    def test_route_new_skills(self):
        assert (
            SkillRouter().route("Please grep for ToolGateway in the repo").selected
            == "repo_code_search"
        )
        assert (
            SkillRouter().route("Run baseline tests before the fix").selected
            == "baseline_verify"
        )
        assert (
            SkillRouter().route("Apply the patch and smoke-check").selected
            == "patch_apply_check"
        )
        assert (
            SkillRouter().route("Create a draft PR against master").selected
            == "draft_pr_prepare"
        )

    def test_route_fallback_negative(self):
        d = SkillRouter().route("What is the weather in Beijing?")
        assert d.selected is None
        assert d.fallback is True

    def test_skill_switch_trace(self):
        d = SkillRouter().route(
            "Now apply the patch we generated",
            previous_selected="stacktrace_localization",
        )
        assert d.selected == "patch_apply_check"
        assert d.switched_from == "stacktrace_localization"


class TestOfflineEval:
    def test_at_least_100_cases_with_hard(self):
        cases = load_router_cases(include_hard=True, include_heldout=False)
        assert len(cases) >= 100

    def test_easy_set_high_top1(self):
        report = evaluate_router(include_hard=False, include_heldout=False)
        assert report.n >= 50
        assert report.top1 >= 0.95

    def test_full_eval_metrics(self):
        # CI 门槛只看 easy+hard；held-out 不卡分，避免过拟合
        report = evaluate_router(include_hard=True, include_heldout=False)
        assert report.n >= 100
        assert report.top1 >= 0.85
        data = report.to_dict()
        assert "mis_trigger" in data
        assert "by_tag" in data
        assert report.skill_switch_rate > 0  # hard set has switch cases
        hard_rows = [r for r in report.rows if "hard" in (r.get("tags") or [])]
        assert hard_rows
        hard_acc = sum(1 for r in hard_rows if r["correct"]) / len(hard_rows)
        assert hard_acc >= 0.80

    def test_heldout_loads_and_reports_without_gate(self):
        from src.skills.router_eval import load_heldout_cases

        held = load_heldout_cases()
        assert len(held) >= 70
        report = evaluate_router(held)
        # 只断言能跑出指标；不对 top1 设下限（故意暴露误漏）
        assert report.n >= 70
        assert 0.0 <= report.top1 <= 1.0
        assert "mis_trigger" in report.to_dict()
        assert "miss_trigger" in report.to_dict()
        # 确保 held-out 足够难：不应接近完美（否则集无效）
        assert report.top1 < 0.95
        zh_rows = [r for r in report.rows if "zh" in (r.get("tags") or [])]
        assert len(zh_rows) >= 20
