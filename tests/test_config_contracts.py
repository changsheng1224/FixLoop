"""Configuration sources obey one precedence rule and reject invalid input."""

import json

import pytest

from agent_runtime.config_loader import load_runtime_policy


def test_empty_environment_means_no_environment_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXLOOP_PROVIDER", "unexpected-host-provider")
    policy = load_runtime_policy(env={}, user_config=str(tmp_path / "missing.json"))
    assert policy.provider == "deepseek"


def test_cli_profile_also_controls_profile_defaults(tmp_path):
    policy = load_runtime_policy(
        env={"FIXLOOP_PROFILE": "ci"},
        cli_overrides={"profile": "dev"},
        user_config=str(tmp_path / "missing.json"),
    )
    assert policy.profile == "dev"
    assert policy.approval == "auto"


def test_lower_priority_namespaced_value_does_not_override_cli_scalar(tmp_path):
    policy = load_runtime_policy(
        env={"FIXLOOP_BUDGET_MAX_TOOL_CALLS": "2"},
        cli_overrides={"max_tool_calls": 9},
        user_config=str(tmp_path / "missing.json"),
    )
    assert policy.max_tool_calls == 9
    assert policy.snapshot()["provenance"]["max_tool_calls"] == "cli"


@pytest.mark.parametrize("value", ["nonsense", "-1"])
def test_invalid_environment_values_fail_with_field_context(tmp_path, value):
    with pytest.raises(ValueError):
        load_runtime_policy(
            env={"FIXLOOP_MAX_STEPS": value}, user_config=str(tmp_path / "missing.json")
        )


@pytest.mark.parametrize("payload", ["{broken", json.dumps(["not an object"])])
def test_invalid_configuration_file_is_not_silently_ignored(tmp_path, payload):
    config = tmp_path / "config.json"
    config.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError):
        load_runtime_policy(env={}, user_config=str(config))


def test_explicit_zero_timeout_and_budget_override_lower_priority_limits(tmp_path):
    policy = load_runtime_policy(
        env={"FIXLOOP_BUDGET_MAX_TOOL_CALLS": "9", "FIXLOOP_DEADLINE_TOOL_S": "30"},
        cli_overrides={"max_tool_calls": 0, "tool_timeout_s": 0},
        user_config=str(tmp_path / "missing.json"),
    )
    assert policy.effective_budget()["tool_calls"] == 0
    assert policy.effective_deadline()["tool_s"] == 0
    assert policy.budget.max_tool_calls == 0
    assert policy.deadline.tool_s == 0


def test_invalid_environment_boolean_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="FIXLOOP_JSON_MODE"):
        load_runtime_policy(
            env={"FIXLOOP_JSON_MODE": "tru"},
            user_config=str(tmp_path / "missing.json"),
        )


def test_repair_configuration_precedence_provenance_and_validation(tmp_path):
    from src.repair.config import load_repair_config
    from tests.repair_support import build_repository

    user = tmp_path / "user.json"
    user.write_text(json.dumps({"repair": {"patcher_max_steps": 8}}), encoding="utf-8")
    root = build_repository(
        tmp_path / "repo",
        {
            ".fixloop/config.json": json.dumps({"repair": {"patcher_max_steps": 12}}),
        },
    )
    config = load_repair_config(
        workspace_root=str(root),
        user_config=str(user),
        env={"FIXLOOP_PATCHER_MAX_STEPS": "16", "DEEPSEEK_API_KEY": "secret"},
        cli_overrides={"patcher_max_steps": 20},
    )
    assert config.patcher_max_steps == 20
    snapshot = config.snapshot()
    assert snapshot["provenance"]["patcher_max_steps"] == "cli"
    assert snapshot["provenance"]["progress"] == "default"
    assert "secret" not in json.dumps(snapshot)
    assert (
        load_repair_config(
            workspace_root=str(root), user_config=str(user), env={}
        ).patcher_max_steps
        == 12
    )
    with pytest.raises(ValueError):
        load_repair_config(env={"FIXLOOP_PATCHER_MAX_STEPS": "bad"}, user_config=str(user))
    with pytest.raises(ValueError):
        load_repair_config(env={"FIXLOOP_PROGRESS_HEARTBEAT_S": "nan"}, user_config=str(user))
    with pytest.raises(ValueError):
        load_repair_config(env={}, user_config=str(user), cli_overrides={"unknown": True})


def test_role_factory_and_warm_budget_use_resolved_model(tmp_path, monkeypatch):
    from agent_runtime.providers.clients import FakeModelClient
    from src.repair_factory import wire_orchestrator

    monkeypatch.setenv("FIXLOOP_MODEL", "custom-model")
    monkeypatch.setenv("FIXLOOP_MAX_STEPS", "11")
    monkeypatch.setenv("FIXLOOP_APPROVAL", "never")
    orch = wire_orchestrator(FakeModelClient(["ok"]), str(tmp_path), skip_verify=True)
    policy = orch.patcher.config
    assert policy.max_steps == 11 and policy.approval == "never"
    assert policy.model == "custom-model"
    assert orch._budget_ctx.master.model == "custom-model"
    assert policy.snapshot()["provenance"]["max_steps"] == "environment"


def test_workspace_file_order_is_preserved_across_alias_spellings(tmp_path):
    from tests.repair_support import build_repository

    build_repository(
        tmp_path,
        {
            ".fixloop/config.json": json.dumps({"profile": "dev", "budget": {"max_tool_calls": 2}}),
            ".agent/config.json": json.dumps({"max_tool_calls": 7}),
        },
    )
    policy = load_runtime_policy(
        workspace_root=str(tmp_path),
        env={},
        user_config=str(tmp_path / "missing.json"),
    )
    assert policy.approval == "auto"
    assert policy.max_tool_calls == policy.budget.max_tool_calls == 7


def test_per_prompt_and_run_token_limits_are_independent(tmp_path):
    policy = load_runtime_policy(
        defaults={"prompt_budget": 4000},
        env={},
        user_config=str(tmp_path / "missing.json"),
    )
    assert policy.prompt_budget == 4000
    assert policy.effective_budget()["prompt_tokens"] == 100_000


def test_non_git_fixture_does_not_load_enclosing_repository_context(tmp_path):
    from agent_runtime.workspace import WorkspaceContext
    from tests.repair_support import build_repository

    root = build_repository(tmp_path / "repo", {"a.py": "value = 1\n"})
    workspace = WorkspaceContext.build(str(root))
    assert workspace.repo_root == str(root)
    assert not workspace.git_status
