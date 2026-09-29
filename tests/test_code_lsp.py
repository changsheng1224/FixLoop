"""Protocol, position, and degraded lookup contract tests."""

from __future__ import annotations

import sys

import pytest

from agent_runtime.code_exploration.lsp import (
    LspClient,
    LspError,
    from_lsp_character,
    path_to_uri,
    to_lsp_character,
    uri_to_path,
)
from agent_runtime.code_exploration.service import CodeExplorationService
from agent_runtime.config_loader import load_runtime_policy
from agent_runtime.tool_context import ToolContext

SERVER = r"""
import json, sys, time
def read():
    header = bytearray()
    while not header.endswith(b"\r\n\r\n"):
        c = sys.stdin.buffer.read(1)
        if not c: return None
        header.extend(c)
    size = int(header.split(b"Content-Length: ")[1].split(b"\r\n")[0])
    return json.loads(sys.stdin.buffer.read(size))
def send(obj):
    body = json.dumps(obj).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()
mutated = False
while (msg := read()) is not None:
    if msg.get("method") == "initialize":
        send({"jsonrpc":"2.0","id":msg["id"],"result":{"capabilities":{
            "definitionProvider":True,"referencesProvider":True}}})
    elif msg.get("method") == "textDocument/definition":
        mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
        if mode == "oversize":
            sys.stdout.buffer.write(b"Content-Length: 1048577\r\n\r\n")
            sys.stdout.buffer.flush()
            continue
        if mode == "wrong_id":
            send({"jsonrpc":"2.0","id":msg["id"] + 1,"result":[]})
            continue
        if mode == "slow":
            time.sleep(1)
            continue
        if mode == "external":
            send({"jsonrpc":"2.0","id":msg["id"],"result":[{
                "uri":sys.argv[2],"range":{"start":{"line":0,"character":0},
                "end":{"line":0,"character":6}}}]})
            continue
        if mode == "link":
            send({"jsonrpc":"2.0","id":msg["id"],"result":[{
                "targetUri":sys.argv[2],
                "targetRange":{"start":{"line":0,"character":0},
                               "end":{"line":0,"character":10}},
                "targetSelectionRange":{"start":{"line":0,"character":4},
                                        "end":{"line":0,"character":10}}}]})
            continue
        if mode == "mutate" and not mutated:
            with open(sys.argv[2], "a", encoding="utf-8") as target:
                target.write("\n# changed during lookup\n")
            mutated = True
            send({"jsonrpc":"2.0","id":msg["id"],"result":[]})
            continue
        send({"jsonrpc":"2.0","method":"window/logMessage","params":{"message":"hi"}})
        send({"jsonrpc":"2.0","id":999,"method":"workspace/configuration","params":{}})
        read()
        send({"jsonrpc":"2.0","id":msg["id"],"result":[]})
    elif msg.get("method") == "shutdown":
        send({"jsonrpc":"2.0","id":msg["id"],"result":None})
    elif msg.get("method") == "exit":
        break
"""


def test_utf16_and_uri(tmp_path):
    row = "a😀éb"
    assert to_lsp_character(row, 2, "utf-16") == 3
    assert from_lsp_character(row, 3, "utf-16") == 2
    with pytest.raises(LspError, match="splits"):
        from_lsp_character(row, 2, "utf-16")
    path = tmp_path / "a space-β.py"
    assert uri_to_path(path_to_uri(path)) == path.resolve()
    with pytest.raises(LspError):
        uri_to_path("https://example.com/private.py")


def test_fake_server_notification_and_server_request(tmp_path):
    server = tmp_path / "server.py"
    server.write_text(SERVER, encoding="utf-8")
    client = LspClient((sys.executable, str(server)), tmp_path)
    try:
        client.start()
        assert client.request("textDocument/definition", {}) == []
    finally:
        client.close()
    assert client.process is None


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("wrong_id", "response id"),
        ("oversize", "message too large"),
        ("slow", "timed out"),
    ],
)
def test_fake_server_protocol_errors(tmp_path, mode, reason):
    server = tmp_path / "server.py"
    server.write_text(SERVER, encoding="utf-8")
    client = LspClient((sys.executable, str(server), mode), tmp_path)
    try:
        client.start()
        with pytest.raises(LspError, match=reason):
            client.request("textDocument/definition", {}, timeout=0.15)
    finally:
        client.close(graceful=False)
    assert client.process is None


