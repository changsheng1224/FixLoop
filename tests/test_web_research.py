"""Research references should be small, scoped, attributable and optional to repair."""

import json
from types import SimpleNamespace

import pytest

from agent_runtime import web
from agent_runtime.budget_manager import BudgetManager
from agent_runtime.config import AgentConfig, WebConfig
from agent_runtime.config_loader import load_runtime_policy
from agent_runtime.context_runtime import ObservationStore
from agent_runtime.observation_recording import ToolObservationRecorder
from agent_runtime.repair_runtime import CanonicalToolCall
from agent_runtime.step_guard import StepContext, StepGuard
from agent_runtime.tool_context import ToolContext
from agent_runtime.tool_step_runtime import ToolStepState
from agent_runtime.web_research import canonical_web_url
from agent_runtime.web_tools import WEB_SCHEMAS
from src.repair.loop_policy import RepairLoopPolicy
from tests.test_web import Client, search_result


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-secret")
    monkeypatch.setattr(web, "_throttle", lambda *args: None)
    return ToolContext(
        root=str(tmp_path),
        web_config=WebConfig(enabled=True),
        web_client=Client(),
        runtime_budget=BudgetManager({"web_requests": 8}),
    )


def execute(ctx, name="web_search", **args):
    defaults = (
        {"query": "migration"} if name == "web_search" else {"url": "https://docs.example.com/v2"}
    )
    return web.execute_web_tool(ctx, name, {**defaults, "purpose": "Resolve an API gap", **args})


def test_scope_and_version_reach_provider_without_local_metadata(ctx):
    result = execute(ctx, domains=["DOCS.EXAMPLE.COM"], target_version="2.0")
    assert result.ok
    payload = ctx.web_client.calls[0][1]
    assert payload == {
        "query": "migration 2.0 site:docs.example.com",
        "count": 3,
        "need_content": False,
    }
    assert result.metadata["web_research"]["purpose"] == "Resolve an API gap"
    assert result.metadata["web_research"]["target_version"] == "2.0"
    assert result.data["research"]["version_verification"] == "unverified"
    assert "unverified" in result.content


@pytest.mark.parametrize(
    "domain",
    [
        "https://docs.example.com",
        "docs.example.com/v2",
        "*.example.com",
        "u:p@example.com",
        "example.com:443",
        "docs..example.com",
    ],
)
def test_domain_scope_is_hostname_only(ctx, domain):
    assert execute(ctx, domains=[domain]).error_code == "invalid_arguments"
    assert not ctx.web_client.calls


def test_query_limit_includes_filter_and_version_without_silent_truncation(ctx):
    assert (
        execute(ctx, query="x" * 120, target_version="2.0", domains=["example.com"]).error_code
        == "invalid_arguments"
    )
    assert not ctx.web_client.calls


def test_postfilter_rejects_provider_out_of_scope_and_malformed_links(ctx):
    urls = [
        "https://docs.example.com/v2",
        "https://api.docs.example.com/v2",
        "https://docs.example.com.evil.test/v2",
        "https://elsewhere.test",
        "file:///private",
        "https://user:pass@docs.example.com",
    ]
    rows = [{**search_result()["results"][0], "url": url} for url in urls]
    ctx.web_client = Client((200, {}, {"results": rows}))
    result = execute(ctx, domains=["docs.example.com"])
    assert len(result.data["results"]) == 2
    assert result.metadata["web_research"]["dropped"] == {
        "invalid_url": 2,
        "outside_domains": 2,
        "duplicate": 0,
    }
    assert len(result.metadata["raw_result"]["provider_response"]["results"]) == 6


