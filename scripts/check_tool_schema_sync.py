#!/usr/bin/env python3
"""CI/pre-commit: 检查 L1 (agent_runtime/tools.py) 与 L2 (src/tools/) 工具名一致性。

L2 canonical 工具名（src/tools/composite.py:REPAIR_CANONICAL_TOOL_NAMES）
应与 L2 注册表（src/tools/registry.py:build_repair_tools）保持一致；
同时 L1 基础工具名应是 L2 canonical 的子集。

用法:
    python scripts/check_tool_schema_sync.py
    exit 0 = 一致, exit 2 = 不一致
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT = Path(__file__).resolve().parent.parent
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))


def _l1_tool_names() -> set[str]:
    from agent_runtime.tool_context import ToolContext
    from agent_runtime.tools import build_tool_registry

    ctx = ToolContext(root=str(_PROJECT))
    return set(build_tool_registry(ctx).keys())


def _l2_registry_tool_names() -> set[str]:
    from agent_runtime.tool_context import ToolContext
    from src.tools.registry import build_repair_tools

    ctx = ToolContext(root=str(_PROJECT))
    return set(build_repair_tools(ctx).keys())


def main() -> int:
    errors = 0

    # 检查 1: build_repair_canonical_tools 实际产出 ↔ REPAIR_CANONICAL_TOOL_NAMES 声明一致
    from agent_runtime.tool_context import ToolContext
    from src.tools.composite import REPAIR_CANONICAL_TOOL_NAMES, build_repair_canonical_tools

    ctx = ToolContext(root=str(_PROJECT))
    built = set(build_repair_canonical_tools(ctx).keys())
    declared = set(REPAIR_CANONICAL_TOOL_NAMES)

    built_only = built - declared
    decl_only = declared - built
    if built_only:
        print(f"built 有但 REPAIR_CANONICAL_TOOL_NAMES 缺失 ({len(built_only)}): {sorted(built_only)}")
        errors += 1
    if decl_only:
        print(f"REPAIR_CANONICAL_TOOL_NAMES 有但 built 缺失 ({len(decl_only)}): {sorted(decl_only)}")
        errors += 1
    if not built_only and not decl_only:
        print(f"✓ canonical 声明与实际产出一致 ({len(built)} tools)")

    # 检查 2: L1 基础工具 ⊆ canonical
    l1 = _l1_tool_names()
    l1_extra = l1 - declared
    if l1_extra:
        print(f"L1 工具不在 canonical 中 ({len(l1_extra)}): {sorted(l1_extra)}")
        errors += 1
    else:
        print(f"✓ L1 ({len(l1)}) ⊆ canonical ({len(declared)})")

    # 检查 3: L2 registry ⊆ canonical
    l2_reg = _l2_registry_tool_names()
    l2_extra = l2_reg - declared
    if l2_extra:
        print(f"L2 registry 工具不在 canonical 中 ({len(l2_extra)}): {sorted(l2_extra)}")
        errors += 1
    else:
        print(f"✓ L2 registry ({len(l2_reg)}) ⊆ canonical ({len(declared)})")

    return 2 if errors > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
