"""三层记忆系统：Working Memory + Episodic Memory + Durable Memory。

Working Memory：当前任务上下文（容量有限，频繁读写）
Episodic Memory：本轮会话的事件笔记（容量有限，FIFO 淘汰）
Durable Memory：跨会话持久记忆（读写 .agent/memory/ Markdown 文件）
"""

import re
import time
from pathlib import Path

# ============================================================================
# 常量
# ============================================================================

MAX_RECENT_FILES = 8
MAX_FILE_SUMMARIES = 6
MAX_EPISODIC_NOTES = 12


# ============================================================================
# 初始状态与规范化
# ============================================================================


def default_memory_state() -> dict:
    """返回初始记忆结构。

    Returns:
        {
            "working": {"task_summary": "", "recent_files": []},
            "episodic_notes": [],
            "file_summaries": {},
            "next_note_index": 0,
        }
    """
    return {
        "working": {
            "task_summary": "",
            "recent_files": [],
        },
        "episodic_notes": [],
        "file_summaries": {},
        "next_note_index": 0,
    }


def normalize_memory_state(state: dict, workspace_root: str) -> dict:
    """规范化记忆状态：兼容旧格式，裁剪超限条目。

    Args:
        state: 当前记忆状态（可能为 None 或不完整）。
        workspace_root: workspace 根目录（用于过滤已删除的文件）。

    Returns:
        规范化后的记忆状态。
    """
    if not isinstance(state, dict):
        return default_memory_state()

    # 确保 working 层存在
    if "working" not in state:
        state["working"] = {"task_summary": "", "recent_files": []}
    working = state["working"]
    if not isinstance(working, dict):
        working = {"task_summary": "", "recent_files": []}
        state["working"] = working

    # 确保字段存在
    working.setdefault("task_summary", "")
    working.setdefault("recent_files", [])

    # 裁剪 recent_files（只保留 8 个且文件存在）
    working["recent_files"] = _filter_existing(
        working["recent_files"][:MAX_RECENT_FILES], workspace_root
    )

    # 确保 episodic_notes
    if "episodic_notes" not in state:
        state["episodic_notes"] = []
    state["episodic_notes"] = state["episodic_notes"][:MAX_EPISODIC_NOTES]

    # 确保 file_summaries
    if "file_summaries" not in state:
        state["file_summaries"] = {}
    # 裁剪到 MAX_FILE_SUMMARIES，保留最新的
    summaries = state["file_summaries"]
    if isinstance(summaries, dict) and len(summaries) > MAX_FILE_SUMMARIES:
        # 按 freshness 排序，保留最新 6 个
        sorted_items = sorted(
            summaries.items(),
            key=lambda x: x[1].get("created_at", 0) if isinstance(x[1], dict) else 0,
            reverse=True,
        )
        state["file_summaries"] = dict(sorted_items[:MAX_FILE_SUMMARIES])

    state.setdefault("next_note_index", 0)

    return state


def _filter_existing(paths: list[str], root: str) -> list[str]:
    """过滤掉不存在的文件路径。"""
    root_path = Path(root)
    result = []
    for p in paths:
        if (root_path / p).exists():
            result.append(p)
    return result


# ============================================================================
# Working Memory 层
# ============================================================================


def set_task_summary(state: dict, user_message: str):
    """设置当前任务的一句话摘要。

    优先用预生成的摘要（调用方通过 light_client 生成后传入）；
    没有时退化为截断用户输入前 300 字符。

    Args:
        state: 记忆状态。
        user_message: 用户输入或预生成的摘要文本。
    """
    summary = user_message.strip().replace("\n", " ")[:300]
    state["working"]["task_summary"] = summary


def remember_file(state: dict, path: str):
    """记录一个文件到 recent_files（去重 + LRU + trim 到 8）。

    Args:
        state: 记忆状态。
        path: 文件相对路径。
    """
    files = state["working"]["recent_files"]
    # 去重：已存在则移到末尾
    if path in files:
        files.remove(path)
    files.append(path)
    # Trim
    state["working"]["recent_files"] = files[-MAX_RECENT_FILES:]


def set_file_summary(state: dict, path: str, summary: str):
    """存储文件摘要（带 freshness hash）。

    Args:
        state: 记忆状态。
        path: 文件路径。
        summary: 文件内容摘要（取前 180 字符）。
    """
    state["file_summaries"][path] = {
        "summary": summary[:180],
        "created_at": time.time(),
        "freshness": _freshness_hash(path),
    }


