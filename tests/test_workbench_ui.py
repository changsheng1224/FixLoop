"""S5 browser sessions, static resource boundaries and restore status."""

import http.client
import json
import time
from http.cookies import SimpleCookie

import pytest

from tests.test_workbench_http import TOKEN, running_server


def raw(server, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    headers = dict(headers or {})
    if body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(body).encode("utf-8")
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def session(server):
    status, headers, body = raw(
        server, "POST", "/api/session", {}, {"Authorization": "Bearer " + TOKEN}
    )
    assert status == 200 and json.loads(body)["authenticated"]
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    assert "Path=/api" in headers["Set-Cookie"]
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    return {"Cookie": cookie, "Origin": f"http://127.0.0.1:{server.server_port}"}


@pytest.mark.parametrize(
    "path,mime",
    [
        ("/", "text/html"),
        ("/assets/app.js", "text/javascript"),
        ("/assets/api.js", "text/javascript"),
        ("/assets/render.js", "text/javascript"),
        ("/assets/style.css", "text/css"),
    ],
)
def test_public_shell_has_no_credentials_or_data_and_strict_csp(tmp_path, path, mime):
    with running_server(tmp_path) as server:
        status, headers, body = raw(server, "GET", path)
        assert status == 200 and headers["Content-Type"].startswith(mime)
        assert TOKEN.encode() not in body and b"Set-Cookie" not in body
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert "unsafe-inline" not in headers["Content-Security-Policy"]
        assert raw(server, "GET", "/api/conversations")[0] == 401


@pytest.mark.parametrize(
    "path,status",
    [
        ("/assets/../service.py", 404),
        ("/assets/%2e%2e/service.py", 404),
        ("/assets/missing.js", 404),
        ("/api-token", 404),
        ("/.env", 404),
        ("/?token=secret", 400),
        ("/assets/app.js?anything=1", 400),
    ],
)
def test_only_fixed_static_resources_are_public(tmp_path, path, status):
    with running_server(tmp_path) as server:
        assert raw(server, "GET", path)[0] == status


@pytest.mark.parametrize("headers", [{"Host": "evil.example"}, {"Origin": "https://evil.example"}])
def test_static_shell_still_enforces_host_and_origin(tmp_path, headers):
    with running_server(tmp_path) as server:
        assert raw(server, "GET", "/", headers=headers)[0] == 403


def test_browser_session_cookie_write_origin_and_logout(tmp_path):
    with running_server(tmp_path) as server:
        first, second = session(server), session(server)
        assert raw(server, "GET", "/api/health", headers=first)[0] == 200
        status, _, body = raw(server, "POST", "/api/conversations", {"title": "Browser"}, first)
        assert status == 201
        cid = json.loads(body)["id"]
        assert raw(server, "GET", "/api/conversations/" + cid, headers=first)[0] == 200
        assert raw(server, "POST", "/api/conversations", {}, {"Cookie": first["Cookie"]})[0] == 403
        wrong_origin = {**first, "Origin": f"http://localhost:{server.server_port}"}
        assert raw(server, "POST", "/api/conversations", {}, wrong_origin)[0] == 403
        assert raw(server, "POST", "/api/session", {}, first)[0] == 401
        assert (
            raw(server, "GET", "/api/health", headers={**first, "Authorization": "Bearer wrong"})[0]
            == 401
        )
        status, headers, _ = raw(server, "DELETE", "/api/session", headers=first)
        assert status == 200 and "Max-Age=0" in headers["Set-Cookie"]
        assert raw(server, "GET", "/api/health", headers=first)[0] == 401
        assert raw(server, "GET", "/api/health", headers=second)[0] == 200
        assert (
            raw(server, "GET", "/api/health", headers={"Authorization": "Bearer " + TOKEN})[0]
            == 200
        )


def test_expired_malformed_and_restart_session_cannot_authenticate(tmp_path):
    with running_server(tmp_path) as server:
        headers = session(server)
        cookie = SimpleCookie(headers["Cookie"])
        value = cookie[server.cookie_name].value
        server.sessions[value] = time.monotonic() - 1
        assert raw(server, "GET", "/api/health", headers=headers)[0] == 401
        assert raw(server, "GET", "/api/health", headers={"Cookie": "broken; =cookie"})[0] == 401
        old_cookie = headers["Cookie"]
    with running_server(tmp_path) as server:
        assert raw(server, "GET", "/api/health", headers={"Cookie": old_cookie})[0] == 401


def test_browser_session_capacity_is_bounded(tmp_path):
    with running_server(tmp_path) as server:
        first = server.new_session()
        for _ in range(64):
            server.new_session()
        assert len(server.sessions) == 64 and first not in server.sessions


def test_restore_receipt_is_not_reported_as_manual_file_edit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "value.txt").write_text("base", encoding="utf-8")
    with running_server(tmp_path / "state") as server:
        service = server.service
        chat = service.create_conversation(project_path=str(repo))
        (repo / "value.txt").write_text("manual", encoding="utf-8")
        plan = service.preview_restore(chat.id, action="fork", node_id=None)
        assert plan["manual"]
        service.apply_restore(chat.id, plan["id"])
        assert not service.project_status(chat.id)["manual_changes"]
        again = service.preview_restore(chat.id, action="fork", node_id=None)
        assert not again["manual"]
