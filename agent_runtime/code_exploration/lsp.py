"""Small, bounded JSON-RPC stdio client for Python language servers."""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.parse import unquote, urlparse

MAX_MESSAGE_BYTES = 1024 * 1024
MAX_HEADER_BYTES = 8192


class LspError(Exception):
    """The server is unavailable or its protocol response is unusable."""


def to_lsp_character(line: str, codepoint_column: int, encoding: str) -> int:
    prefix = line[:codepoint_column]
    if encoding == "utf-8":
        return len(prefix.encode("utf-8"))
    if encoding == "utf-32":
        return len(prefix)
    return len(prefix.encode("utf-16-le")) // 2


def from_lsp_character(line: str, character: int, encoding: str) -> int:
    if character < 0:
        raise LspError("negative LSP character")
    for index in range(len(line) + 1):
        position = to_lsp_character(line, index, encoding)
        if position == character:
            return index
        if position > character:
            raise LspError("LSP character splits a Unicode scalar")
    raise LspError("LSP character exceeds line")


def path_to_uri(path: Path) -> str:
    return path.resolve().as_uri()


def uri_to_path(uri: str) -> Path:
    if re.search(r"%(?![0-9a-fA-F]{2})", uri):
        raise LspError("malformed percent encoding")
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise LspError("unsupported location URI")
    if parsed.query or parsed.fragment or not parsed.path:
        raise LspError("malformed location URI")
    decoded = unquote(parsed.path)
    if os.name == "nt":
        if len(decoded) < 3 or decoded[0] != "/" or decoded[2] != ":":
            raise LspError("malformed Windows file URI")
        decoded = decoded[1:]
    return Path(decoded)


