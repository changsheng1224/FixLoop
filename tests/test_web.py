"""External research must stay bounded, cancellable and outside source/test authority."""

import io
import json
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from agent_runtime import web
from agent_runtime.budget_manager import BudgetManager
from agent_runtime.cancellation import CancellationToken
from agent_runtime.config import AgentConfig, WebConfig
from agent_runtime.config_loader import load_runtime_policy
from agent_runtime.context_runtime import ObservationStore
from agent_runtime.evidence_extractors import extract_evidence
from agent_runtime.observation_recording import ToolObservationRecorder
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.repair_runtime import CanonicalToolCall
from agent_runtime.runtime import Agent
from agent_runtime.tool_context import ToolContext
from agent_runtime.workspace import WorkspaceContext
from src.middleware import build_repair_gateway
from src.tools.composite import build_repair_canonical_tools, is_repair_canonical_registry

REAL_THROTTLE = web._throttle


def search_result(content=""):
    return {
        "results": [
            {
                "title": "Official docs",
                "url": "https://docs.example.com/api",
                "snippet": "snippet",
                "summary": "summary",
                "content": content,
                "date": None,
                "authority": 3,
                "rerank_score": 2.1,
            }
        ]
    }


class Client:
    def __init__(self, *responses):
        self.responses = list(responses or [(200, {}, search_result())])
        self.calls = []

    def post(self, operation, payload, api_key, timeout_s):
        self.calls.append((operation, payload, api_key, timeout_s))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


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


def call(ctx, **args):
    return web.execute_web_tool(ctx, "web_search", {"query": "python site:docs.python.org", **args})


def used(ctx, resource):
    return ctx.runtime_budget.summary()[resource]["used"]


def test_search_defaults_metadata_and_empty_results(ctx):
    result = call(ctx)
    assert result.ok and result.data["results"][0]["authority"] == 3
    assert ctx.web_client.calls[0][1]["count"] == 3
    assert ctx.web_client.calls[0][1]["need_content"] is False
    assert "untrusted_external" == result.metadata["trust"]
    assert "unit-test-secret" not in repr(result)
    assert used(ctx, "web_requests") == 1
    assert used(ctx, "hard_cost_limit_usd") == web.REQUEST_COST_USD
    ctx.web_client = Client((200, {}, {"results": []}))
    empty = call(ctx)
    assert empty.ok and empty.metadata["estimated_cost_usd"] == 0
    assert used(ctx, "web_requests") == 2
    assert used(ctx, "hard_cost_limit_usd") == web.REQUEST_COST_USD


def test_fetch_returns_markdown(ctx):
    ctx.web_client = Client((200, {}, {"title": "Docs", "content": "# API\ntext"}))
    result = web.execute_web_tool(ctx, "web_fetch", {"url": "https://example.com/docs"})
    assert result.ok and result.data["content"] == "# API\ntext"
    assert ctx.web_client.calls[0][0] == "fetch"


@pytest.mark.parametrize(
    "args",
    [
        {"query": " "},
        {"query": "x" * 129},
        {"query": 1},
        {"query": "x", "count": 51},
        {"query": "x", "count": True},
        {"query": "x", "need_content": "true"},
        {"query": "x", "api_key": "secret"},
        {"query": "x", "base_url": "https://evil.test"},
    ],
)
def test_invalid_search_never_dispatches(ctx, args):
    result = web.execute_web_tool(ctx, "web_search", args)
    assert result.error_code == "invalid_arguments" and not ctx.web_client.calls


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://u:p@example.com",
        "http://localhost/a",
        "http://127.0.0.1",
        "http://[::1]/",
        "http://10.0.0.2",
        "https://service.internal/x",
        "https://example.com:99999",
        "https://example.com/\nsecret",
        "https://example.com\\@127.0.0.1/",
        "https://" + "x" * 8192,
    ],
)
def test_invalid_or_private_fetch_never_dispatches(ctx, url):
    result = web.execute_web_tool(ctx, "web_fetch", {"url": url})
    assert result.error_code == "invalid_arguments" and not ctx.web_client.calls


