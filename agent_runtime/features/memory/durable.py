"""持久记忆 — Durable Memory：跨会话 Markdown 文件存储。"""

import re
from pathlib import Path

DURABLE_TOPICS = [
    "project-conventions", "key-decisions", "dependency-facts", "user-preferences",
]
SAVE_INTENT_WORDS = ["remember", "记住", "保存", "记录", "永记", "don't forget", "备忘", "存下来"]
PREFIX_MAP = {
    "Convention:": "project-conventions", "Decision:": "key-decisions",
    "Dependency:": "dependency-facts", "Preference:": "user-preferences",
}


class DurableMemoryStore:
    def __init__(self, root: str):
        self.memory_dir = Path(root) / ".agent" / "memory"
        self.topics_dir = self.memory_dir / "topics"

    def ensure_dirs(self):
        self.topics_dir.mkdir(parents=True, exist_ok=True)

    def promote(self, promotions: list[tuple[str, str]]):
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
        m = {"preference": "user-preferences", "convention": "project-conventions",
             "decision": "key-decisions", "dependency": "dependency-facts"}
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


def promote_durable_memory(user_message: str, final_answer: str,
                            store: DurableMemoryStore | None = None,
                            root: str = ".") -> bool:
    if not _has_save_intent(user_message):
        return False
    promotions = _extract_promotions(final_answer)
    if not promotions:
        return False
    if store is None:
        store = DurableMemoryStore(root)
    valid = [(t, txt) for t, txt in promotions if not reject_durable_reason(txt)]
    if not valid:
        return False
    store.promote(valid)
    return True


def _has_save_intent(user_message: str) -> bool:
    msg_lower = user_message.lower()
    return any(w.lower() in msg_lower for w in SAVE_INTENT_WORDS)


def _extract_promotions(text: str) -> list[tuple[str, str]]:
    promotions = []
    for line in text.splitlines():
        line = line.strip()
        for prefix, topic in PREFIX_MAP.items():
            if line.startswith(prefix):
                body = line[len(prefix):].strip()
                if body:
                    promotions.append((topic, f"{prefix} {body}"))
                break
    return promotions


def reject_durable_reason(text: str) -> str:
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
