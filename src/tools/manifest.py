""".agent/tools.yaml manifest 加载与校验（V1.4-Bonus10b）。

用户可在 repo 根目录 ``.agent/tools.yaml`` 中自定义 Agent 工具权限。
加载时校验工具名合法性，与内置 REPAIR_PERMISSION_TABLE 合并。

格式::

    tools:
      write_file: [patcher, localizer]
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


def load_tools_manifest(repo_root: str | Path) -> dict[str, set[str]]:
    """从 ``.agent/tools.yaml`` 加载用户自定义权限表。

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


def merge_permission_table(
    builtin: dict[str, set[str]], manifest: dict[str, set[str]]
) -> dict[str, set[str]]:
    """合并内置权限表与用户 manifest。用户设置覆盖内置默认。

    Args:
        builtin: 内置权限表（REPAIR_PERMISSION_TABLE）。
        manifest: 用户 manifest（load_tools_manifest 返回值）。

    Returns:
        合并后的权限表。
    """
    merged = dict(builtin)
    for tool_name, agents in manifest.items():
        merged[tool_name] = agents  # 用户覆盖
    return merged
