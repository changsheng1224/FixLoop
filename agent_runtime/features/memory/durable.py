"""持久记忆 — Durable Memory：跨会话 Markdown 文件存储。"""

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class ConflictResolution(Enum):
    NONE = "none"
    EQUIVALENT = "equivalent"
    OVERRIDE = "override"
    INVALID = "invalid"


# 权威序：user > agent > auto
AUTHORITY_ORDER = {"user": 3, "agent": 2, "auto": 1}

DURABLE_TOPICS = [
    "project-conventions",
    "key-decisions",
    "dependency-facts",
    "user-preferences",
]
SAVE_INTENT_WORDS = ["remember", "记住", "保存", "记录", "永记", "don't forget", "备忘", "存下来"]
PREFIX_MAP = {
    "Convention:": "project-conventions",
    "Decision:": "key-decisions",
    "Dependency:": "dependency-facts",
    "Preference:": "user-preferences",
}


@dataclass
class UserPreference:
    """结构化用户偏好条目。"""

    key: str
    value: str
    confidence: float = 1.0
    source: str = ""
    updated_at: float = 0.0

    def __post_init__(self):
        if not self.updated_at:
            self.updated_at = time.time()

    @classmethod
    def from_line(cls, line: str) -> "UserPreference | None":
        """从 `| key | value | confidence | source |` 表格行解析。"""
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) < 2:
            return None
        try:
            conf = float(parts[2]) if len(parts) > 2 else 1.0
        except (ValueError, IndexError):
            conf = 1.0
        return cls(
            key=parts[0],
            value=parts[1],
            confidence=conf,
            source=parts[3] if len(parts) > 3 else "",
        )

    def to_table_row(self) -> str:
        return f"| {self.key} | {self.value} | {self.confidence:.2f} | {self.source} |"

    @staticmethod
    def table_header() -> str:
        return "| key | value | confidence | source |\n|-----|-------|------------|--------|"


DECAY_RATE = 0.95  # 每天衰减 5%（模块级常量）
CHUNK_THRESHOLD_BYTES = 32768  # 32KB — topic 文件超此阈值拆分为 chunked 存储
CHUNK_ENTRIES_PER_FILE = 15    # 每个 chunk 文件最大条目数