def test_dedup_removes_tracking_fragments_and_raw_blob_aliases(ctx):
    urls = [
        "https://docs.example.com/v2?utm_source=x#one",
        "https://docs.example.com/v2#two",
        "https://github.com/owner/repo/blob/main/docs.md",
        "https://raw.githubusercontent.com/owner/repo/main/docs.md#section",
        "https://raw.githubusercontent.com/owner/repo/refs/heads/main/docs.md",
    ]
    ctx.web_client = Client(
        (200, {}, {"results": [{**search_result()["results"][0], "url": url} for url in urls]})
    )
    result = execute(ctx)
    assert len(result.data["results"]) == 2
    assert result.metadata["web_research"]["dropped"]["duplicate"] == 3
    assert canonical_web_url("https://github.com/o/r/blob/v1/a.md") != canonical_web_url(
        "https://github.com/o/r/blob/v2/a.md"
    )
    assert canonical_web_url(
        "https://raw.githubusercontent.com/o/r/refs/tags/v1/a.md"
    ) != canonical_web_url("https://raw.githubusercontent.com/o/r/refs/heads/v1/a.md")


def test_compact_summary_keeps_full_source_and_provider_scores(ctx):
    row = {
        **search_result("body " * 1000)["results"][0],
        "summary": "s" * 3000,
        "rerank_score": 0.04,
    }
    ctx.web_client = Client((200, {}, {"results": [row]}))
    result = execute(ctx)
    assert len(result.data["results"][0]["summary"]) == 700
    assert "content" not in result.data["results"][0]
    assert result.data["results"][0]["rerank_score"] == 0.04
    assert result.output_truncated
    assert result.metadata["raw_result"]["provider_response"]["results"][0] == row


def test_empty_filtered_results_give_stop_or_revise_guidance(ctx):
    result = execute(ctx, domains=["different.test"])
    assert result.ok and not result.data["results"]
    assert "local evidence" in result.data["research"]["guidance"]


def test_failed_request_still_records_research_intent(ctx):
    ctx.web_client = Client((401, {}, None))
    result = execute(ctx, target_version="2.0")
    assert result.error_code == "web_http_401"
    assert result.metadata["web_research"]["target_version"] == "2.0"
    assert result.metadata["web_research"]["purpose"] == "Resolve an API gap"


def test_preview_result_count_is_bounded_even_if_provider_ignores_count(ctx):
    rows = [
        {**search_result()["results"][0], "url": f"https://docs.example.com/{i}"} for i in range(6)
    ]
    ctx.web_client = Client((200, {}, {"results": rows}))
    result = execute(ctx)
    assert len(result.data["results"]) == 3
    assert result.metadata["web_research"]["limited_results"] == 3
    assert result.output_truncated


def test_focused_fetch_finds_late_evidence_without_rewriting_code(ctx):
    content = (
        "unrelated section\n\n" * 400
        + "# Migration\nEngine\\.execute was removed.\n```python\nwith engine.begin() as conn:\n    conn.execute(text('select 1'))\n```\n"
        + "later text\n" * 400
    )
    ctx.web_client = Client((200, {}, {"title": "Docs", "content": content}))
    result = execute(ctx, "web_fetch", focus="Engine execute engine.begin", target_version="2.0")
    preview = result.data["content"]
    research = result.data["research"]
    assert "Engine\\.execute" in preview
    assert "    conn.execute(text('select 1'))" in preview
    assert research["selection"] == "focus" and result.output_truncated
    for span in research["source_ranges"]:
        assert content[span["start"] : span["end"]] in preview
    assert result.metadata["raw_result"]["provider_response"]["content"] == content
    assert ctx.web_client.calls[0][1] == {"url": "https://docs.example.com/v2"}
    assert "Code formatting/completeness is unverified" in result.content


def test_focus_miss_is_explicit_and_raw_stays_expandable(ctx):
    ctx.web_client = Client((200, {}, {"title": "Docs", "content": "intro\n" * 1000}))
    result = execute(ctx, "web_fetch", focus="nonexistent-api")
    assert result.data["research"]["selection"] == "no_focus_match"
    assert result.output_truncated