def test_disabled_and_missing_key_never_dispatch(ctx, monkeypatch):
    ctx.web_config.enabled = False
    assert call(ctx).error_code == "policy_denied"
    ctx.web_config.enabled = True
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert call(ctx).error_code == "web_auth_missing"
    assert not ctx.web_client.calls


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 413, 302])
def test_permanent_errors_are_sanitized_and_not_retried(ctx, status):
    ctx.web_client = Client((status, {}, {"error": "unit-test-secret"}))
    result = call(ctx)
    assert not result.ok and not result.retryable
    assert "unit-test-secret" not in repr(result)
    assert len(ctx.web_client.calls) == 1
    assert used(ctx, "hard_cost_limit_usd") == 0


@pytest.mark.parametrize("status", [408, 429, 500, 504])
def test_transient_errors_retry_with_exact_server_wait(ctx, monkeypatch, status):
    waits = []
    monkeypatch.setattr(web, "wait_retry", lambda delay, **kw: waits.append(delay))
    ctx.web_client = Client((status, {"Retry-After": "3"}, None), (200, {}, search_result()))
    result = call(ctx)
    assert result.ok and waits == [3]
    assert used(ctx, "web_requests") == 2
    assert result.metadata["web_requests"] == 2
    assert result.metadata["estimated_cost_usd"] == pytest.approx(web.REQUEST_COST_USD)


def test_server_wait_cannot_be_shortened_to_deadline(ctx):
    ctx.web_client = Client((429, {"Retry-After": "30"}, None))
    result = call(ctx)
    assert result.error_code == "deadline_exceeded"
    assert len(ctx.web_client.calls) == 1


def test_every_attempt_counts_and_cost_cap_prevents_dispatch(ctx, monkeypatch):
    ctx.runtime_budget = BudgetManager({"web_requests": 1})
    monkeypatch.setattr(web, "wait_retry", lambda *args, **kw: None)
    ctx.web_client = Client((503, {}, None))
    assert call(ctx).error_code == "budget_exceeded"
    assert len(ctx.web_client.calls) == 1
    ctx.runtime_budget = BudgetManager({"hard_cost_limit_usd": 0.0005})
    ctx.web_client = Client()
    assert call(ctx).error_code == "budget_exceeded"
    assert not ctx.web_client.calls


def test_soft_cost_limit_does_not_block_requests(ctx):
    ctx.runtime_budget = BudgetManager({"soft_cost_limit_usd": 0.0001})
    assert call(ctx).ok
    assert used(ctx, "soft_cost_limit_usd") == web.REQUEST_COST_USD


def test_precancel_and_expired_run_never_dispatch(ctx):
    ctx.cancel_token = CancellationToken()
    ctx.cancel_token.cancel()
    assert call(ctx).status == "cancelled"
    ctx.cancel_token = None
    ctx.deadline = SimpleNamespace(remaining_s=lambda: -1)
    assert call(ctx).error_code == "deadline_exceeded"
    assert not ctx.web_client.calls


def test_cancel_during_retry_wait_prevents_next_attempt(ctx, monkeypatch):
    ctx.cancel_token = CancellationToken()
    ctx.web_client = Client((429, {}, None))

    def cancel_wait(*args, **kwargs):
        ctx.cancel_token.cancel()
        ctx.cancel_token.check()

    monkeypatch.setattr(web, "wait_retry", cancel_wait)
    assert call(ctx).status == "cancelled"
    assert len(ctx.web_client.calls) == 1


def test_inflight_timeout_does_not_retry_or_settle_another_run(ctx):
    release, exited = threading.Event(), threading.Event()
    old_budget = ctx.runtime_budget
    ctx.web_config.timeout_s = 0.03

    def blocked(*args):
        try:
            release.wait(2)
            return 503, {}, None
        finally:
            exited.set()

    ctx.web_client.post = blocked
    try:
        result = call(ctx)
        assert result.error_code == "deadline_exceeded"
        assert result.metadata["estimated_cost_usd"] == web.REQUEST_COST_USD
        ctx.runtime_budget = BudgetManager({"web_requests": 8})
    finally:
        release.set()
        assert exited.wait(2)
    assert used(ctx, "web_requests") == 0
    assert old_budget.summary()["web_requests"]["used"] == 1


@pytest.mark.parametrize("data", [None, {}, {"results": [None]}, {"results": [{}]}])
def test_malformed_success_is_a_protocol_failure(ctx, data):
    ctx.web_client = Client((200, {}, data))
    result = call(ctx)
    assert result.error_code == "provider_protocol_error" and not result.retryable
    assert used(ctx, "hard_cost_limit_usd") == web.REQUEST_COST_USD


