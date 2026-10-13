"""Business defaults and one resolver for effective role execution limits."""

from agent_runtime.config_loader import load_runtime_policy

ROLE_DEFAULTS = {
    "patcher": {
        "approval": "auto",
        "max_steps": 24,
        "max_model_attempts": 76,
        "max_new_tokens": 8192,
        "prompt_budget": 8000,
        "hard_cap": 8000,
        "max_json_retries": 0,
        "budget": {
            "total_tokens": 100000,
            "max_tool_calls": 24,
            "max_write_calls": 8,
            "max_verify_calls": 2,
            "max_recovery_attempts": 2,
            "max_read_calls": 12,
            "max_shell_calls": 2,
        },
    },
    "explorer": {
        "approval": "auto",
        "max_steps": 3,
        "max_model_attempts": 3,
        "max_new_tokens": 1024,
        "prompt_budget": 24000,
        "hard_cap": 24000,
        "max_json_retries": 0,
        "budget": {"total_tokens": 24000, "max_llm_calls": 3, "max_tool_calls": 4},
    },
}


def resolve_role_policy(role, root, **overrides):
    if role not in ROLE_DEFAULTS:
        raise ValueError(f"unsupported repair role: {role}")
    config = load_runtime_policy(
        workspace_root=root, defaults=ROLE_DEFAULTS[role], cli_overrides=overrides
    )
    sources = config.snapshot()["provenance"]
    for key, source in sources.items():
        if source == "default":
            sources[key] = f"repair_default:{role}"
        elif source == "cli":
            sources[key] = "request"
    return config.set_provenance(sources)


def isolated_explorer_policy(limits, *, model_context_window=0, context_safety_margin=256):
    """Resolve the explicitly configured child limits without reading parent settings."""
    from copy import deepcopy

    from agent_runtime.config import AgentConfig

    values = deepcopy(ROLE_DEFAULTS["explorer"])
    values.update(
        max_steps=limits.model_turns,
        max_model_attempts=limits.model_turns,
        prompt_budget=limits.tokens,
        hard_cap=limits.tokens,
        model_context_window=model_context_window,
        context_safety_margin=context_safety_margin,
    )
    values["budget"].update(
        total_tokens=limits.tokens,
        max_llm_calls=limits.model_turns,
        max_tool_calls=limits.tool_calls,
        max_read_calls=limits.tool_calls,
    )
    policy = AgentConfig(**values)
    sources = {key: "repair.exploration" for key in policy.snapshot()["provenance"]}
    return policy.set_provenance(sources)
