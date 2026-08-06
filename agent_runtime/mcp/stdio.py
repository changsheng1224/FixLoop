"""MCP stdio transport：子进程 + newline-delimited JSON-RPC 2.0。"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Any

from agent_runtime.mcp.errors import (
    McpSchemaError,
    McpTimeoutError,
    McpUnavailableError,
)

# 官方 GitHub MCP / 主流 SDK 常用版本；握手失败时可扩展协商
_DEFAULT_PROTOCOL_VERSION = "2024-11-05"


class StdioTransport:
    """启动 MCP server 子进程，经 stdin/stdout 交换 JSON-RPC。"""

    def __init__(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_s: float = 30.0,
        protocol_version: str = _DEFAULT_PROTOCOL_VERSION,
        client_name: str = "fixloop",
        client_version: str = "0.2.0",
    ) -> None:
        if not command:
            raise ValueError("StdioTransport command 不能为空")
        self._command = list(command)
        self._env = env
        self._cwd = cwd
        self.timeout_s = timeout_s
        self._protocol_version = protocol_version
        self._client_name = client_name
        self._client_version = client_version
        self._proc: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._lock = threading.Lock()
        self._stderr_thread: threading.Thread | None = None
        self._stderr_lines: list[str] = []
        self.available = False

    def start(self) -> None:
        if self._proc is not None:
            return
        env = os.environ.copy()
        if self._env:
            env.update(self._env)
        try:
            self._proc = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self._cwd,
                env=env,
            )
        except OSError as exc:
            raise McpUnavailableError(
                "无法启动 MCP server 进程",
                detail=str(exc),
            ) from exc
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name="mcp-stdio-stderr",
            daemon=True,
        )
        self._stderr_thread.start()
        try:
            self._initialize()
        except Exception:
            self.close()
            raise
        self.available = True

    def close(self) -> None:
        self.available = False
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.available or self._proc is None:
            raise McpUnavailableError("MCP stdio 未启动或已关闭")
        with self._lock:
            return self._request_unlocked(method, params or {})

    def _initialize(self) -> None:
        result = self._request_unlocked(
            "initialize",
            {
                "protocolVersion": self._protocol_version,
                "capabilities": {},
                "clientInfo": {
                    "name": self._client_name,
                    "version": self._client_version,
                },
            },
        )
        if not isinstance(result, dict):
            raise McpSchemaError("initialize 响应非法")
        # 通知：无 id，不期待 response
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _request_unlocked(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self._proc is not None
        req_id = self._next_id
        self._next_id += 1
        msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        self._write(msg)
        deadline = time.monotonic() + self.timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpTimeoutError(
                    f"MCP stdio 等待响应超时 (>{self.timeout_s}s)",
                    detail=method,
                )
            line = self._readline(timeout=remaining)
            if line is None:
                raise McpUnavailableError(
                    "MCP server 进程已退出",
                    detail=self._exit_detail(),
                )
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            # 通知 / 无 id：跳过
            if "id" not in payload:
                continue
            if payload.get("id") != req_id:
                continue
            if "error" in payload and payload["error"] is not None:
                err = payload["error"]
                if isinstance(err, dict):
                    msg_text = str(err.get("message") or err)
                    detail = json.dumps(err, ensure_ascii=False)
                else:
                    msg_text = str(err)
                    detail = ""
                raise McpUnavailableError(msg_text, detail=detail)
            result = payload.get("result")
            if result is None:
                raise McpSchemaError("JSON-RPC 响应缺少 result", detail=line[:200])
            if not isinstance(result, dict):
                # tools/list 的 result 是 object；少数 method 可能返回非 dict — 包一层
                return {"value": result}
            return result

    def _write(self, msg: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        try:
            self._proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except OSError as exc:
            raise McpUnavailableError("写入 MCP stdin 失败", detail=str(exc)) from exc

    def _readline(self, *, timeout: float) -> str | None:
        assert self._proc is not None and self._proc.stdout is not None
        if self._proc.poll() is not None:
            return None
        line_box: list[str | None] = [None]
        err_box: list[BaseException | None] = [None]

        def _read() -> None:
            try:
                line_box[0] = self._proc.stdout.readline()  # type: ignore[union-attr]
            except BaseException as exc:  # noqa: BLE001
                err_box[0] = exc

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            raise McpTimeoutError(
                f"MCP stdio 读取超时 (>{timeout:.1f}s)",
                detail="readline",
            )
        if err_box[0] is not None:
            raise McpUnavailableError("读取 MCP stdout 失败", detail=str(err_box[0]))
        line = line_box[0]
        if line is None or line == "":
            return None
        return line.strip()

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                text = line.rstrip("\n")
                if text:
                    self._stderr_lines.append(text)
                    if len(self._stderr_lines) > 200:
                        self._stderr_lines = self._stderr_lines[-100:]
        except Exception:  # noqa: BLE001
            return

    def _exit_detail(self) -> str:
        proc = self._proc
        code = proc.returncode if proc else None
        tail = " | ".join(self._stderr_lines[-5:])
        return f"exit={code}; stderr={tail}"

    def __enter__(self) -> StdioTransport:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