def test_transport_failure_is_sanitized_and_cost_remains_uncertain(ctx):
    ctx.web_client = Client(URLError("unit-test-secret"))
    result = call(ctx)
    assert result.error_code == "web_transport_error"
    assert "unit-test-secret" not in repr(result)
    assert used(ctx, "hard_cost_limit_usd") == web.REQUEST_COST_USD


def test_process_pacing_is_independent_for_search_and_fetch(monkeypatch):
    monkeypatch.setattr(web, "_next_request", {})
    REAL_THROTTLE("search", None, time.monotonic() + 1)
    REAL_THROTTLE("fetch", None, time.monotonic() + 1)
    with pytest.raises(TimeoutError):
        REAL_THROTTLE("search", None, time.monotonic() + 0.01)


def test_transport_fixed_endpoint_redirect_policy_and_bounded_response(monkeypatch):
    seen = []

    class Response(io.BytesIO):
        status = 200

    def open_request(request, timeout):
        seen.append(request)
        return Response(json.dumps(search_result()).encode())

    def opener(*handlers):
        assert isinstance(handlers[0], web._NoRedirect)
        return SimpleNamespace(open=open_request)

    monkeypatch.setattr(web, "build_opener", opener)
    status, _, data = web.DeepSeekWebClient().post("search", {"query": "中文"}, "secret", 1)
    assert status == 200 and data["results"]
    assert seen[0].full_url == "https://api.deepseek.com/v1/tools/search"
    assert seen[0].get_header("Authorization") == "Bearer secret"
    assert json.loads(seen[0].data) == {"query": "中文"}
    assert web._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test") is None
    monkeypatch.setattr(web, "MAX_RESPONSE_BYTES", 2)
    with pytest.raises(ValueError):
        web.DeepSeekWebClient().post("search", {}, "secret", 1)


def test_transport_does_not_return_error_body(monkeypatch):
    def fail(*args, **kwargs):
        raise HTTPError("url", 429, "secret", {"Retry-After": "2"}, io.BytesIO(b"secret"))

    monkeypatch.setattr(web, "build_opener", lambda *args: SimpleNamespace(open=fail))
    assert web.DeepSeekWebClient().post("fetch", {}, "secret", 1) == (
        429,
        {"Retry-After": "2"},
        None,
    )


def test_configuration_requires_host_opt_in_and_hides_credentials(tmp_path):
    (tmp_path / ".fixloop").mkdir()
    (tmp_path / ".fixloop/config.json").write_text(json.dumps({"web": {"enabled": True}}))
    missing = str(tmp_path / "missing.json")
    config = load_runtime_policy(workspace_root=str(tmp_path), env={}, user_config=missing)
    assert not config.web.enabled
    config = load_runtime_policy(
        workspace_root=str(tmp_path),
        user_config=missing,
        env={
            "FIXLOOP_WEB_ENABLED": "true",
            "FIXLOOP_BUDGET_MAX_WEB_REQUESTS": "3",
            "DEEPSEEK_API_KEY": "unit-test-secret",
        },
    )
    assert config.web.enabled and config.budget.max_web_requests == 3
    assert config.snapshot()["provenance"]["web.enabled"] == "environment"
    assert "unit-test-secret" not in json.dumps(config.snapshot())
    assert config.effective_budget()["web_requests"] == 3
    for payload in ({"web": {"timeout_s": 0}}, {"web": {"api_key": "secret"}}):
        with pytest.raises(ValueError):
            AgentConfig(**payload)


def test_registry_defaults_role_and_sandbox_classification(ctx):
    from agent_runtime.linux_sandbox.tool_policy import SandboxToolAccess, sandbox_tool_access

    ctx.web_config.enabled = False
    registry = build_repair_canonical_tools(ctx)
    assert is_repair_canonical_registry(registry)
    assert registry["web_search"]["lifecycle"] == "disabled"
    from agent_runtime.loop_protocols import build_native_tools

    assert "web_search" not in {t["name"] for t in build_native_tools(registry)}
    ctx.web_config.enabled = True
    registry = build_repair_canonical_tools(ctx)
    assert "web_search" in {t["name"] for t in build_native_tools(registry)}
    gateway = build_repair_gateway()
    gateway.bind_tools(registry)
    assert gateway.can_call("patcher", "web_search")
    assert not gateway.can_call("explorer", "web_search")
    assert not gateway.can_call("verifier", "web_fetch")
    assert registry["web_fetch"]["path_args"] == []
    assert sandbox_tool_access("web_search") == SandboxToolAccess.TRUSTED_NETWORK


