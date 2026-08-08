"""Session Store：会话 JSON 持久化到 .agent/sessions/。"""

import hashlib
import json
import re
import threading
import uuid
from pathlib import Path

from agent_runtime.session_contract import SESSION_SCHEMA_VERSION

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class SessionStore:
    """会话持久化存储。

    目录结构：
        .agent/sessions/{session_id}.json
    """

    def __init__(self, root: str, *, trace=None):
        self.root = Path(root)
        self.sessions_dir = self.root / ".agent" / "sessions"
        self.trace = trace

    def _emit(self, event: str, payload: dict) -> None:
        if self.trace is not None:
            try:
                self.trace(event, payload, "ok")
            except TypeError:
                try:
                    self.trace(event, payload)
                except Exception:
                    pass
            except Exception:
                pass

    def _lock(self, session_id: str) -> threading.RLock:
        key = str(self.sessions_dir / session_id)
        with _LOCKS_GUARD:
            return _LOCKS.setdefault(key, threading.RLock())

    @staticmethod
    def _validate_id(session_id: str) -> str:
        value = str(session_id or "")
        if not _SESSION_ID_RE.fullmatch(value):
            raise ValueError("invalid session id")
        return value

    def _workspace_id(self) -> str:
        return hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:16]

    def ensure_dir(self):
        """创建 .agent/sessions/ 目录（若不存在）。"""
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        session: dict,
        *,
        user_id: str = "",
        workspace_id: str = "",
        expected_revision: int | None = None,
    ):
        """保存会话到 JSON 文件。

        Args:
            session: 会话字典（必须含 "id" 字段）。
        """
        self.ensure_dir()
        session_id = self._validate_id(session.get("id", "unknown"))
        path = self.sessions_dir / f"{session_id}.json"
        lock = self._lock(session_id)
        with lock:
            current = self._read_path(path)
            current_revision = int((current or {}).get("revision", 0) or 0)
            if expected_revision is not None and current_revision != int(expected_revision):
                raise RuntimeError(
                    "session revision mismatch: "
                    f"expected={expected_revision}, current={current_revision}"
                )
            payload = dict(session)
            payload["schema_version"] = str(payload.get("schema_version") or SESSION_SCHEMA_VERSION)
            payload["revision"] = current_revision + 1
            scope = dict(payload.get("session_scope") or {})
            if user_id:
                scope["user_id"] = str(user_id)
            else:
                scope.setdefault("user_id", "")
            if workspace_id:
                scope["workspace_id"] = str(workspace_id)
            else:
                scope.setdefault("workspace_id", self._workspace_id())
            scope.setdefault("session_id", session_id)
            payload["session_scope"] = scope
            encoded = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
            tmp = self.sessions_dir / f".{session_id}.{uuid.uuid4().hex}.tmp"
            tmp.write_text(encoded, encoding="utf-8")
            tmp.replace(path)
            try:
                bak = path.with_suffix(".json.bak")
                bak_tmp = self.sessions_dir / f".{session_id}.{uuid.uuid4().hex}.bak.tmp"
                bak_tmp.write_text(encoded, encoding="utf-8")
                bak_tmp.replace(bak)
            except OSError:
                pass
            session.clear()
            session.update(payload)
            self._emit("session_saved", {
                "session_id": session_id,
                "revision": payload["revision"],
                "workspace_id": scope.get("workspace_id", ""),
            })
            return payload

    def load(
        self,
        session_id: str,
        *,
        user_id: str = "",
        workspace_id: str = "",
    ) -> dict | None:
        """读取指定会话；损坏时尝试 .bak 恢复。

        Args:
            session_id: 会话 ID。

        Returns:
            会话字典，不存在或损坏恢复失败时返回 None。
        """
        session_id = self._validate_id(session_id)
        path = self.sessions_dir / f"{session_id}.json"
        if not path.exists():
            return None
        try:
            data = self._read_path(path)
            if not isinstance(data, dict):
                bak = path.with_suffix(".json.bak")
                data = self._read_path(bak) if bak.exists() else None
            if not isinstance(data, dict):
                return None
            scope = data.get("session_scope") or {}
            if user_id and str(scope.get("user_id", "")) != str(user_id):
                return None
            if workspace_id and str(scope.get("workspace_id", "")) != str(workspace_id):
                return None
            self._emit("session_loaded", {
                "session_id": session_id,
                "revision": data.get("revision", 0),
                "workspace_id": scope.get("workspace_id", ""),
            })
            return data
        except (json.JSONDecodeError, OSError):
            bak = path.with_suffix(".json.bak")
            if bak.exists():
                try:
                    data = self._read_path(bak)
                    return data if isinstance(data, dict) else None
                except Exception:
                    pass
            return None

    @staticmethod
    def _read_path(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def latest(self) -> str | None:
        """返回最近修改的 session id（按 mtime 排序）。

        Returns:
            最新的 session id，无会话时返回 None。
        """
        if not self.sessions_dir.exists():
            return None
        files = sorted(
            self.sessions_dir.glob("*.json"),
            key=lambda p: (p.stat().st_mtime_ns, p.name),
            reverse=True,
        )
        if not files:
            return None
        return files[0].stem

    def list_all(self) -> list[str]:
        """列出所有 session id。"""
        if not self.sessions_dir.exists():
            return []
        return sorted([p.stem for p in self.sessions_dir.glob("*.json")])
