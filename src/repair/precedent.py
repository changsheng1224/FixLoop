"""Repair Precedent 读写一体（V1.4-Bonus9d）。

基于 durable memory 的 ``dependency-facts`` topic，
启动时读取相似修复先例，成功时写入新先例。

每条 precedent 编码为 topic 文件中的一行 JSON：::

    {"issue_type":"type_error","case_id":"case_001","summary":"int() wrapper","ts":1234567890}
"""

from __future__ import annotations

import json
import time
from pathlib import Path

_PRECEDENT_TOPIC = "dependency-facts"
_PRECEDENT_MARKER = "Precedent:"


def _precedent_topic_path(repo_root: str) -> Path:
    return Path(repo_root) / ".agent" / "memory" / "topics" / f"{_PRECEDENT_TOPIC}.md"


class RepairPrecedentStore:
    """Repair 先例存储：读相似修复 + 写成功先例。"""

    def __init__(self, repo_root: str):
        self._path = _precedent_topic_path(repo_root)

    # ---- 读 ----

    def load_similar(self, issue_type: str, limit: int = 3) -> list[dict]:
        """读取与指定 issue_type 匹配的相似修复先例。

        Args:
            issue_type: 问题类型（type_error / import_error / logic_error...）。
            limit: 最多返回条数。

        Returns:
            匹配的先例列表（按时间倒序）。
        """
        entries = self._read_all()
        matched = [e for e in entries if e.get("issue_type") == issue_type]
        matched.sort(key=lambda e: e.get("ts", 0), reverse=True)
        return matched[:limit]

    def load_all(self) -> list[dict]:
        """读取全部先例（用于调试/报告）。"""
        entries = self._read_all()
        entries.sort(key=lambda e: e.get("ts", 0), reverse=True)
        return entries

    # ---- 写 ----

    def upsert(self, issue_type: str, summary: str, case_id: str = "") -> None:
        """写入或更新先例条目。

        Args:
            issue_type: 问题类型。
            summary: patch 摘要（≤200 chars）。
            case_id: 关联的 eval case ID（可选）。
        """
        entry = {
            "issue_type": issue_type,
            "case_id": case_id or "",
            "summary": summary[:200],
            "ts": int(time.time()),
        }
        entries = self._read_all()
        # 去重：同 issue_type + case_id 覆盖旧条目
        if case_id:
            entries = [e for e in entries if not (
                e.get("issue_type") == issue_type and e.get("case_id") == case_id
            )]
        entries.append(entry)
        self._write_all(entries)

    # ---- 内部 ----

    def _read_all(self) -> list[dict]:
        if not self._path.is_file():
            return []
        entries: list[dict] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            try:
                data = json.loads(line)
                if isinstance(data, dict) and "issue_type" in data:
                    entries.append(data)
            except (json.JSONDecodeError, ValueError):
                pass
        return entries

    def _write_all(self, entries: list[dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"# {_PRECEDENT_TOPIC.replace('-', ' ').title()} (Repair Precedents)", ""]
        for e in entries:
            lines.append(json.dumps(e, ensure_ascii=False))
        self._path.write_text("\n".join(lines) + "\n", encoding="utf-8")
