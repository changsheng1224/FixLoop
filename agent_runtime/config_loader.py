"""Configuration precedence and provenance loader.

Precedence (low to high): defaults → profile → user file → workspace file →
environment → explicit CLI overrides.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agent_runtime.config import AgentConfig

PROFILE_PRESETS: dict[str, dict[str, Any]] = {
    "prod": {},
    "dev": {
        "approval": "auto",
        "degradation": {"enabled": False},
    },
    "ci": {
        "approval": "never",
        "json_mode": False,
        "budget": {"max_write_calls": 0, "max_tool_calls": 0},
        "degradation": {"enabled": True, "skip_optional_context": True},
    },
}

_ENV_FIELDS = {
    "provider": str,
    "model": str,
    "profile": str,
    "max_steps": int,
    "max_new_tokens": int,
    "prompt_budget": int,
    "hard_cap": int,
    "approval": str,
    "temperature": float,
    "json_mode": lambda v: str(v).lower() in {"1", "true", "yes", "on"},
    "max_json_retries": int,
    "budget.max_turns": int,
    "budget.max_llm_calls": int,
    "budget.max_tool_calls": int,
    "budget.max_write_calls": int,
    "budget.max_verify_calls": int,
    "budget.max_recovery_attempts": int,
    "budget.soft_cost_limit_usd": float,
    "budget.hard_cost_limit_usd": float,
    "deadline.repair_s": int,
    "deadline.step_s": int,
    "deadline.tool_s": int,
    "deadline.retry_backoff_cap_s": float,
    "slo.ttft_p95_ms": int,
    "slo.model_p95_ms": int,
    "slo.repair_p95_ms": int,
}


def _merge(
    target: dict[str, Any],
    source: dict[str, Any],
    prefix: str,
    provenance: dict,
    source_name: str = "source",
) -> None:
    for key, value in source.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            nested = target.setdefault(key, {})
            if not isinstance(nested, dict):
                nested = {}
                target[key] = nested
            _merge(nested, value, path, provenance, source_name)
        else:
            target[key] = value
            provenance[path] = source_name


def _read_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _env_overrides(env: dict[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field, caster in _ENV_FIELDS.items():
        name = "FIXLOOP_" + field.upper().replace(".", "_")
        raw = env.get(name)
        if raw is None or raw == "":
            continue
        try:
            value = caster(raw)
        except (TypeError, ValueError):
            continue
        cursor = result
        parts = field.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    return result


def load_runtime_policy(
    *,
    workspace_root: str | None = None,
    cli_overrides: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    user_config: str | None = None,
) -> AgentConfig:
    """Load an ``AgentConfig`` with deterministic precedence and provenance."""
    values: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    actual_env = env or dict(os.environ)
    user_path = Path(user_config) if user_config else Path.home() / ".fixloop" / "config.json"
    user_values = _read_config(user_path) if user_path.is_file() else {}
    workspace_values: list[dict[str, Any]] = []
    if workspace_root:
        root = Path(workspace_root)
        for path in (root / ".fixloop" / "config.json", root / ".agent" / "config.json"):
            if path.is_file():
                workspace_values.append(_read_config(path))
    cli_profile = (cli_overrides or {}).get("profile")
    profile = str(
        actual_env.get("FIXLOOP_PROFILE")
        or cli_profile
        or (workspace_values[-1] if workspace_values else {}).get("profile")
        or user_values.get("profile")
        or "prod"
    ).lower()
    _merge(values, PROFILE_PRESETS.get(profile, {}), "", provenance, "profile")
    _merge(values, user_values, "", provenance, "user_file")
    for workspace_value in workspace_values:
        _merge(values, workspace_value, "", provenance, "workspace_file")
    _merge(values, _env_overrides(actual_env), "", provenance, "environment")
    if profile in PROFILE_PRESETS and "profile" not in values:
        values["profile"] = profile
        provenance["profile"] = "environment" if "FIXLOOP_PROFILE" in actual_env else "default"
    if cli_overrides:
        _merge(
            values,
            {key: value for key, value in cli_overrides.items() if value is not None},
            "",
            provenance,
            "cli",
        )
    _apply_legacy_aliases(values, provenance)
    return AgentConfig(**values).set_provenance(provenance)


def _apply_legacy_aliases(values: dict[str, Any], provenance: dict[str, str]) -> None:
    """Keep older scalar consumers aligned with namespaced policy values."""
    budget = values.get("budget") or {}
    deadline = values.get("deadline") or {}
    aliases = {
        "prompt_tokens": "prompt_budget",
        "max_llm_calls": "max_llm_calls_per_repair",
        "max_tool_calls": "max_tool_calls",
        "max_write_calls": "max_write_calls",
        "max_verify_calls": "max_verify_calls",
        "max_recovery_attempts": "max_recovery_attempts",
    }
    for source, target in aliases.items():
        if source in budget:
            values[target] = budget[source]
            provenance[target] = provenance.get(
                f"budget.{source}", provenance.get(target, "policy")
            )
    deadline_aliases = {
        "repair_s": "repair_wall_timeout_s",
        "step_s": "step_timeout_s",
        "tool_s": "tool_timeout_s",
    }
    for source, target in deadline_aliases.items():
        if source in deadline:
            values[target] = deadline[source]
            provenance[target] = provenance.get(
                f"deadline.{source}", provenance.get(target, "policy")
            )


__all__ = ["PROFILE_PRESETS", "load_runtime_policy"]
