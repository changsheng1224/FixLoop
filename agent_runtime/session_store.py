"""Session Store：会话 JSON 持久化到 .agent/sessions/。"""

import json
from pathlib import Path


class SessionStore:
    """会话持久化存储。

    目录结构：
        .agent/sessions/{session_id}.json
    """

    def __init__(self, root: str):
        self.root = Path(root)
        self.sessions_dir = self.root / ".agent" / "sessions"

    def ensure_dir(self):
        """创建 .agent/sessions/ 目录（若不存在）。"""
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def save(self, session: dict):
        """保存会话到 JSON 文件。

        Args:
            session: 会话字典（必须含 "id" 字段）。
        """
        self.ensure_dir()
        session_id = session.get("id", "unknown")
        path = self.sessions_dir / f"{session_id}.json"
        # 原子写：先写 .tmp 再 rename
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(session, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(path)

    def load(self, session_id: str) -> dict | None:
        """读取指定会话。

        Args:
            session_id: 会话 ID。

        Returns:
            会话字典，不存在时返回 None。
        """
        path = self.sessions_dir / f"{session_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

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
