""".agent/tools.yaml manifest 加载+校验单测（V1.4-Bonus10b）。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.tools.manifest import load_tool_role_overrides

# ---------------------------------------------------------------------------
# load_tools_manifest
# ---------------------------------------------------------------------------


class TestLoadManifest:
    def test_no_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = load_tool_role_overrides(tmp)
            assert result == {}

    def test_empty_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".agent"
            agent_dir.mkdir()
            (agent_dir / "tools.yaml").write_text("")
            result = load_tool_role_overrides(tmp)
            assert result == {}

    def test_loads_valid_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".agent"
            agent_dir.mkdir()
            (agent_dir / "tools.yaml").write_text(
                "tools:\n  write_file: [patcher]\n  search: ['*']\n"
            )
            result = load_tool_role_overrides(tmp)
            assert result["write_file"] == {"patcher"}
            assert result["search"] == {"*"}

    def test_unknown_tool_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".agent"
            agent_dir.mkdir()
            (agent_dir / "tools.yaml").write_text(
                "tools:\n  read_file: [patcher]\n  nonexistent_tool: [localizer]\n"
            )
            result = load_tool_role_overrides(tmp)
            assert "read_file" in result
            assert "nonexistent_tool" not in result

    def test_wildcard_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".agent"
            agent_dir.mkdir()
            (agent_dir / "tools.yaml").write_text("tools:\n  grep: '*'\n")
            result = load_tool_role_overrides(tmp)
            assert result["grep"] == {"*"}


# ---------------------------------------------------------------------------
# 集成：build_repair_gateway + manifest
# ---------------------------------------------------------------------------


class TestGatewayWithManifest:
    def test_default_gateway_without_manifest(self):
        from src.middleware import build_repair_gateway

        gw = build_repair_gateway()
        # 默认行为：patcher 可写文件
        assert gw.can_call("patcher", "write_file")
        assert not gw.can_call("verifier", "write_file")

    def test_gateway_with_manifest(self):
        from src.middleware import build_repair_gateway

        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".agent"
            agent_dir.mkdir()
            (agent_dir / "tools.yaml").write_text("tools:\n  write_file: [verifier]\n")
            gw = build_repair_gateway(tmp)
            assert gw.can_call("verifier", "write_file")
            assert not gw.can_call("patcher", "write_file")
