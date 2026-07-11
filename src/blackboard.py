"""Multi-Agent Blackboard：共享状态板 + 冲突检测。

Agent 通过 Blackboard 交换结构化数据，Orchestrator 负责仲裁冲突。
"""

import time
from dataclasses import dataclass, field


@dataclass
class BlackboardEntry:
    """黑板上的单条记录。"""

    key: str
    value: any
    source_agent: str
    created_at: float = field(default_factory=time.time)
    ttl: float | None = None  # None = 永不过期

    def expired(self) -> bool:
        """条目是否已超过 TTL。"""
        if self.ttl is None:
            return False
        return time.time() - self.created_at > self.ttl


class Blackboard:
    """共享状态板。

    - 同 key 同 source：覆盖写入。
    - 同 key 不同 source：记录冲突，不覆盖。
    - 支持 TTL 过期。
    - 支持前缀匹配读取。
    """

    def __init__(self):
        self._entries: dict[str, BlackboardEntry] = {}
        self._conflicts: list[dict] = []

    def write(self, key: str, value, source_agent: str, ttl: float | None = None) -> bool:
        """写入条目。返回 True 表示成功，False 表示冲突。

        Args:
            key: 条目标识。
            value: 条目值。
            source_agent: 来源 Agent 名。
            ttl: 过期时间（秒），None 表示永不过期。

        Returns:
            True 写入成功，False 存在冲突。
        """
        existing = self._entries.get(key)
        if existing and not existing.expired():
            if existing.source_agent != source_agent:
                self._conflicts.append(
                    {
                        "key": key,
                        "sources": [existing.source_agent, source_agent],
                        "values": [existing.value, value],
                    }
                )
                return False
        # 覆盖或新增
        self._entries[key] = BlackboardEntry(
            key=key,
            value=value,
            source_agent=source_agent,
            ttl=ttl,
        )
        return True

    def read(self, key: str):
        """读取单条。过期返回 None。"""
        entry = self._entries.get(key)
        if entry and entry.expired():
            del self._entries[key]
            return None
        return entry.value if entry else None

    def read_related(self, prefix: str) -> dict[str, any]:
        """前缀匹配读取所有相关条目。"""
        result = {}
        expired_keys = []
        for key, entry in self._entries.items():
            if key.startswith(prefix):
                if entry.expired():
                    expired_keys.append(key)
                else:
                    result[key] = entry.value
        for key in expired_keys:
            del self._entries[key]
        return result

    def snapshot(self) -> dict:
        """返回当前板面的不可变副本。"""
        return {
            "entries": {k: v.value for k, v in self._entries.items() if not v.expired()},
            "conflicts": list(self._conflicts),
        }

    def resolve_conflict(self, key: str, winner_source: str):
        """手动仲裁冲突——保留 winner_source 的版本。"""
        self._conflicts = [c for c in self._conflicts if c["key"] != key]

    def apply_conflict_winner(self, key: str, value, winner_source: str) -> None:
        """仲裁后强制写入 winner 版本并清除该 key 的冲突记录。"""
        self._entries[key] = BlackboardEntry(
            key=key,
            value=value,
            source_agent=winner_source,
        )
        self.resolve_conflict(key, winner_source)

    @property
    def conflicts(self) -> list[dict]:
        """当前未仲裁的写入冲突列表。"""
        return list(self._conflicts)
