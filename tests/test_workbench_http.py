"""Real loopback HTTP/SSE acceptance (no external network or provider calls)."""

import http.client
import json
import queue
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import pytest

from src.workbench.http import WorkbenchHTTPServer
from src.workbench.service import WorkbenchService
from tests.test_workbench_service import Echo, result

TOKEN = "test-workbench-token-for-http-only"


@contextmanager
def running_server(tmp_path, *, executor=None, project_executor=None):
    with WorkbenchService(
        tmp_path,
        executor=executor or Echo(),
        project_executor=project_executor,
        project_lock_dir=tmp_path.parent / "http-locks",
    ) as service:
        with WorkbenchHTTPServer(("127.0.0.1", 0), service, token=TOKEN) as server:
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
            )
            thread.start()
            try:
                yield server
            finally:
                server.shutdown()
                thread.join(3)


def request(server, method, path, body=None, *, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    auth = {"Authorization": "Bearer " + TOKEN}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        auth["Content-Type"] = "application/json"
    if headers:
        auth.update(headers)
    connection.request(method, path, data, headers=auth)
    response = connection.getresponse()
    payload = json.loads(response.read())
    status = response.status
    connection.close()
    return status, payload


def sse(server, path, *, last_id=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    headers = {"Authorization": "Bearer " + TOKEN}
    if last_id is not None:
        headers["Last-Event-ID"] = last_id
    connection.request("GET", path, headers=headers)
    return connection, connection.getresponse()


def frame(response):
    lines = []
    while True:
        line = response.readline().decode("utf-8").rstrip("\r\n")
        if not line:
            return "\n".join(lines)
        lines.append(line)


def test_http_chat_crud_edit_regenerate_and_switch(tmp_path):
    with running_server(tmp_path) as server:
        status, health = request(server, "GET", "/api/health")
        assert status == 200 and health["modes"] == ["chat", "project_chat", "repair"]
        status, chat = request(server, "POST", "/api/conversations", {"title": "日常"})
        assert status == 201 and chat["project_path"] is None
        cid = chat["id"]
        base = "/api/conversations/" + cid
        assert request(server, "PATCH", base, {"title": "写作"})[1]["title"] == "写作"
        status, turn = request(server, "POST", base + "/turns", {"text": "第一轮"})
        assert status == 202
        server.service.wait(cid, turn["run"]["id"])
        assert (
            request(server, "GET", base + "/messages")[1]["messages"][-1]["content"]
            == "回答：第一轮"
        )
        status, edited = request(
            server, "POST", base + "/nodes/" + turn["node"]["id"] + "/edit", {"text": "修改"}
        )
        assert status == 202
        server.service.wait(cid, edited["run"]["id"])
        status, repeated = request(
            server, "POST", base + "/nodes/" + edited["node"]["id"] + "/regenerate", {}
        )
        assert status == 202
        server.service.wait(cid, repeated["run"]["id"])
        assert request(server, "POST", base + "/view", {"node_id": None})[0] == 200
        assert (
            request(server, "POST", base + "/switch", {"branch_id": chat["active_branch_id"]})[0]
            == 200
        )
        assert request(server, "POST", base + "/fork", {"node_id": None})[0] == 201
        assert request(server, "GET", base + "/messages?node_id=")[1]["messages"] == []
        tree = request(server, "GET", base + "/tree")[1]
        assert len(tree["branches"]) == 4 and len(tree["runs"]) == 3
        assert len(request(server, "GET", "/api/conversations")[1]["conversations"]) == 1


def test_http_project_reexecution_requires_exact_reviewed_input_plan(tmp_path):
    from src.workbench.project import ProjectResult

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "v.py").write_text("base", encoding="utf-8")

    def editor(**kw):
        text = kw["messages"][-1]["content"]
        (kw["files"].root / "v.py").write_text(text, encoding="utf-8")
        kw["publish"]("project.progress", {"stage": "test"})
        kw["publish"]("project.report", {"verified": True})
        return ProjectResult("done", artifacts={"verified": True})

    with running_server(tmp_path / "state", project_executor=editor) as server:
        status, chat = request(server, "POST", "/api/conversations", {"project_path": str(repo)})
        assert status == 201
        cid = chat["id"]
        base = "/api/conversations/" + cid
        status, turn = request(server, "POST", base + "/turns", {"text": "one", "mode": "repair"})
        assert status == 202
        run = server.service.wait(cid, turn["run"]["id"])
        assert run.output_snapshot_id and run.artifacts["verified"]
        node_path = base + "/nodes/" + turn["node"]["id"]
        assert request(server, "POST", node_path + "/edit", {"text": "new"})[0] == 409
        (repo / "v.py").write_text("manual", encoding="utf-8")
        status, preview = request(
            server,
            "POST",
            base + "/restores/preview",
            {"action": "input", "node_id": turn["node"]["id"]},
        )
        assert status == 201
        status, edited = request(
            server, "POST", node_path + "/edit", {"text": "new", "plan_id": preview["id"]}
        )
        assert status == 202
        server.service.wait(cid, edited["run"]["id"])
        assert (repo / "v.py").read_text(encoding="utf-8") == "new"
        events = server.service.events(cid, edited["run"]["id"])
        assert {"project.progress", "project.report"} <= {e["kind"] for e in events}
        status, preview = request(
            server,
            "POST",
            base + "/restores/preview",
            {"action": "input", "node_id": edited["node"]["id"]},
        )
        status, repeated = request(
            server,
            "POST",
            base + "/nodes/" + edited["node"]["id"] + "/regenerate",
            {"plan_id": preview["id"]},
        )
        assert status == 202
        assert server.service.wait(cid, repeated["run"]["id"]).status == "succeeded"


def test_sse_delivers_before_completion_disconnect_does_not_cancel_and_replays(tmp_path):
    release = threading.Event()
    started = threading.Event()

    def executor(messages, token, chunk):
        chunk("你好\n")
        started.set()
        assert release.wait(3)
        chunk("世界")
        return result("你好\n世界")

    with running_server(tmp_path, executor=executor) as server:
        chat = server.service.create_conversation()
        turn = server.service.send(chat.id, "test")
        assert started.wait(2)
        path = f"/api/conversations/{chat.id}/runs/{turn.run.id}/events"
        connection, response = sse(server, path)
        assert response.status == 200
        assert "run.queued" in frame(response)
        assert "run.started" in frame(response)
        delta = frame(response)
        assert "answer.delta" in delta and "你好\\n" in delta
        assert server.service.store.get_run(chat.id, turn.run.id).status == "running"
        response.close()
        connection.close()
        release.set()
        assert server.service.wait(chat.id, turn.run.id).status == "succeeded"
        connection, response = sse(server, path, last_id=f"{turn.run.id}:3")
        replay = response.read().decode("utf-8")
        assert "世界" in replay and "run.finished" in replay and "你好" in replay
        # The initial delta was not replayed; the final run record contains the full answer.
        assert f"id: {turn.run.id}:3\n" not in replay
        connection.close()
        status, run = request(server, "GET", path.removesuffix("/events"))
        assert status == 200 and run["run"]["answer"] == "你好\n世界"


def test_http_stop_and_busy_conflict(tmp_path):
    started = threading.Event()

    def executor(messages, token, chunk):
        chunk("partial")
        started.set()
        while not token.is_cancelled:
            time.sleep(0.005)
        token.check()

    with running_server(tmp_path, executor=executor) as server:
        chat = server.service.create_conversation()
        base = "/api/conversations/" + chat.id
        turn = request(server, "POST", base + "/turns", {"text": "test"})[1]
        assert started.wait(2)
        assert request(server, "POST", base + "/turns", {"text": "busy"})[0] == 409
        rid = turn["run"]["id"]
        assert request(server, "POST", base + f"/runs/{rid}/stop", {})[0] == 202
        assert server.service.wait(chat.id, rid).status == "cancelled"


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"Authorization": "Bearer wrong"}, 401),
        ({"Host": "evil.example"}, 403),
        ({"Origin": "https://evil.example"}, 403),
    ],
)
def test_local_api_auth_host_and_origin(tmp_path, headers, expected):
    with running_server(tmp_path) as server:
        assert (
            request(server, "POST", "/api/conversations", {"title": "blocked"}, headers=headers)[0]
            == expected
        )
        assert server.service.store.list_conversations() == ()


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "x", "expected_revision": True},
        {"text": "x", "expected_revision": -1},
        {"text": "x", "unknown": "field"},
        {"text": 42},
        {},
    ],
)
def test_invalid_requests_leave_history_unchanged(tmp_path, payload):
    with running_server(tmp_path) as server:
        chat = server.service.create_conversation()
        assert request(server, "POST", f"/api/conversations/{chat.id}/turns", payload)[0] == 400
        assert server.service.store.get_tree(chat.id).nodes == ()