def invalidate_file_summary(state: dict, path: str):
    """删除文件的摘要缓存（文件被修改后失效）。

    Args:
        state: 记忆状态。
        path: 文件路径。
    """
    state["file_summaries"].pop(path, None)


def _freshness_hash(path: str) -> str:
    """计算文件的 freshness hash（基于 mtime + size）。"""
    try:
        p = Path(path)
        if p.exists():
            stat = p.stat()
            return f"{stat.st_mtime}:{stat.st_size}"
    except OSError:
        pass
    return ""


# ============================================================================
# Episodic Memory 层
# ============================================================================


def append_note(
    state: dict,
    text: str,
    tags: list[str] | None = None,
    source: str = "",
    kind: str = "observation",
):
    """追加一条事件笔记（dedupe by text + trim 到 12 条）。

    Args:
        state: 记忆状态。
        text: 笔记内容。
        tags: 标签列表。
        source: 来源（如文件路径）。
        kind: 类型: "observation" | "error" | "decision"。
    """
    if tags is None:
        tags = []

    # Dedupe：如果最后一条笔记内容完全相同，不重复添加
    notes = state["episodic_notes"]
    if notes and notes[-1].get("text") == text:
        return

    note_index = state["next_note_index"]
    state["next_note_index"] = note_index + 1

    note = {
        "text": text[:300],  # 截断
        "tags": tags[:5],    # 最多 5 个 tag
        "source": source,
        "created_at": time.time(),
        "note_index": note_index,
        "kind": kind,
    }
    notes.append(note)

    # Trim 到 12 条（FIFO）
    if len(notes) > MAX_EPISODIC_NOTES:
        state["episodic_notes"] = notes[-MAX_EPISODIC_NOTES:]


def retrieval_candidates(state: dict, query: str, limit: int = 3) -> list[dict]:
    """检索与查询相关的记忆条目。

    排序策略：tag 精确匹配 > keyword 重叠 > recency 时间衰减。

    Args:
        state: 记忆状态。
        query: 搜索查询。
        limit: 返回条数上限。

    Returns:
        相关笔记列表（按相关性降序）。
    """
    query_lower = query.lower()
    query_tokens = set(query_lower.split())

    notes = state.get("episodic_notes", [])
    if not notes:
        return []

    scored = []
    now = time.time()
    for note in notes:
        score = 0.0
        text = note.get("text", "").lower()
        tags = [t.lower() for t in note.get("tags", [])]

        # Tag 精确匹配（权重最高）
        for token in query_tokens:
            if token in tags:
                score += 3.0
            # 文本关键词重叠
            if token in text:
                score += 1.0

        # Tag 子串匹配
        for tag in tags:
            if tag in query_lower:
                score += 2.0

        # Recency 时间衰减（1 小时内的笔记加分，仅当已有相关性）
        if score > 0:
            age_hours = (now - note.get("created_at", now)) / 3600
            if age_hours < 1:
                score += 1.0 * (1 - age_hours)

        if score > 0:
            scored.append((score, note))

    # 按分数降序，取 top_k
    scored.sort(key=lambda x: x[0], reverse=True)
    return [note for _, note in scored[:limit]]


# ============================================================================
# Semantic Memory — 基于 embedding 的语义检索（补充 keywords 匹配）
# ============================================================================

_SEMANTIC_MODEL = None  # 懒加载单例


def _get_semantic_model():
    """懒加载 sentence-transformers 模型（约 80MB）。

    Returns:
        SentenceTransformer 实例，加载失败返回 None。
    """
    global _SEMANTIC_MODEL
    if _SEMANTIC_MODEL is not None:
        return _SEMANTIC_MODEL
    try:
        from sentence_transformers import SentenceTransformer
        _SEMANTIC_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
        return _SEMANTIC_MODEL
    except Exception:
        return None


