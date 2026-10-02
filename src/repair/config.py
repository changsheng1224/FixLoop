"""Layer 2 configuration using the runtime's shared source resolver."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from agent_runtime.config_loader import load_config_values, parse_bool
from agent_runtime.policy import config_snapshot


class RepairConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    sandbox_policy: Literal["required", "preferred", "disabled"] = "preferred"
    patcher_max_steps: int = Field(default=24, ge=1, le=50)
    patcher_compact: bool = True
    progress: bool = True
    progress_stdout: bool = True
    progress_jsonl: str = ""
    progress_heartbeat: bool = True
    progress_heartbeat_s: float = Field(default=60, ge=5, allow_inf_nan=False)
    progress_heartbeat_text: bool = False
    _provenance: dict[str, str] = PrivateAttr(default_factory=dict)

    def snapshot(self) -> dict:
        return config_snapshot(self, self._provenance)


def _progress_bool(value: str) -> bool:
    return False if value.strip().lower() == "quiet" else parse_bool(value)


def _stdout_bool(value: str) -> bool:
    return False if value.strip().lower() == "stderr" else parse_bool(value)


_ENV_FIELDS = {
    "sandbox_policy": str,
    "patcher_max_steps": int,
    "patcher_compact": parse_bool,
    "progress": _progress_bool,
    "progress_stdout": _stdout_bool,
    "progress_jsonl": str,
    "progress_heartbeat": parse_bool,
    "progress_heartbeat_s": float,
    "progress_heartbeat_text": parse_bool,
}


def load_repair_config(
    *,
    workspace_root: str | None = None,
    cli_overrides: dict | None = None,
    env: dict[str, str] | None = None,
    user_config: str | None = None,
) -> RepairConfig:
    values, sources = load_config_values(
        workspace_root=workspace_root,
        cli_overrides={"repair": cli_overrides} if cli_overrides else None,
        env=env,
        user_config=user_config,
        env_fields={
            f"repair.{field}": (f"FIXLOOP_{field.upper()}", caster)
            for field, caster in _ENV_FIELDS.items()
        },
    )
    config = RepairConfig.model_validate(values.get("repair", {}))
    config._provenance = {
        key.removeprefix("repair."): source
        for key, source in sources.items()
        if key.startswith("repair.")
    }
    return config
