"""Load ToolSpec role overrides from ``.agent/tools.yaml``.

用户可在 repo 根目录 ``.agent/tools.yaml`` 中自定义 Agent 工具权限。
Only tools already present in the canonical registry may be overridden.

格式::

    tools:
      write_file: [patcher]
      run_shell: []
      search: ["*"]
"""

from __future__ import annotations

from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    yaml = None  # type: ignore[assignment]

from src.tools.composite import REPAIR_CANONICAL_TOOL_NAMES

MANIFEST_FILENAME = "tools.yaml"


def load_tool_role_overrides(repo_root: str | Path) -> dict[str, set[str]]:
    """Load canonical tool role overrides from the workspace manifest.

    Args:
        repo_root: 仓库根目录。

    Returns:
        {tool_name: {agent_name, ...}} 字典。文件不存在或为空时返回 {}。
    """
    if yaml is None:
        return {}

    manifest_path = Path(repo_root) / ".agent" / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return {}

    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}

    tools_section = raw.get("tools") if isinstance(raw, dict) else {}
    if not isinstance(tools_section, dict):
        return {}

    result: dict[str, set[str]] = {}
    for tool_name, agents in tools_section.items():
        tool_name = str(tool_name).strip()
        if not tool_name:
            continue
        if tool_name not in REPAIR_CANONICAL_TOOL_NAMES:
            import sys

            print(
                "[tools.yaml] ⚠ 未知工具 "
                f"'{tool_name}'，跳过（已知: {sorted(REPAIR_CANONICAL_TOOL_NAMES)}）",
                file=sys.stderr,
            )
            continue
        if isinstance(agents, list):
            result[tool_name] = {str(a).strip() for a in agents if str(a).strip()}
        elif agents == "*":
            result[tool_name] = {"*"}
    return result
