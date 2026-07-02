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
