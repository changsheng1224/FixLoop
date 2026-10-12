"""S6 test infrastructure: loopback protocol server and real workbench processes.

Only synthetic test prompts are retained. Nothing replaces the production executors,
model clients, tool dispatch, verification, snapshot store or HTTP application.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


def http(url, method="GET", body=None, *, token=None, expected=200, timeout=10):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        response = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read().decode("utf-8")
        assert response.status == expected, (response.status, raw, url)
        return json.loads(raw)


def eventually(check, timeout=45):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError("acceptance condition did not become true")
        time.sleep(0.05)


def tool(name, **args):
    return "<tool>" + json.dumps({"name": name, "args": args}) + "</tool>"


def repair_script(old, new, *, tail=None):
    return [
        {"text": '{"conclusion":"Update the requested initial value and verify it."}'},
        {"text": tool("read_file", path="v.py")},
        {
            "text": tool(
                "patch_file", path="v.py", old_text=f"value = {old}", new_text=f"value = {new}"
            )
        },
        tail or {"text": "<final>Updated the initial value.</final>"},
    ]


class ModelServer(ThreadingHTTPServer):
    """Small Ollama protocol peer; scripts live exclusively in test infrastructure."""

    daemon_threads = True

    def __init__(self, root):
        super().__init__(("127.0.0.1", 0), _ModelHandler)
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.guard = threading.Lock()
        self.scripts = deque()
        self.records = []
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}"

    def script(self, steps):
        with self.guard:
            assert not self.scripts, "unconsumed model responses from preceding scenario"
            self.scripts.extend(steps)

    def snapshot(self):
        with self.guard:
            return {"requests": list(self.records), "remaining": len(self.scripts)}

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop_event.set()
        self.shutdown()
        self.thread.join(3)
        self.server_close()


class _ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path == "/__test__/records":
            self.respond(200, self.server.snapshot())
        else:
            self.respond(404, {"error": "unknown test route"})

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/__test__/script":
            self.server.script(body["steps"])
            self.respond(200, {"ok": True})
            return
        if self.path not in {"/api/chat", "/api/generate"}:
            self.respond(404, {"error": "unexpected provider endpoint"})
            return
        with self.server.guard:
            step = self.server.scripts.popleft() if self.server.scripts else None
            record = {
                "sequence": len(self.server.records) + 1,
                "path": self.path,
                "body": body,
                "unexpected": step is None,
            }
            self.server.records.append(record)
            with (self.server.root / "provider.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(record, ensure_ascii=False) + "\n")
        if step is None:
            self.respond(400, {"error": "no scripted model response"})
            return
        if step.get("http_error"):
            self.respond(step["http_error"], {"error": "synthetic provider failure"})
            return
        try:
            if not body.get("stream"):
                self.server.stop_event.wait(step.get("delay_s", 0))
                self.respond(200, {"response": step["text"], "done": True})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            chunks = step.get(
                "chunks", [{"text": step.get("text", ""), "delay_s": step.get("delay_s", 0)}]
            )
            for chunk in chunks:
                if self.server.stop_event.wait(chunk.get("delay_s", 0)):
                    return
                value = chunk["text"]
                data = (
                    {"message": {"role": "assistant", "content": value}}
                    if self.path == "/api/chat"
                    else {"response": value}
                )
                self.wfile.write(
                    (json.dumps({**data, "done": False}, ensure_ascii=False) + "\n").encode("utf-8")
                )
                self.wfile.flush()
            if not step.get("disconnect"):
                self.wfile.write(
                    b'{"done":true,"done_reason":"stop","prompt_eval_count":10,"eval_count":5}\n'
                )
                self.wfile.flush()
        except (OSError, ConnectionError):
            pass  # A stop or killed service is an expected transport disconnect.


class WorkbenchProcess:
    """Separate production service, including real workers and OS ownership locks."""

    def __init__(self, root, provider, *, timeout=40, crash_restore=False, cli=False):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.provider = provider
        self.timeout = timeout
        self.crash_restore = crash_restore
        self.cli = cli
        self.child = None

    def start(self):
        self.log = (self.root / "service.log").open("a", encoding="utf-8")
        environment = dict(os.environ, PYTHONUTF8="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        if self.cli:
            command = [
                sys.executable,
                "-m",
                "src.cli",
                "workbench",
                "--state-dir",
                str(self.root / "state"),
                "--port",
                "0",
                "--provider",
                "ollama",
                "--model",
                "s6-local",
                "--base-url",
                self.provider,
                "--timeout",
                str(self.timeout),
            ]
        else:
            (self.root / "ready.json").unlink(missing_ok=True)
            command = [
                sys.executable,
                "-X",
                "utf8",
                str(ROOT / "tests/workbench_e2e_fixture.py"),
                str(self.root),
                "--project-lock-dir",
                str(self.root.parent / "shared-project-locks"),
                "--provider-url",
                self.provider,
                "--timeout",
                str(self.timeout),
            ]
            if self.crash_restore:
                command.append("--crash-restore")
        self.child = subprocess.Popen(
            command, cwd=ROOT, env=environment, stdout=self.log, stderr=subprocess.STDOUT
        )

        def ready():
            if self.child.poll() is not None:
                raise RuntimeError(
                    f"service exited {self.child.returncode}: {self.root / 'service.log'}"
                )
            if self.cli:
                text = (self.root / "service.log").read_text(encoding="utf-8")
                lines = [
                    line for line in text.splitlines() if line.startswith("FixLoop workbench: ")
                ]
                token = self.root / "state/api-token"
                if lines and token.exists():
                    return {
                        "url": lines[-1].split(": ", 1)[1],
                        "token": token.read_text(encoding="ascii"),
                    }
            else:
                path = self.root / "ready.json"
                if path.exists():
                    try:
                        return json.loads(path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        pass
            return None

        try:
            self.startup = eventually(ready, timeout=20)
            return self
        except BaseException:
            self.kill()
            raise

    def api(self, path, method="GET", body=None, expected=200):
        return http(
            self.startup["url"] + "/api" + path,
            method,
            body,
            token=self.startup["token"],
            expected=expected,
        )

    def create(self, title="S6", project=None):
        return self.api(
            "/conversations",
            "POST",
            {"title": title, **({"project_path": str(project)} if project else {})},
            201,
        )

    def turn(self, cid, text, mode="chat"):
        return self.api(f"/conversations/{cid}/turns", "POST", {"text": text, "mode": mode}, 202)

    def wait(self, cid, rid, timeout=45):
        def finished():
            run = self.api(f"/conversations/{cid}/runs/{rid}")["run"]
            return run if run["status"] in TERMINAL else None

        return eventually(finished, timeout)

    def kill(self):
        if self.child and self.child.poll() is None:
            self.child.kill()
            self.child.wait(10)
        if hasattr(self, "log"):
            self.log.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.kill()
