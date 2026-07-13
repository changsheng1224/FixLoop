"""ToolGateway 单测：权限检查、越权拒绝。"""

from src.middleware import ToolGateway


def _make_exec_fn(result="ok"):
    return lambda: result


class TestToolGateway:
    def test_allowed_call_passes(self):
        gw = ToolGateway({"read_file": {"localizer", "retriever"}})
        result = gw.dispatch("localizer", "read_file", _make_exec_fn("file content"))
        assert result == "file content"

    def test_denied_call_rejected(self):
        gw = ToolGateway({"write_file": {"patcher"}})
        result = gw.dispatch("localizer", "write_file", _make_exec_fn("ok"))
        assert "permission_denied" in result.metadata["tool_error_code"]

    def test_wildcard_allows_all(self):
        gw = ToolGateway({"search": {"*"}})
        r1 = gw.dispatch("localizer", "search", _make_exec_fn("ok"))
        r2 = gw.dispatch("retriever", "search", _make_exec_fn("ok"))
        assert r1 == "ok"
        assert r2 == "ok"

    def test_unknown_tool_denied(self):
        gw = ToolGateway({})
        assert gw.can_call("anyone", "ghost_tool") is False

    def test_grant_and_revoke(self):
        gw = ToolGateway({"read_file": {"localizer"}})
        gw.grant("retriever", "read_file")
        assert gw.can_call("retriever", "read_file") is True
        gw.revoke("retriever", "read_file")
        assert gw.can_call("retriever", "read_file") is False


# ---------------------------------------------------------------------------
# restrict_to（V1.4-Bonus13b）
# ---------------------------------------------------------------------------


class TestRestrictTo:
    def test_restrict_allows_only_listed_tools(self):
        gw = ToolGateway({"read_file": {"*"}, "write_file": {"patcher"}, "grep": {"*"}})
        gw.restrict_to("patcher", ["write_file", "read_file"])
        assert gw.can_call("patcher", "write_file")
        assert gw.can_call("patcher", "read_file")
        assert not gw.can_call("patcher", "grep")  # 不在白名单

    def test_restrict_adds_missing_tools(self):
        """restrict_to 将白名单工具 grant 给 agent。"""
        gw = ToolGateway({"read_file": {"localizer"}, "write_file": {"patcher"}})
        gw.restrict_to("localizer", ["write_file", "search"])
        assert gw.can_call("localizer", "write_file")
        assert gw.can_call("localizer", "search")

    def test_restrict_removes_non_listed_tools(self):
        gw = ToolGateway({"read_file": {"patcher"}, "write_file": {"patcher"}, "grep": {"patcher"}})
        gw.restrict_to("patcher", ["write_file"])
        assert gw.can_call("patcher", "write_file")
        assert not gw.can_call("patcher", "read_file")
        assert not gw.can_call("patcher", "grep")

    def test_restrict_empty_list_removes_all(self):
        gw = ToolGateway({"read_file": {"patcher"}, "write_file": {"patcher"}})
        gw.restrict_to("patcher", [])
        assert not gw.can_call("patcher", "read_file")
        assert not gw.can_call("patcher", "write_file")

    def test_restrict_only_affects_specified_agent(self):
        """restrict_to 仅影响指定 agent，不影响其他。"""
        gw = ToolGateway({"read_file": {"*"}, "write_file": {"patcher"}})
        gw.restrict_to("patcher", ["read_file"])
        # patcher 被限制
        assert not gw.can_call("patcher", "write_file")
        # localizer 不受影响
        assert gw.can_call("localizer", "read_file")
