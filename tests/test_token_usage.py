"""评测 token 用量收集测试。"""

import json

from agent_runtime.providers.clients import FakeModelClient

from src.eval.token_usage import (
    build_repair_token_usage,
    build_token_usage_summary,
    collect_repo_token_reports,
    get_client_session_usage,
    reset_client_session_usage,
)


class TestFakeClientSessionUsage:
    def test_session_usage_accumulates(self):
        client = FakeModelClient(["<final>a</final>", "<final>b</final>"])
        client.complete("hello world " * 10)
        client.complete("goodbye " * 5)
        usage = get_client_session_usage(client)
        assert usage["api_calls"] == 2
        assert usage["total_tokens"] > 0

    def test_reset_session_usage(self):
        client = FakeModelClient(["<final>ok</final>"])
        client.complete("prompt")
        reset_client_session_usage(client)
        usage = get_client_session_usage(client)
        assert usage["total_tokens"] == 0
        assert usage["api_calls"] == 0


class TestCollectRepoTokenReports:
    def test_reads_agent_run_reports(self, temp_workspace):
        run_dir = temp_workspace / ".agent" / "runs" / "20260703-test"
        run_dir.mkdir(parents=True)
        (run_dir / "report.json").write_text(
            json.dumps(
                {
                    "total_tokens": 1200,
                    "token_usage": {"system": 200, "request": 1000},
                }
            ),
            encoding="utf-8",
        )
        summary = collect_repo_token_reports(temp_workspace)
        assert summary["estimated_total"] == 1200
        assert summary["estimated_sections"]["system"] == 200
        assert summary["run_count"] == 1


class TestBuildTokenUsageSummary:
    def test_prefers_api_usage_over_estimated(self, temp_workspace):
        run_dir = temp_workspace / ".agent" / "runs" / "run-a"
        run_dir.mkdir(parents=True)
        (run_dir / "report.json").write_text(
            json.dumps({"total_tokens": 500, "token_usage": {"request": 500}}),
            encoding="utf-8",
        )
        client = FakeModelClient(["<final>ok</final>"])
        client.complete("x" * 400)
        summary = build_token_usage_summary(client, temp_workspace)
        assert summary["estimated_total"] == 500
        assert summary["total_tokens"] >= summary["input_tokens"]
        assert summary["api_calls"] == 1

    def test_sums_multiple_clients(self, temp_workspace):
        client_a = FakeModelClient(["<final>a</final>"])
        client_b = FakeModelClient(["<final>b</final>"])
        client_a.complete("aaa" * 20)
        client_b.complete("bbb" * 20)
        summary = build_repair_token_usage([client_a, client_b], temp_workspace)
        assert summary["api_calls"] == 2
        assert summary["total_tokens"] > 0
