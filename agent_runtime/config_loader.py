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


def parse_bool(value: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("expected a boolean")


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
    "json_mode": parse_bool,
    "max_json_retries": int,
    "loop_detect_threshold": int,
    "budget.prompt_tokens": int,
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
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid configuration file: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"configuration must be an object: {path}")
    return data


def _env_overrides(env: dict[str, str], fields: dict | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field, caster in (fields if fields is not None else _ENV_FIELDS).items():
        name = "FIXLOOP_" + field.upper().replace(".", "_")
        if isinstance(caster, tuple):
            name, caster = caster
        raw = env.get(name)
        if raw is None or raw == "":
            continue
        try:
            value = caster(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid environment configuration: {name}") from exc
        cursor = result
        parts = field.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    return result


def load_config_values(
    *,
    workspace_root: str | None = None,
    cli_overrides: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    user_config: str | None = None,
    defaults: dict[str, Any] | None = None,
    env_fields: dict | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Merge configuration layers with deterministic precedence and provenance."""
    values: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    actual_env = dict(os.environ) if env is None else dict(env)
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
        cli_profile
        or actual_env.get("FIXLOOP_PROFILE")
        or next(
            (item["profile"] for item in reversed(workspace_values) if item.get("profile")), None
        )
        or user_values.get("profile")
        or (defaults or {}).get("profile")
        or "prod"
    ).lower()

    def merge_layer(layer: dict, name: str) -> None:
        _merge(values, layer, "", provenance, name)

    merge_layer(defaults or {}, "default")
    merge_layer(PROFILE_PRESETS.get(profile, {}), "profile")
    merge_layer(user_values, "user_file")
    for workspace_value in workspace_values:
        # Semantic server activation is a host/user decision, not a repository setting.
        merge_layer(
            {key: value for key, value in workspace_value.items() if key != "code_exploration"},
            "workspace_file",
        )
    merge_layer(_env_overrides(actual_env, env_fields), "environment")
    if "profile" not in values:
        values["profile"] = profile
        provenance["profile"] = "default"
    if cli_overrides:
        merge_layer(
            {key: value for key, value in cli_overrides.items() if value is not None},
            "cli",
        )
    return values, provenance


def load_runtime_policy(
    *,
    workspace_root: str | None = None,
    cli_overrides: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    user_config: str | None = None,
    defaults: dict[str, Any] | None = None,
) -> AgentConfig:
    values, provenance = load_config_values(
        workspace_root=workspace_root,
        cli_overrides=cli_overrides,
        env=env,
        user_config=user_config,
        defaults=defaults,
    )
    values.pop("repair", None)
    provenance = {key: value for key, value in provenance.items() if not key.startswith("repair.")}
    config = AgentConfig(**values)
    return config.set_provenance(provenance)


__all__ = ["PROFILE_PRESETS", "load_config_values", "load_runtime_policy", "parse_bool"]