def test_scoped_run_access_and_replay_cursor_validation(tmp_path):
    with running_server(tmp_path) as server:
        first, other = server.service.create_conversation(), server.service.create_conversation()
        turn = server.service.send(first.id, "test")
        server.service.wait(first.id, turn.run.id)
        own = f"/api/conversations/{first.id}/runs/{turn.run.id}"
        foreign = f"/api/conversations/{other.id}/runs/{turn.run.id}"
        assert request(server, "GET", foreign)[0] == 404
        assert request(server, "POST", foreign + "/stop", {})[0] == 404
        assert request(server, "GET", own + "/events?after=999")[0] == 400
        assert request(server, "GET", own + "/events?after=-1")[0] == 400
        connection, response = sse(server, own + "/events", last_id="another-run:2")
        assert response.status == 400
        response.read()
        connection.close()
        assert (
            request(
                server,
                "POST",
                f"/api/conversations/{first.id}/turns",
                {"text": "x", "mode": "repair"},
            )[0]
            == 422
        )


def test_cli_help_and_loopback_binding(tmp_path):
    help_result = subprocess.run(
        [sys.executable, "-m", "src.workbench", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert help_result.returncode == 0 and "--provider" in help_result.stdout
    with WorkbenchService(tmp_path, executor=Echo()) as service:
        with pytest.raises(ValueError):
            WorkbenchHTTPServer(("0.0.0.0", 0), service, token=TOKEN)
        with pytest.raises(ValueError):
            WorkbenchHTTPServer(("127.0.0.1", 0), service, token="short")


def test_main_cli_help_exposes_workbench():
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "src.cli", "workbench", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--state-dir" in completed.stdout and "--provider" in completed.stdout


def test_cli_subprocess_serves_authenticated_fake_chat_and_sse(tmp_path):
    process = subprocess.Popen(
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "src.cli",
            "workbench",
            "--provider",
            "fake",
            "--port",
            "0",
            "--state-dir",
            str(tmp_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    lines = queue.Queue()

    def read_startup():
        for line in process.stdout:
            lines.put(line)

    reader = threading.Thread(target=read_startup, daemon=True)
    reader.start()
    try:
        startup = lines.get(timeout=10)
        assert startup.startswith("FixLoop workbench: http://127.0.0.1:"), startup
        port = int(startup.rstrip().rsplit(":", 1)[1])
        token = (tmp_path / "api-token").read_text(encoding="ascii")

        def api(method, path, body=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            headers = {"Authorization": "Bearer " + token}
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            connection.request(method, path, data, headers=headers)
            response = connection.getresponse()
            payload = json.loads(response.read())
            status = response.status
            connection.close()
            return status, payload

        assert api("GET", "/api/health")[0] == 200
        status, chat = api("POST", "/api/conversations", {"title": "Writing"})
        assert status == 201
        base = "/api/conversations/" + chat["id"]
        status, turn = api("POST", base + "/turns", {"text": "Draft a note"})
        assert status == 202
        path = base + "/runs/" + turn["run"]["id"]
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", path + "/events", headers={"Authorization": "Bearer " + token})
        response = connection.getresponse()
        assert response.status == 200
        stream = response.read().decode("utf-8")
        connection.close()
        assert "answer.delta" in stream and "run.snapshot" in stream
        status, finished = api("GET", path)
        assert status == 200 and finished["run"]["status"] == "succeeded"
        assert finished["run"]["answer"] and "<final>" not in finished["run"]["answer"]
        assert api("GET", base + "/messages")[1]["messages"][-1]["role"] == "assistant"
    finally:
        process.terminate()
        process.wait(timeout=10)
        reader.join(2)
        process.stdout.close()
        process.stderr.close()
