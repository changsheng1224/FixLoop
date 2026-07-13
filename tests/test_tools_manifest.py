""".agent/tools.yaml manifest 加载+校验单测（V1.4-Bonus10b）。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.middleware import REPAIR_PERMISSION_TABLE
from src.tools.manifest import load_tools_manifest, merge_permission_table


# ---------------------------------------------------------------------------
# load_tools_manifest
# ---------------------------------------------------------------------------


class TestLoadManifest:
    def test_no_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = load_tools_manifest(tmp)
            assert result == {}

    def test_empty_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".agent"
            agent_dir.mkdir()
            (agent_dir / "tools.yaml").write_text("")
            result = load_tools_manifest(tmp)
            assert result == {}

    def test_loads_valid_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".agent"
            agent_dir.mkdir()
            (agent_dir / "tools.yaml").write_text(
                "tools:\n"
                "  write_file: [patcher, localizer]\n"
                "  search: ['*']\n"
            )
            result = load_tools_manifest(tmp)
            assert result["write_file"] == {"patcher", "localizer"}
            assert result["search"] == {"*"}

    def test_unknown_tool_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".agent"
            agent_dir.mkdir()
            (agent_dir / "tools.yaml").write_text(
                "tools:\n"
                "  read_file: [patcher]\n"
                "  nonexistent_tool: [localizer]\n"
            )
            result = load_tools_manifest(tmp)
            assert "read_file" in result
            assert "nonexistent_tool" not in result

    def test_wildcard_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".agent"
            agent_dir.mkdir()
            (agent_dir / "tools.yaml").write_text(
                "tools:\n  grep: '*'\n"
            )
            result = load_tools_manifest(tmp)
            assert result["grep"] == {"*"}


# ---------------------------------------------------------------------------
# merge_permission_table
# ---------------------------------------------------------------------------


class TestMergePermissionTable:
    def test_manifest_overrides_builtin(self):
        manifest = {"write_file": {"patcher", "localizer"}}
        merged = merge_permission_table(dict(REPAIR_PERMISSION_TABLE), manifest)
        assert merged["write_file"] == {"patcher", "localizer"}

    def test_manifest_adds_new_tool(self):
        manifest = {"new_tool": {"localizer"}}
        merged = merge_permission_table(dict(REPAIR_PERMISSION_TABLE), manifest)
        assert merged["new_tool"] == {"localizer"}

    def test_builtin_preserved_when_not_in_manifest(self):
        manifest = {}
        merged = merge_permission_table(dict(REPAIR_PERMISSION_TABLE), manifest)
        assert merged["read_file"] == {"*"}  # unchanged

    def test_empty_manifest_no_change(self):
        merged = merge_permission_table(dict(REPAIR_PERMISSION_TABLE), {})
        assert merged == REPAIR_PERMISSION_TABLE


# ---------------------------------------------------------------------------
# 集成：build_repair_gateway + manifest
# ---------------------------------------------------------------------------


class TestGatewayWithManifest:
    def test_default_gateway_without_manifest(self):
        from src.middleware import build_repair_gateway

        gw = build_repair_gateway()
        # 默认行为：patcher 可写文件
        assert gw.can_call("patcher", "write_file")
        assert not gw.can_call("localizer", "write_file")

    def test_gateway_with_manifest(self):
        from src.middleware import build_repair_gateway

        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".agent"
            agent_dir.mkdir()
            (agent_dir / "tools.yaml").write_text(
                "tools:\n  write_file: [patcher, localizer]\n"
            )
            gw = build_repair_gateway(tmp)
            # manifest 允许 localizer 写文件
            assert gw.can_call("localizer", "write_file")
            assert gw.can_call("patcher", "write_file")