class LspClient:
    def __init__(self, argv: tuple[str, ...], root: Path):
        if not argv or not Path(argv[0]).is_absolute() or not Path(argv[0]).is_file():
            raise LspError("trusted LSP executable is unavailable")
        self.argv = argv
        self.root = root
        self.process: subprocess.Popen | None = None
        self.messages: queue.Queue = queue.Queue(maxsize=64)
        self.next_id = 0
        self.encoding = "utf-16"
        self.capabilities: dict = {}
        self.open_versions: dict[str, tuple[str, int]] = {}
        self._cache_dir: tempfile.TemporaryDirectory | None = None

    def start(self, timeout: float = 8.0) -> None:
        if self.process is not None:
            return
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["PYTHONNOUSERSITE"] = "1"
        self._cache_dir = tempfile.TemporaryDirectory(prefix="fixloop-lsp-")
        env["LOCALAPPDATA"] = self._cache_dir.name
        try:
            self.process = subprocess.Popen(
                self.argv,
                cwd=self.root,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self._cache_dir.cleanup()
            self._cache_dir = None
            raise LspError("LSP process failed to start") from exc
        threading.Thread(target=self._reader, daemon=True).start()
        try:
            response = self.request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "rootUri": path_to_uri(self.root),
                    "capabilities": {
                        "general": {"positionEncodings": ["utf-16", "utf-8", "utf-32"]},
                        "textDocument": {"definition": {}, "references": {}},
                    },
                    "initializationOptions": {
                        "pylsp": {
                            "configurationSources": [],
                            "plugins": {
                                "autopep8": {"enabled": False},
                                "flake8": {"enabled": False},
                                "jedi_completion": {"enabled": False},
                                "jedi_definition": {"enabled": True},
                                "jedi_references": {"enabled": True},
                                "mccabe": {"enabled": False},
                                "pycodestyle": {"enabled": False},
                                "pydocstyle": {"enabled": False},
                                "pyflakes": {"enabled": False},
                                "pylint": {"enabled": False},
                                "rope_autoimport": {"enabled": False},
                                "yapf": {"enabled": False},
                            },
                        }
                    },
                },
                timeout,
            )
            if not isinstance(response, dict):
                raise LspError("invalid initialize result")
            self.capabilities = response.get("capabilities") or {}
            self.encoding = self.capabilities.get("positionEncoding") or "utf-16"
            if self.encoding not in {"utf-16", "utf-8", "utf-32"}:
                raise LspError("unsupported position encoding")
            self.notify("initialized", {})
        except Exception:
            self.close(graceful=False)
            raise

    def _reader(self) -> None:
        try:
            assert self.process is not None and self.process.stdout is not None
            stream = self.process.stdout
            while True:
                header = bytearray()
                while not header.endswith(b"\r\n\r\n"):
                    chunk = stream.read(1)
                    if not chunk:
                        raise LspError("LSP stream closed")
                    header.extend(chunk)
                    if len(header) > MAX_HEADER_BYTES:
                        raise LspError("LSP header too large")
                lengths = [
                    line.split(b":", 1)[1].strip()
                    for line in header[:-4].split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                ]
                if len(lengths) != 1 or not lengths[0].isdigit():
                    raise LspError("invalid Content-Length")
                length = int(lengths[0])
                if length > MAX_MESSAGE_BYTES:
                    raise LspError("LSP message too large")
                body = stream.read(length)
                if len(body) != length:
                    raise LspError("truncated LSP message")
                message = json.loads(body)
                if not isinstance(message, dict):
                    raise LspError("invalid JSON-RPC message")
                self.messages.put(message)
        except Exception as exc:
            self.messages.put(LspError(str(exc)))

    def _send(self, message: dict) -> None:
        if self.process is None or self.process.stdin is None:
            raise LspError("LSP server is closed")
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_MESSAGE_BYTES:
            raise LspError("outgoing LSP message too large")
        payload = f"Content-Length: {len(body)}\r\n\r\n".encode() + body
        done = threading.Event()
        failure: list[Exception] = []

        def write() -> None:
            try:
                self.process.stdin.write(payload)
                self.process.stdin.flush()
            except (OSError, ValueError, AttributeError) as exc:
                failure.append(exc)
            finally:
                done.set()

        threading.Thread(target=write, daemon=True).start()
        if not done.wait(3.0):
            self.close(graceful=False)
            raise LspError("LSP write timed out")
        if failure:
            raise LspError("LSP write failed") from failure[0]

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict, timeout: float = 3.0, cancelled=None) -> object:
        self.next_id += 1
        request_id = self.next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        import time

        deadline = time.monotonic() + timeout
        while True:
            if cancelled is not None and cancelled():
                self.close(graceful=False)
                raise LspError("cancelled")
            try:
                item = self.messages.get(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
            except queue.Empty:
                if time.monotonic() >= deadline:
                    self.close(graceful=False)
                    raise LspError("LSP request timed out") from None
                continue
            if isinstance(item, Exception):
                raise item
            if "method" in item:
                if "id" in item:
                    self._send(
                        {
                            "jsonrpc": "2.0",
                            "id": item["id"],
                            "error": {"code": -32601, "message": "Method not supported"},
                        }
                    )
                continue
            if item.get("id") != request_id:
                raise LspError("unexpected LSP response id")
            if "error" in item:
                raise LspError("LSP request failed")
            return item.get("result")

    def sync(self, path: Path, content: str, digest: str) -> None:
        uri = path_to_uri(path)
        old = self.open_versions.get(uri)
        if old is not None and old[0] == digest:
            return
        version = (old[1] + 1) if old else 1
        if old is None:
            self.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": "python",
                        "version": version,
                        "text": content,
                    }
                },
            )
        else:
            self.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": content}],
                },
            )
        self.open_versions[uri] = (digest, version)

    def close(self, graceful: bool = True) -> None:
        process = self.process
        if process is None:
            return
        if graceful and process.poll() is None:
            try:
                self.request("shutdown", {}, timeout=0.5)
                self.notify("exit", {})
                process.wait(timeout=0.5)
            except (LspError, subprocess.TimeoutExpired):
                pass
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        self.process = None
        self.open_versions.clear()
        if self._cache_dir is not None:
            self._cache_dir.cleanup()
            self._cache_dir = None