def test_request_cancel_kills_process(tmp_path):
    server = tmp_path / "server.py"
    server.write_text(SERVER, encoding="utf-8")
    client = LspClient((sys.executable, str(server), "slow"), tmp_path)
    client.start()
    with pytest.raises(LspError, match="cancelled"):
        client.request("textDocument/definition", {}, cancelled=lambda: True)
    assert client.process is None


def test_file_change_retries_once(tmp_path):
    source = tmp_path / "a.py"
    source.write_text("def target():\n    return 1\n", encoding="utf-8")
    server = tmp_path / "server.py"
    server.write_text(SERVER, encoding="utf-8")
    ctx = ToolContext(root=str(tmp_path), exploration_mode="lsp")
    service = CodeExplorationService(
        ctx, mode="lsp", server_argv=(sys.executable, str(server), "mutate", str(source))
    )
    try:
        response = service.lookup({"path": "a.py", "line": 1, "column": 5})
        facts = response.metadata["retrieval_result"]
        assert facts["degradation_reason"] is None, response.content
        assert service.client.open_versions[path_to_uri(source)][1] == 2
    finally:
        service.close()


def test_external_location_never_reads_source(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("target()\n", encoding="utf-8")
    secret = tmp_path / "outside.py"
    secret.write_text("private_token = 'do-not-show'\n", encoding="utf-8")
    server = root / "server.py"
    server.write_text(SERVER, encoding="utf-8")
    ctx = ToolContext(root=str(root), exploration_mode="lsp")
    service = CodeExplorationService(
        ctx, mode="lsp", server_argv=(sys.executable, str(server), "external", secret.as_uri())
    )
    try:
        response = service.lookup({"path": "a.py", "line": 1, "column": 1})
        facts = response.metadata["retrieval_result"]
        assert facts["hits"][0]["resolution"] == "external"
        assert facts["hits"][0]["path"] == ""
        assert "do-not-show" not in response.content
        assert str(secret) not in response.content
    finally:
        service.close()


def test_location_link_uses_selection_range(tmp_path):
    source = tmp_path / "a.py"
    source.write_text("target()\n", encoding="utf-8")
    target = tmp_path / "target.py"
    target.write_text("def target():\n    pass\n", encoding="utf-8")
    server = tmp_path / "server.py"
    server.write_text(SERVER, encoding="utf-8")
    ctx = ToolContext(root=str(tmp_path), exploration_mode="lsp")
    service = CodeExplorationService(
        ctx, mode="lsp", server_argv=(sys.executable, str(server), "link", target.as_uri())
    )
    try:
        response = service.lookup({"path": "a.py", "line": 1, "column": 1})
        hit = response.metadata["retrieval_result"]["hits"][0]
        assert hit["path"] == "target.py"
        assert hit["range"]["start"] == {"line": 1, "column": 5}
    finally:
        service.close()


def test_unavailable_server_returns_candidates(tmp_path):
    (tmp_path / "a.py").write_text("def target():\n    return 1\n", encoding="utf-8")
    ctx = ToolContext(root=str(tmp_path), exploration_mode="lsp")
    service = CodeExplorationService(ctx, mode="lsp", server_argv=())
    result = service.lookup({"path": "a.py", "line": 1, "column": 5})
    facts = result.metadata["retrieval_result"]
    assert facts["degradation_reason"]
    assert facts["hits"] and all(hit["resolution"] == "candidate" for hit in facts["hits"])
    assert service.client is None


def test_text_mode_disabled_without_process(tmp_path):
    ctx = ToolContext(root=str(tmp_path))
    service = CodeExplorationService(ctx, mode="text")
    result = service.lookup({"path": "anything.py", "line": 1, "column": 1})
    assert result.metadata["retrieval_result"]["degradation_reason"] == "disabled"
    assert service.client is None


def test_workspace_config_cannot_enable_lsp(tmp_path):
    config_dir = tmp_path / ".fixloop"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        '{"code_exploration": {"mode": "lsp", "server_argv": ["evil"]}}',
        encoding="utf-8",
    )
    config = load_runtime_policy(
        workspace_root=str(tmp_path), user_config=str(tmp_path / "missing.json"), env={}
    )
    assert config.code_exploration.mode == "text"
    assert config.code_exploration.server_argv is None


def test_user_config_can_select_trusted_argv(tmp_path):
    user_file = tmp_path / "user.json"
    user_file.write_text(
        '{"code_exploration": {"mode": "lsp", "server_argv": ["C:/tools/pylsp.exe"]}}',
        encoding="utf-8",
    )
    config = load_runtime_policy(user_config=str(user_file), env={})
    assert config.code_exploration.mode == "lsp"
    assert config.code_exploration.server_argv == ("C:/tools/pylsp.exe",)
