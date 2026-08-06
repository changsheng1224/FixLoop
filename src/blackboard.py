"""Multi-Agent Blackboard：共享状态板 + 冲突检测。

Agent 通过 Blackboard 交换结构化数据，Orchestrator 负责仲裁冲突。
"""

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class BlackboardEntry:
    """黑板上的单条记录。"""

    key: str
    value: any
    source_agent: str
    created_at: float = field(default_factory=time.time)
    ttl: float | None = None  # None = 永不过期
    status: str = "accepted"
    evidence_refs: list[str] = field(default_factory=list)
    base_revision: int = 0
    revision: int = 0
    entry_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

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
        self._revision = 0
        self._proposals: dict[str, dict] = {}

    @property
    def revision(self) -> int:
        return self._revision

    def propose(
        self,
        key: str,
        value,
        source_agent: str,
        *,
        evidence_refs: list[str] | None = None,
        base_revision: int | None = None,
    ) -> dict:
        """Add an untrusted proposal; only merge_proposal can accept it."""
        proposal = {
            "proposal_id": uuid.uuid4().hex[:12],
            "key": key,
            "value": value,
            "source_agent": source_agent,
            "evidence_refs": list(dict.fromkeys(evidence_refs or [])),
            "base_revision": self._revision if base_revision is None else int(base_revision),
            "status": "proposal",
            "created_at": time.time(),
        }
        self._proposals[proposal["proposal_id"]] = proposal
        return dict(proposal)

    def merge_proposal(self, proposal: dict) -> dict:
        """CAS merge proposal; stale proposals are retained as conflicts."""
        if proposal.get("base_revision") != self._revision:
            conflict = {**proposal, "status": "stale", "current_revision": self._revision}
            self._conflicts.append(conflict)
            return conflict
        key = str(proposal.get("key", ""))
        existing = self._entries.get(key)
        if existing and not existing.expired() and existing.value == proposal.get("value"):
            self._revision += 1
            existing.evidence_refs = list(
                dict.fromkeys(existing.evidence_refs + list(proposal.get("evidence_refs") or []))
            )
            existing.revision = self._revision
            accepted = {**proposal, "status": "accepted", "revision": self._revision}
            self._proposals.pop(proposal.get("proposal_id", ""), None)
            return accepted
        if existing and not existing.expired() and existing.value != proposal.get("value"):
            conflict = {
                **proposal,
                "status": "conflicted",
                "existing_source": existing.source_agent,
                "existing_value": existing.value,
            }
            self._conflicts.append(conflict)
            return conflict
        self._revision += 1
        entry = BlackboardEntry(
            key=key,
            value=proposal.get("value"),
            source_agent=str(proposal.get("source_agent", "")),
            status="accepted",
            evidence_refs=list(proposal.get("evidence_refs") or []),
            base_revision=int(proposal.get("base_revision", 0) or 0),
            revision=self._revision,
            entry_id=str(proposal.get("proposal_id", "")),
        )
        self._entries[key] = entry
        accepted = {**proposal, "status": "accepted", "revision": self._revision}
        self._proposals.pop(proposal.get("proposal_id", ""), None)
        return accepted

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
            revision=self._revision + 1,
        )
        self._revision += 1
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
            "revision": self._revision,
            "proposals": list(self._proposals.values()),
        }

    def proposals(self) -> list[dict]:
        """Return pending proposals for governance/reporting."""
        return [dict(item) for item in self._proposals.values()]

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