class DurableMemoryStore:
    """跨会话 Markdown 持久记忆（.agent/memory/topics/）。

    MEMORY.md 路由表：topic | entries | bytes | strategy(inline|chunked)。
    小 topic → topics/{t}.md；大 topic → topics/{t}/chunk-{n}.md。
    """

    def __init__(self, root: str):
        self.memory_dir = Path(root) / ".agent" / "memory"
        self.topics_dir = self.memory_dir / "topics"
        self._topics_root = str(self.topics_dir.resolve())

    def _ensure_within(self, path: Path) -> None:
        """确保路径在 topics_dir 内（防止路径遍历攻击）。

        使用 path_safety.resolve_under_root 统一校验（含 .. 和 symlink 检测）。
        """
        from agent_runtime.features.memory.candidate import MemoryPathError, resolve_memory_path

        try:
            resolve_memory_path(self._topics_root, str(path))
        except MemoryPathError:
            raise
        except Exception:
            # fallback: 原有 startswith 检查
            if not str(path.resolve()).startswith(self._topics_root):
                raise MemoryPathError(str(path), detail="路径不在 topics_dir 内")

    def _topic_path(self, topic: str, chunk: int | None = None) -> Path:
        """返回 topic 文件路径。chunked 策略时指定 chunk 编号。"""
        if chunk is not None:
            chunk_dir = self.topics_dir / topic
            return chunk_dir / f"chunk-{chunk}.md"
        return self.topics_dir / f"{topic}.md"

    def _topic_strategy(self, topic: str) -> str:
        """判断 topic 存储策略：inline 或 chunked。"""
        chunk_dir = self.topics_dir / topic
        if chunk_dir.is_dir():
            return "chunked"
        path = self._topic_path(topic)
        if not path.is_file():
            return "inline"
        return "chunked" if path.stat().st_size > CHUNK_THRESHOLD_BYTES else "inline"

    def _load_routing_table(self) -> dict[str, dict]:
        """从 MEMORY.md 解析路由表。返回 {topic: {entries, bytes, strategy}}。"""
        index_path = self.memory_dir / "MEMORY.md"
        routing: dict[str, dict] = {}
        if not index_path.is_file():
            return routing
        in_table = False
        for line in index_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("| topic") or line.startswith("|---"):
                in_table = True
                continue
            if in_table and line.startswith("|"):
                parts = [p.strip() for p in line.split("|") if p.strip()]
                if len(parts) >= 4:
                    try:
                        routing[parts[0]] = {
                            "entries": int(parts[1]),
                            "bytes": int(parts[2]),
                            "strategy": parts[3],
                        }
                    except (ValueError, IndexError):
                        pass
            elif in_table and not line.startswith("|"):
                break
        return routing

    # ── 结构化用户画像 ──

    def get_preferences(self) -> list[UserPreference]:
        """读取 user-preferences topic 的结构化条目（含时间衰减）。"""
        entries = self._read_topic("user-preferences")
        prefs = []
        in_table = False
        for line in entries:
            if line.startswith("| key "):
                in_table = True
                continue
            if line.startswith("|---"):
                continue
            if in_table and line.startswith("|"):
                pref = UserPreference.from_line(line)
                if pref:
                    prefs.append(pref)
            else:
                in_table = False
        return _apply_time_decay(prefs)

    def upsert_preference(self, pref: UserPreference) -> None:
        """写入或更新一个用户偏好条目（按 key 去重）。"""
        self.ensure_dirs()
        header = UserPreference.table_header()
        existing = self.get_preferences()
        # upsert
        replaced = False
        for i, p in enumerate(existing):
            if p.key.lower() == pref.key.lower():
                existing[i] = pref
                replaced = True
                break
        if not replaced:
            existing.append(pref)
        lines = [header] + [p.to_table_row() for p in existing]
        (self.topics_dir / "user-preferences.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._update_index()

    def ensure_dirs(self):
        """创建 memory/topics 目录。"""
        self.topics_dir.mkdir(parents=True, exist_ok=True)

    def promote(self, promotions: list[tuple[str, str]]):
        """将 (topic, text) 条目 upsert 到对应 topic 并更新路由表。

        超 CHUNK_THRESHOLD_BYTES 自动 split 为 chunked 存储。
        """
        if not promotions:
            return
        self.ensure_dirs()
        by_topic: dict[str, list[str]] = {}
        for topic, text in promotions:
            topic = self._normalize_topic(topic)
            if topic not in DURABLE_TOPICS:
                continue  # 拒绝未知 topic
            by_topic.setdefault(topic, []).append(text)
        for topic, texts in by_topic.items():
            strategy = self._topic_strategy(topic)
            existing = self._read_topic(topic, strategy=strategy)
            for text in texts:
                existing = self._upsert_entry(existing, text)
            self._write_topic(topic, existing)
        self._update_index()

    def retrieval(self, query: str, limit: int = 3) -> list[dict]:
        """返回带 topic 标注的结果列表。

        先读 MEMORY.md 路由表 → chunked 主题只读前 2 chunk（semantic max-pool），
        inline 主题全量读取。
        """
        query_lower = query.lower()
        results = []
        if not self.topics_dir.exists():
            return results

        routing = self._load_routing_table()
        for topic in sorted(DURABLE_TOPICS):
            if len(results) >= limit:
                break
            route = routing.get(topic, {})
            strategy = route.get("strategy", "inline")
            if strategy == "chunked":
                # chunked: semantic max-pool — 只读前 2 chunk
                entries = self._read_chunked_first(topic, max_chunks=2)
            else:
                entries = self._read_topic(topic)
            for entry in entries:
                if query_lower in entry.lower():
                    results.append({"topic": topic, "text": entry})
                    if len(results) >= limit:
                        break
        return results

    def _read_chunked_first(self, topic: str, max_chunks: int = 2) -> list[str]:
        """读取 chunked topic 的前 N 个 chunk（semantic max-pool 优化）。"""
        chunk_dir = self.topics_dir / topic
        if not chunk_dir.is_dir():
            return []
        entries: list[str] = []
        for chunk_file in sorted(chunk_dir.glob("chunk-*.md"))[:max_chunks]:
            try:
                entries.extend(
                    e.strip() for e in chunk_file.read_text(encoding="utf-8").split("\n---\n") if e.strip()
                )
            except OSError:
                pass
        return entries

    def _normalize_topic(self, topic: str) -> str:
        t = topic.lower()
        if t in DURABLE_TOPICS:
            return t
        m = {
            "preference": "user-preferences",
            "convention": "project-conventions",
            "decision": "key-decisions",
            "dependency": "dependency-facts",
        }
        return m.get(t, "project-conventions")

    def _read_topic(self, topic_or_path: str | Path, strategy: str = "inline") -> list[str]:
        """读取 topic 全部条目（inline 读单文件，chunked 合并多 chunk）。"""
        if isinstance(topic_or_path, Path):
            # backward compat: direct path read
            path = topic_or_path
            if not path.exists():
                return []
            return [e.strip() for e in path.read_text(encoding="utf-8").split("\n---\n") if e.strip()]

        topic = topic_or_path
        if strategy == "chunked":
            return self._read_chunked(topic)
        path = self._topic_path(topic)
        if not path.exists():
            return []
        return [e.strip() for e in path.read_text(encoding="utf-8").split("\n---\n") if e.strip()]

    def _read_chunked(self, topic: str) -> list[str]:
        """读取 chunked topic 的全部 chunk 并合并。"""
        chunk_dir = self.topics_dir / topic
        if not chunk_dir.is_dir():
            return []
        entries: list[str] = []
        for chunk_file in sorted(chunk_dir.glob("chunk-*.md")):
            try:
                entries.extend(
                    e.strip() for e in chunk_file.read_text(encoding="utf-8").split("\n---\n") if e.strip()
                )
            except OSError:
                pass
        return entries

    def _write_topic(self, topic: str, entries: list[str]):
        """写入 topic 条目（inline 或 chunked，超阈值自动 split）。"""
        if not entries:
            # 清理
            inline_path = self._topic_path(topic)
            if inline_path.exists():
                inline_path.unlink()
            chunk_dir = self.topics_dir / topic
            if chunk_dir.is_dir():
                import shutil
                shutil.rmtree(str(chunk_dir), ignore_errors=True)
            return

        inline_path = self._topic_path(topic)
        test_text = "\n\n---\n\n".join(entries) + "\n"
        if len(test_text.encode("utf-8")) <= CHUNK_THRESHOLD_BYTES:
            # inline 足够
            chunk_dir = self.topics_dir / topic
            if chunk_dir.is_dir():
                import shutil
                shutil.rmtree(str(chunk_dir), ignore_errors=True)
            inline_path.write_text(test_text, encoding="utf-8")
        else:
            # chunked 拆分
            if inline_path.exists():
                inline_path.unlink()
            chunk_dir = self.topics_dir / topic
            chunk_dir.mkdir(parents=True, exist_ok=True)
            # 清除旧 chunk
            for old in chunk_dir.glob("chunk-*.md"):
                old.unlink()
            for ci in range(0, len(entries), CHUNK_ENTRIES_PER_FILE):
                chunk_entries = entries[ci:ci + CHUNK_ENTRIES_PER_FILE]
                chunk_path = chunk_dir / f"chunk-{ci // CHUNK_ENTRIES_PER_FILE}.md"
                chunk_path.write_text(
                    "\n\n---\n\n".join(chunk_entries) + "\n", encoding="utf-8"
                )

    @staticmethod
    def _upsert_entry(entries: list[str], new_text: str, authority: str = "auto") -> list[str]:
        new_subject = new_text.split("\n")[0].strip().lower()
        for i, entry in enumerate(entries):
            if entry.split("\n")[0].strip().lower() == new_subject:
                result = _resolve_conflict(entry, new_text, authority)
                if result in (ConflictResolution.OVERRIDE, ConflictResolution.EQUIVALENT):
                    entries[i] = new_text
                elif result == ConflictResolution.INVALID:
                    # 互斥版本：追加而非覆盖
                    ver = sum(1 for e in entries if e.split("\n")[0].strip().lower() == new_subject) + 1
                    entries.append(new_text.replace("\n", f"  # v{ver}\n", 1))
                return entries
        entries.append(new_text)
        return entries

    def _update_index(self):
        """写入 MEMORY.md 路由表：| topic | entries | bytes | strategy |。"""
        rows = [
            "# Agent Memory Index",
            "",
            f"_{len(DURABLE_TOPICS)} topics_",
            "",
            "| topic | entries | bytes | strategy |",
            "|-------|---------|-------|----------|",
        ]
        for topic in DURABLE_TOPICS:
            strategy = self._topic_strategy(topic)
            entries = self._read_topic(topic, strategy=strategy)
            count = len(entries)
            byte_size = 0
            if strategy == "chunked":
                chunk_dir = self.topics_dir / topic
                if chunk_dir.is_dir():
                    for cf in chunk_dir.glob("chunk-*.md"):
                        try:
                            byte_size += cf.stat().st_size
                        except OSError:
                            pass
            else:
                path = self._topic_path(topic)
                if path.is_file():
                    try:
                        byte_size = path.stat().st_size
                    except OSError:
                        pass
            rows.append(f"| {topic} | {count} | {byte_size} | {strategy} |")
        rows.append("")
        (self.memory_dir / "MEMORY.md").write_text("\n".join(rows), encoding="utf-8")


def promote_durable_memory(
    user_message: str, final_answer: str, store: DurableMemoryStore | None = None, root: str = ".",
    light_client=None,
) -> bool:
    """检测用户保存意图并从回答中提取 Convention/Decision 等条目写入 durable。

    规则抽取失败时，可选的 light_client 做 LLM 分类（仅填 topic/key，不自由建库）。
    """
    if not _has_save_intent(user_message):
        return False
    promotions = _extract_promotions(final_answer)
    if not promotions and light_client is not None:
        promotions = _llm_extract_promotions(final_answer, light_client)
    if not promotions:
        return False
    if store is None:
        store = DurableMemoryStore(root)
    valid = [(t, txt) for t, txt in promotions if not reject_durable_reason(txt)]
    if not valid:
        return False
    store.promote(valid)
    return True


def _llm_extract_promotions(text: str, client) -> list[tuple[str, str]]:
    """LLM 分类：将自由文本映射到预定义 topic（禁止自由建库）。"""
    topics = ", ".join(DURABLE_TOPICS)
    prompt = (
        f"从以下文本提取知识条目。只输出 JSON 数组：\n"
        f"[{{\"topic\":\"{DURABLE_TOPICS[0]}\",\"text\":\"...\"}},...]\n"
        f"topic 只能从 [{topics}] 中选择。\n\n{text[:500]}"
    )
    try:
        import json

        raw = client.complete(prompt, max_new_tokens=256)
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start >= 0 and end > start:
            items = json.loads(raw[start:end])
            return [
                (item["topic"], item.get("text", ""))
                for item in items
                if item.get("topic") in DURABLE_TOPICS and item.get("text")
            ]
    except Exception:
        pass
    return []


def _has_save_intent(user_message: str) -> bool:
    msg_lower = user_message.lower()
    return any(w.lower() in msg_lower for w in SAVE_INTENT_WORDS)


def _extract_promotions(text: str) -> list[tuple[str, str]]:
    promotions = []
    for line in text.splitlines():
        line = line.strip()
        for prefix, topic in PREFIX_MAP.items():
            if line.startswith(prefix):
                body = line[len(prefix) :].strip()
                if body:
                    promotions.append((topic, f"{prefix} {body}"))
                break
    return promotions


def _resolve_conflict(existing: str, new: str, new_authority: str = "auto") -> ConflictResolution:
    """冲突状态机：比较新旧条目，按权威序决定是否覆盖。"""
    if not existing:
        return ConflictResolution.NONE
    if existing.strip().lower() == new.strip().lower():
        return ConflictResolution.EQUIVALENT
    # 从条目中提取 authority 标记
    old_auth = "auto"
    m = re.search(r"\[authority:(\w+)\]", existing)
    if m:
        old_auth = m.group(1)
    new_rank = AUTHORITY_ORDER.get(new_authority, 1)
    old_rank = AUTHORITY_ORDER.get(old_auth, 1)
    if new_rank > old_rank:
        return ConflictResolution.OVERRIDE
    return ConflictResolution.INVALID


def _apply_time_decay(prefs: list[UserPreference]) -> list[UserPreference]:
    """对偏好条目应用时间衰减；低于阈值 0.1 的不再参与召回。"""
    now = time.time()
    result = []
    for p in prefs:
        days = (now - p.updated_at) / 86400
        if days > 1:
            p.confidence = round(p.confidence * (DECAY_RATE ** days), 3)
        if p.confidence >= 0.1:
            result.append(p)
    return result


def reject_durable_reason(text: str) -> str:
    """校验 durable 条目；返回空串表示通过，否则为拒绝原因。"""
    text = text.strip()
    if not text:
        return "空内容"
    if len(text) < 5:
        return "内容过短"
    if len(text) > 500:
        return "内容过长"
    if re.search(r"sk-[a-zA-Z0-9]{20,}", text):
        return "疑似包含 API key"
    if re.search(r"gh[pous]_[a-zA-Z0-9]{20,}", text):
        return "疑似包含 GitHub token"
    return ""
