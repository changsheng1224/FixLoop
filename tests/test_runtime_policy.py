import json

import pytest

from agent_runtime.budget_manager import BudgetManager
from agent_runtime.config_loader import load_runtime_policy
from agent_runtime.policy import RuntimePolicy


def test_config_precedence_and_provenance(tmp_path, monkeypatch):
    user_file = tmp_path / "user.json"
    user_file.write_text(json.dumps({"provider": "user", "max_steps": 4}), encoding="utf-8")
    workspace = tmp_path / "workspace"
    (workspace / ".fixloop").mkdir(parents=True)
    (workspace / ".fixloop" / "config.json").write_text(
        json.dumps({"provider": "workspace", "budget": {"max_tool_calls": 2}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("FIXLOOP_PROVIDER", "environment")
    config = load_runtime_policy(workspace_root=str(workspace), user_config=str(user_file))
    assert config.provider == "environment"
    assert config.max_steps == 4
    assert config.max_tool_calls == 2
    assert config.snapshot()["provenance"]["provider"] == "environment"
    assert config.snapshot()["provenance"]["budget.max_tool_calls"] == "workspace_file"


def test_policy_cross_field_validation():
    with pytest.raises(ValueError, match="hard_cap"):
        RuntimePolicy(prompt_budget=1000, hard_cap=2000)
    with pytest.raises(ValueError, match="soft_cost"):
        RuntimePolicy(budget={"soft_cost_limit_usd": 2, "hard_cost_limit_usd": 1})


def test_budget_manager_reserve_and_restore():
    manager = BudgetManager({"tool_calls": 1, "prompt_tokens": 10})
    assert manager.reserve("tool_calls").allowed
    assert not manager.reserve("tool_calls").allowed
    assert manager.reserve("prompt_tokens", 10).allowed
    snapshot = manager.snapshot()
    restored = BudgetManager()
    restored.restore(snapshot)
    assert restored.remaining("tool_calls") == 0
    assert restored.summary()["tool_calls"]["rejected"] == 1