def test_research_provenance_and_event_use_the_same_observation(ctx):
    result = execute(ctx, target_version="2.0")
    state, events = {"id": "research-test"}, []
    recorder = ToolObservationRecorder(
        session=state,
        root=ctx.root,
        state_root="",
        emit=lambda kind, payload: events.append((kind, payload)),
        on_changed_paths=lambda *args: None,
        on_retrieval=lambda *args: None,
    )
    recorded = recorder.record(
        CanonicalToolCall.create(
            "web_search", {"query": "migration", "purpose": "Resolve an API gap"}
        ),
        result,
        duration_ms=12,
        metadata=result.metadata,
        source_version="",
        idempotency_key="research-test",
    )
    provenance = recorded.stored.provenance
    assert provenance["trust"] == "untrusted_external" and provenance["verification"] is False
    assert provenance["web_research"]["target_version"] == "2.0"
    event = next(payload for kind, payload in events if kind == "web_research_observed")
    assert event["observation_id"] == recorded.stored.observation_id
    assert event["web_research"] == provenance["web_research"]
    store = ObservationStore(state, root=ctx.root)
    try:
        assert json.loads(store.expand(recorded.stored.observation_id))["provider_response"][
            "results"
        ]
    finally:
        store.close()


def test_summary_limit_configuration(tmp_path):
    policy = load_runtime_policy(
        env={"FIXLOOP_WEB_SUMMARY_CHARS": "300"}, user_config=str(tmp_path / "missing")
    )
    assert policy.web.summary_chars == 300
    assert policy.snapshot()["provenance"]["web.summary_chars"] == "environment"
    with pytest.raises(ValueError):
        AgentConfig(web={"summary_chars": 3000})


def test_patcher_requires_purpose_while_generic_schema_stays_role_neutral(ctx):
    from agent_runtime.tool_schema import validate_tool_arguments
    from src.prompts.loader import load_system_prompt
    from src.tools.composite import build_repair_canonical_tools

    tools = build_repair_canonical_tools(ctx)
    _, errors = validate_tool_arguments(tools["web_search"]["schema"], {"query": "migration"})
    assert errors and "purpose" not in WEB_SCHEMAS["web_search"]["required"]
    prompt = load_system_prompt("patcher_web")
    assert "已有官方资料 URL 时优先 web_fetch" in prompt
    assert "target_version" in prompt and "本地测试" in prompt


def test_external_policy_is_only_injected_when_enabled(ctx):
    from agent_runtime.providers.clients import FakeModelClient
    from agent_runtime.workspace import WorkspaceContext
    from src.agents.factory import create_patcher

    workspace = WorkspaceContext(cwd=ctx.root, repo_root=ctx.root)
    for enabled in (False, True):
        agent = create_patcher(
            FakeModelClient([]),
            workspace,
            cwd=ctx.root,
            runtime_policy=AgentConfig(provider="fake", web={"enabled": enabled}),
        )
        assert ("已有官方资料 URL 时优先 web_fetch" in agent.prefix.role_text) == enabled


@pytest.mark.parametrize("phase,action_required", [("converge", False), ("explore", True)])
def test_research_blocked_during_convergence_even_for_text_calls(phase, action_required):
    guard = StepGuard()
    guard.reset()
    if phase == "converge":
        guard.enter_convergence("test")
    events = []
    context = SimpleNamespace(
        agent=SimpleNamespace(),
        guard=guard,
        state=ToolStepState(action_required=action_required),
        emit=lambda *args: events.append(args),
    )
    result = RepairLoopPolicy().preflight(
        context, "web_search", {"query": "migration", "purpose": "gap"}, step=1
    )
    assert result.error_code == "convergence_required"
    assert events[0][0] == "web_research_blocked"


def test_network_reads_consume_existing_exploration_window():
    guard = StepGuard(reads_before_converge=2)
    guard.reset(localization_mode=True)
    for query in ("first", "second"):
        args = {"query": query}
        guard.evaluate(
            StepContext(
                tool_name="web_search",
                tool_args=args,
                progress_key=guard.read_progress_key("web_search", args),
            )
        )
    assert guard.phase == "converge"


def test_native_recovery_schema_cannot_reopen_external_research():
    context = SimpleNamespace(
        state=ToolStepState(recovery_allowed_tools={"read_file", "web_search", "web_fetch"})
    )
    assert RepairLoopPolicy().visible_tools(context, action_required=True) == {"read_file"}