def test_executor_binds_run_budget_and_dry_run_never_dispatches(ctx):
    agent = Agent(
        AgentConfig(
            provider="fake", approval="auto", web={"enabled": True}, budget={"max_web_requests": 1}
        ),
        FakeModelClient([]),
        WorkspaceContext(cwd=ctx.root, repo_root=ctx.root),
        tool_context=ctx,
    )
    first = agent.execute_tool("web_search", {"query": "first"})
    second = agent.execute_tool("web_search", {"query": "second"})
    assert first.ok and second.error_code == "budget_exceeded"
    assert len(ctx.web_client.calls) == 1
    agent.dry_run = True
    agent._get_tool_executor().dry_run = True
    assert agent.execute_tool("web_search", {"query": "dry"}).metadata["dry_run"]
    assert len(ctx.web_client.calls) == 1


def test_truncated_web_observation_expands_with_untrusted_provenance(ctx):
    ctx.web_config.preview_chars = 1000
    ctx.web_client = Client((200, {}, search_result("untrusted page\n" * 500)))
    result = call(ctx)
    assert result.output_truncated
    state = {"id": "web-test"}
    recorder = ToolObservationRecorder(
        session=state,
        root=ctx.root,
        state_root="",
        emit=lambda *args: None,
        on_changed_paths=lambda *args: None,
        on_retrieval=lambda *args: None,
    )
    recorded = recorder.record(
        CanonicalToolCall.create("web_search", {"query": "docs"}),
        result,
        duration_ms=1,
        metadata=result.metadata,
        source_version="",
        idempotency_key="web-test",
    )
    assert recorded.stored.provenance["trust"] == "untrusted_external"
    assert recorded.stored.provenance["verification"] is False
    ctx.observation_state = state
    from agent_runtime.tools import tool_expand_observation

    expanded = tool_expand_observation(ctx, {"observation_id": recorded.stored.observation_id})
    assert "untrusted_external" in expanded.content
    assert "UNTRUSTED EXTERNAL WEB CONTENT" in expanded.content
    assert not extract_evidence("web_search", {}, "FAILED tests/a.py::test_foo")
    store = ObservationStore(state, root=ctx.root)
    try:
        assert store.expand(recorded.stored.observation_id).count("untrusted page") == 500
    finally:
        store.close()


def test_sandbox_network_uses_explicit_permission_and_fixed_handler(ctx):
    from agent_runtime.linux_sandbox.permissions import PermissionScope, PermissionService
    from agent_runtime.linux_sandbox.workspace_routing import execute_canonical_tool

    ctx.sandbox_permissions = PermissionService(
        {"workspace": "w", "task": "t", "run": "r"}, PermissionScope()
    )
    ctx.sandbox_backend = SimpleNamespace(permissions=ctx.sandbox_permissions)
    ctx.sandbox_approval_policy = "never"
    denied = execute_canonical_tool(ctx, "web_search", {"query": "docs"})
    assert not denied.ok and not ctx.web_client.calls
    ctx.sandbox_permissions = PermissionService(
        {"workspace": "w", "task": "t", "run": "r2"},
        PermissionScope(capabilities=("trusted_network",)),
    )
    ctx.sandbox_backend.permissions = ctx.sandbox_permissions
    ctx.sandbox_trusted_handlers["web_search"] = lambda *args: pytest.fail("injected handler")
    result = execute_canonical_tool(ctx, "web_search", {"query": "docs"})
    assert result.ok and result.metadata["execution_tier"] == "trusted_host"
    assert result.receipt["permission"]["status"] == "allowed"
    assert len(ctx.web_client.calls) == 1
    ctx.web_config.enabled = False
    assert (
        execute_canonical_tool(ctx, "web_search", {"query": "docs"}).error_code == "policy_denied"
    )