class SemanticMemory:
    """基于 embedding 的语义记忆。

    使用本地 all-MiniLM-L6-v2 模型（约 80MB）做 embedding，
    cosine similarity 检索。keywords 匹配的补充——处理同义词和英文变体。
    """

    def __init__(self):
        self.model = _get_semantic_model()
        self._notes: list[dict] = []  # [{text, embedding, tags, ...}]

    @property
    def available(self) -> bool:
        return self.model is not None

    def add(self, note: dict):
        """添加一条记忆条目（含 embedding）。

        Args:
            note: 记忆条目 dict（必须含 'text' 字段）。
        """
        if not self.available:
            return
        text = note.get("text", "")
        if not text:
            return
        try:
            embedding = self.model.encode(text)
            self._notes.append({
                **note,
                "embedding": embedding,
            })
        except Exception:
            pass

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        """语义检索：cosine similarity 排序。

        Args:
            query: 搜索查询。
            top_k: 返回条数。

        Returns:
            相关记忆条目列表。
        """
        if not self.available or not self._notes:
            return []
        try:
            import numpy as np

            query_emb = self.model.encode(query)
            scores = []
            for note in self._notes:
                emb = note.get("embedding")
                if emb is None:
                    continue
                sim = float(
                    np.dot(query_emb, emb)
                    / (np.linalg.norm(query_emb) * np.linalg.norm(emb))
                )
                if sim > 0.3:  # 阈值：太低不算相关
                    scores.append((sim, note))

            scores.sort(key=lambda x: x[0], reverse=True)
            return [note for _, note in scores[:top_k]]
        except Exception:
            return []


def retrieval_candidates_semantic(
    state: dict, query: str, limit: int = 3
) -> list[dict]:
    """语义 + keywords 混合检索。

    先做 keywords 精确匹配（快速），再用 semantic 补充（同义词/变体）。
    两路结果合并去重后取 top_k。

    Args:
        state: 记忆状态。
        query: 搜索查询。
        limit: 返回条数上限。

    Returns:
        相关记忆条目列表。
    """
    # 第一路：keywords
    kw_results = retrieval_candidates(state, query, limit)

    # 第二路：semantic（如果模型可用）
    sem = SemanticMemory()
    notes = state.get("episodic_notes", [])
    for note in notes[-20:]:  # 只索引最近 20 条
        sem.add(note)
    sem_results = sem.search(query, limit)

    # 合并去重（按 note_index）
    seen = set()
    merged = []
    for note in kw_results + sem_results:
        idx = note.get("note_index")
        if idx is not None and idx not in seen:
            seen.add(idx)
            merged.append(note)

    return merged[:limit]


# ============================================================================
# Durable Memory 层 — 跨会话持久化（Markdown 文件）
# ============================================================================

# 内置主题
DURABLE_TOPICS = [
    "project-conventions",
    "key-decisions",
    "dependency-facts",
    "user-preferences",
]

# 意图检测词：用户想保存到 durable memory
SAVE_INTENT_WORDS = [
    "remember", "记住", "保存", "记录", "永记",
    "don't forget", "备忘", "存下来",
]

# 行前缀映射：从 final_answer 提取特定类型的记忆
PREFIX_MAP = {
    "Convention:": "project-conventions",
    "Decision:": "key-decisions",
    "Dependency:": "dependency-facts",
    "Preference:": "user-preferences",
}


