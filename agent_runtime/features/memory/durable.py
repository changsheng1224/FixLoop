"""持久记忆 — Durable Memory：跨会话 Markdown 文件存储。"""

import re
from dataclasses import dataclass, field
from pathlib import Path

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


class DurableMemoryStore:
    """跨会话 Markdown 持久记忆（.agent/memory/topics/）。"""

    def __init__(self, root: str):
        self.memory_dir = Path(root) / ".agent" / "memory"
        self.topics_dir = self.memory_dir / "topics"

    # ── 结构化用户画像 ──

    def get_preferences(self) -> list[UserPreference]:
        """读取 user-preferences topic 的结构化条目。"""
        entries = self._read_topic(self.topics_dir / "user-preferences.md")
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
        return prefs

    def upsert_preference(self, pref: UserPreference) -> None:
        """写入或更新一个用户偏好条目（按 key 去重）。"""
        self.ensure_dirs()
        topic_file = self.topics_dir / "user-preferences.md"
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
        topic_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._update_index()

    def ensure_dirs(self):
        """创建 memory/topics 目录。"""
        self.topics_dir.mkdir(parents=True, exist_ok=True)

    def promote(self, promotions: list[tuple[str, str]]):
        """将 (topic, text) 条目 upsert 到对应 topic 文件并更新索引。"""
        if not promotions:
            return
        self.ensure_dirs()
        by_topic: dict[str, list[str]] = {}
        for topic, text in promotions:
            topic = self._normalize_topic(topic)
            by_topic.setdefault(topic, []).append(text)
        for topic, texts in by_topic.items():
            topic_file = self.topics_dir / f"{topic}.md"
            existing = self._read_topic(topic_file)
            for text in texts:
                existing = self._upsert_entry(existing, text)
            self._write_topic(topic_file, existing)
        self._update_index()

    def retrieval(self, query: str, limit: int = 3) -> list[dict]:
        """返回带 topic 标注的结果列表。"""
        query_lower = query.lower()
        results = []
        if not self.topics_dir.exists():
            return results
        for topic_file in sorted(self.topics_dir.glob("*.md")):
            topic = topic_file.stem
            for entry in self._read_topic(topic_file):
                if query_lower in entry.lower():
                    results.append({"topic": topic, "text": entry})
                    if len(results) >= limit:
                        return results
        return results

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

    @staticmethod
    def _read_topic(path: Path) -> list[str]:
        if not path.exists():
            return []
        return [e.strip() for e in path.read_text(encoding="utf-8").split("\n---\n") if e.strip()]

    @staticmethod
    def _write_topic(path: Path, entries: list[str]):
        if entries:
            path.write_text("\n\n---\n\n".join(entries) + "\n", encoding="utf-8")
        elif path.exists():
            path.unlink()

    @staticmethod
    def _upsert_entry(entries: list[str], new_text: str) -> list[str]:
        new_subject = new_text.split("\n")[0].strip()
        for i, entry in enumerate(entries):
            if entry.split("\n")[0].strip().lower() == new_subject.lower():
                entries[i] = new_text
                return entries
        entries.append(new_text)
        return entries

    def _update_index(self):
        lines = ["# Agent Memory Index", "", f"_{len(DURABLE_TOPICS)} topics_", ""]
        for topic in DURABLE_TOPICS:
            count = len(self._read_topic(self.topics_dir / f"{topic}.md"))
            lines.append(f"- **{topic}** ({count} entries)")
        lines.append("")
        (self.memory_dir / "MEMORY.md").write_text("\n".join(lines), encoding="utf-8")


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