def test_native_loop_records_web_observation_and_shared_budget(ctx):
    from agent_runtime.agent_loop import AgentLoop
    from agent_runtime.providers.clients import FakeNativeToolClient

    client = FakeNativeToolClient(
        [
            '<tool>{"name":"web_search","args":{"query":"public API docs"}}</tool>',
            "<final>Research complete.</final>",
        ]
    )
    agent = Agent(
        AgentConfig(provider="fake", approval="auto", web={"enabled": True}),
        client,
        WorkspaceContext(cwd=ctx.root, repo_root=ctx.root),
        tool_context=ctx,
    )
    assert "Research complete" in AgentLoop(agent).run("Look up the public API", skip_plan=True)
    assert len(ctx.web_client.calls) == 1, agent.session.get("tool_observations")
    assert agent.budget_manager.summary()["web_requests"]["used"] == 1
    store = ObservationStore(agent.session, root=ctx.root)
    try:
        observations = store.query(tool="web_search")
        assert len(observations) == 1
        assert observations[0].provenance["trust"] == "untrusted_external"
        assert "UNTRUSTED EXTERNAL WEB CONTENT" in store.expand(observations[0].observation_id)
    finally:
        store.close()


def test_service_factory_enables_tools_before_prefix_and_gateway_binding(ctx, monkeypatch):
    from src.repair.assembly import create_repair_service

    monkeypatch.setenv("FIXLOOP_WEB_ENABLED", "true")
    service = create_repair_service(FakeModelClient([]), ctx.root, skip_verify=True)
    patcher = service.session.patcher
    assert patcher.tools["web_search"]["lifecycle"] == "active"
    assert patcher.tool_gateway.can_call("patcher", "web_search")
    assert "web_search" in patcher.prefix.text
    patcher.tool_context.web_client = ctx.web_client
    assert patcher.execute_tool(
        "web_search",
        {"query": "official docs", "purpose": "Clarify the dependency migration requirement"},
    ).ok


def test_request_budget_survives_existing_snapshot_restore(ctx):
    ctx.runtime_budget = BudgetManager({"web_requests": 1})
    assert call(ctx).ok
    restored = BudgetManager({"web_requests": 1})
    restored.restore(ctx.runtime_budget.snapshot())
    ctx.runtime_budget = restored
    assert call(ctx).error_code == "budget_exceeded"
    assert len(ctx.web_client.calls) == 1


def test_sandbox_permission_revocation_blocks_retry(ctx, monkeypatch):
    from agent_runtime.linux_sandbox.permissions import PermissionScope, PermissionService
    from agent_runtime.linux_sandbox.workspace_routing import execute_canonical_tool

    service = PermissionService(
        {"workspace": "workspace", "task": "task", "run": "run"},
        PermissionScope(capabilities=("trusted_network",)),
    )
    ctx.sandbox_backend = SimpleNamespace(permissions=service)
    ctx.sandbox_permissions = service
    ctx.web_client = Client((429, {}, None))
    monkeypatch.setattr(
        web, "wait_retry", lambda *args, **kw: service.replace_scope(PermissionScope())
    )
    result = execute_canonical_tool(ctx, "web_search", {"query": "public docs"})
    assert result.error_code == "policy_denied"
    assert len(ctx.web_client.calls) == 1
    assert used(ctx, "web_requests") == 1


def test_sandbox_startup_cannot_dispatch_without_authority(ctx):
    ctx.sandbox_mode = True
    assert call(ctx).error_code == "policy_denied"
    assert not ctx.web_client.calls


def test_sandbox_executor_uses_host_broker_and_permission_receipt(ctx):
    from agent_runtime.linux_sandbox.permissions import PermissionScope, PermissionService

    service = PermissionService(
        {"workspace": "workspace", "task": "task", "run": "run"},
        PermissionScope(capabilities=("trusted_network",)),
    )
    ctx.sandbox_backend = SimpleNamespace(permissions=service)
    ctx.sandbox_permissions = service
    ctx.sandbox_mode = True
    agent = Agent(
        AgentConfig(provider="fake", approval="auto", web={"enabled": True}),
        FakeModelClient([]),
        WorkspaceContext(cwd=ctx.root, repo_root=ctx.root),
        tool_context=ctx,
    )
    agent.tools["web_search"]["run"] = lambda *args: pytest.fail("registry bypass")
    result = agent.execute_tool("web_search", {"query": "public docs"})
    assert result.ok and result.metadata["execution_tier"] == "trusted_host"
    assert result.metadata["sandbox_permission"]["status"] == "allowed"
    assert result.receipt["tool"] == "web_search"
    assert len(ctx.web_client.calls) == 1