class DurableMemoryStore:
    """跨会话持久记忆存储。

    记忆目录结构：
        .agent/memory/
        ├── MEMORY.md          # 索引文件（所有主题的列表）
        └── topics/
            ├── project-conventions.md
            ├── key-decisions.md
            ├── dependency-facts.md
            └── user-preferences.md
    """

    def __init__(self, root: str):
        self.root = Path(root)
        self.memory_dir = self.root / ".agent" / "memory"
        self.topics_dir = self.memory_dir / "topics"

    def ensure_dirs(self):
        """确保记忆目录存在。"""
        self.topics_dir.mkdir(parents=True, exist_ok=True)

    def promote(self, promotions: list[tuple[str, str]]):
        """将一批 (topic, text) 条目持久化到对应主题文件。

        同一主题下的旧条目如 subject（首行）相同则自动替换。

        Args:
            promotions: [(topic, text), ...] 列表。
        """
        if not promotions:
            return
        self.ensure_dirs()

        entries_by_topic: dict[str, list[str]] = {}
        for topic, text in promotions:
            topic = self._normalize_topic(topic)
            if topic not in entries_by_topic:
                entries_by_topic[topic] = []
            entries_by_topic[topic].append(text)

        for topic, texts in entries_by_topic.items():
            topic_file = self.topics_dir / f"{topic}.md"
            existing = self._read_topic(topic_file)
            for text in texts:
                existing = self._upsert_entry(existing, text)
            self._write_topic(topic_file, existing)

        # 更新索引
        self._update_index()

    def retrieval(self, query: str, limit: int = 3) -> list[str]:
        """从 durable memory 检索相关条目。

        Args:
            query: 搜索词。
            limit: 返回条数上限。

        Returns:
            匹配的文本条目列表。
        """
        query_lower = query.lower()
        results = []
        if not self.topics_dir.exists():
            return results

        for topic_file in sorted(self.topics_dir.glob("*.md")):
            entries = self._read_topic(topic_file)
            for entry in entries:
                if query_lower in entry.lower():
                    results.append(entry)
                    if len(results) >= limit:
                        return results
        return results

    def _normalize_topic(self, topic: str) -> str:
        """规范化 topic 名（未知 topic 归入 project-conventions）。"""
        mapping = {
            "project-conventions": "project-conventions",
            "key-decisions": "key-decisions",
            "dependency-facts": "dependency-facts",
            "user-preferences": "user-preferences",
            "preference": "user-preferences",
            "convention": "project-conventions",
            "decision": "key-decisions",
            "dependency": "dependency-facts",
        }
        return mapping.get(topic.lower(), "project-conventions")

    @staticmethod
    def _read_topic(path: Path) -> list[str]:
        if not path.exists():
            return []
        content = path.read_text(encoding="utf-8")
        # 按 "---" 分隔条目
        entries = [e.strip() for e in content.split("\n---\n") if e.strip()]
        return entries

    @staticmethod
    def _write_topic(path: Path, entries: list[str]):
        if entries:
            path.write_text("\n\n---\n\n".join(entries) + "\n", encoding="utf-8")
        elif path.exists():
            path.unlink()

    @staticmethod
    def _upsert_entry(entries: list[str], new_text: str) -> list[str]:
        """插入或替换条目。首行作为 subject 用于去重。"""
        new_subject = new_text.split("\n")[0].strip()
        for i, entry in enumerate(entries):
            subject = entry.split("\n")[0].strip()
            if subject.lower() == new_subject.lower():
                entries[i] = new_text  # 替换
                return entries
        entries.append(new_text)  # 新增
        return entries

    def _update_index(self):
        """重建索引文件 MEMORY.md。"""
        lines = [
            "# Agent Memory Index",
            "",
            f"_{len(DURABLE_TOPICS)} topics_",
            "",
        ]
        for topic in DURABLE_TOPICS:
            topic_file = self.topics_dir / f"{topic}.md"
            count = len(self._read_topic(topic_file))
            lines.append(f"- **{topic}** ({count} entries)")
        lines.append("")
        index_path = self.memory_dir / "MEMORY.md"
        index_path.write_text("\n".join(lines), encoding="utf-8")


def promote_durable_memory(
    user_message: str,
    final_answer: str,
    store: DurableMemoryStore | None = None,
    root: str = ".",
) -> bool:
    """检测用户意图，从 final_answer 提取记忆条目并持久化。

    Args:
        user_message: 用户原始输入。
        final_answer: Agent 的最终答案。
        store: DurableMemoryStore 实例（可选，如不传则用 root 创建）。
        root: workspace 根目录。

    Returns:
        True 如果至少成功 promote 了一条。
    """
    # 检测保存意图
    if not _has_save_intent(user_message):
        return False

    # 从 final_answer 中提取带前缀的条目
    promotions = _extract_promotions(final_answer)
    if not promotions:
        return False

    # 创建 store（如果未传入）
    if store is None:
        store = DurableMemoryStore(root)

    # 过滤
    valid = []
    for topic, text in promotions:
        reason = reject_durable_reason(text)
        if reason:
            continue
        valid.append((topic, text))

    if not valid:
        return False

    store.promote(valid)
    return True


def _has_save_intent(user_message: str) -> bool:
    """检测用户消息中是否含保存意图词。"""
    msg_lower = user_message.lower()
    return any(word.lower() in msg_lower for word in SAVE_INTENT_WORDS)


def _extract_promotions(text: str) -> list[tuple[str, str]]:
    """从文本中提取带前缀的记忆条目。

    支持的格式：
        Convention: <text>
        Decision: <text>
        Dependency: <text>
        Preference: <text>
    每行独立解析。
    """
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
    """拒绝写入 durable memory 的理由。返回空字符串表示通过。

    拒绝条件：
    - 空内容
    - 含疑似 API key / token
    - 过短（< 5 字符）
    - 超长噪音（> 500 字符）
    """
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
