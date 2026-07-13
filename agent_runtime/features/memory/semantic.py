"""语义记忆 — Semantic Memory：基于 embedding 的同义词检索。

离线 / GFW 策略（按优先级）：
1. ``HF_HUB_OFFLINE=1`` — 强制离线，仅用本地缓存
2. 本地已有模型缓存 — 自动设置 ``HF_HUB_OFFLINE=1``，避免启动时访问 huggingface.co
3. ``HF_ENDPOINT=https://hf-mirror.com`` — 首次下载走国内镜像
"""

import hashlib
import os
import threading
from pathlib import Path

_SEMANTIC_MODEL = None
_SEMANTIC_LOCK = threading.Lock()
_SEMANTIC_INIT_LOGGED = False
_SEMANTIC_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_MAX_CHARS = 800  # ~256 tokens for English（all-MiniLM-L6-v2 max_seq_length）
_EMBED_CACHE_DIR = Path(".agent/embed_cache")


def _embed_cache_path(content_hash: str) -> Path:
    return _EMBED_CACHE_DIR / f"{content_hash}.npy"


def _load_embed_cache(content_hash: str):
    """从磁盘加载缓存的 embedding；不存在返回 None。"""
    path = _embed_cache_path(content_hash)
    if not path.is_file():
        return None
    try:
        import numpy as np

        return np.load(path)
    except Exception:
        return None


def _save_embed_cache(content_hash: str, embedding):
    """将 embedding 缓存到磁盘。"""
    try:
        _EMBED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        import numpy as np

        np.save(str(_embed_cache_path(content_hash)), embedding)
    except Exception:
        pass


def _hf_cache_dir() -> Path:
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return Path(hf_home) / "hub"
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "huggingface" / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _model_cached_locally(model_id: str) -> bool:
    cache_name = "models--" + model_id.replace("/", "--")
    snapshots = _hf_cache_dir() / cache_name / "snapshots"
    if not snapshots.is_dir():
        return False
    return any((snap / "config.json").is_file() for snap in snapshots.iterdir())


def _parse_bool_env(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes"):
        return True
    if normalized in ("0", "false", "no"):
        return False
    return None


def _configure_hf_hub(model_id: str) -> None:
    """在加载 SentenceTransformer 前配置 HuggingFace Hub 连接策略。"""
    explicit = os.environ.get("HF_HUB_OFFLINE")
    if explicit is not None:
        parsed = _parse_bool_env(explicit)
        if parsed is True:
            os.environ["HF_HUB_OFFLINE"] = "1"
            return
        if parsed is False:
            os.environ["HF_HUB_OFFLINE"] = "0"
            return

    if _model_cached_locally(model_id):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")


def _get_semantic_model():
    global _SEMANTIC_MODEL, _SEMANTIC_INIT_LOGGED
    if _SEMANTIC_MODEL is not None:
        return _SEMANTIC_MODEL
    with _SEMANTIC_LOCK:
        if _SEMANTIC_MODEL is not None:
            return _SEMANTIC_MODEL
        try:
            import sys as _sys

            if not _SEMANTIC_INIT_LOGGED:
                _SEMANTIC_INIT_LOGGED = True
                print(
                    "[agent_runtime] 加载语义模型 (~90MB)...", file=_sys.stderr, end="", flush=True
                )

            _configure_hf_hub(_SEMANTIC_MODEL_ID)

            from sentence_transformers import SentenceTransformer

            _SEMANTIC_MODEL = SentenceTransformer(_SEMANTIC_MODEL_ID)

            if _SEMANTIC_INIT_LOGGED:
                print(" ✅", file=_sys.stderr)
            return _SEMANTIC_MODEL
        except Exception:
            if _SEMANTIC_INIT_LOGGED:
                import sys as _sys

                print(" ⚠ 不可用（语义检索降级为 keywords 模式）", file=_sys.stderr)
            return None


class ChunkedMemoryNote:
    """语料 chunk + max-pool 检索（V1.4-Bonus9）。

    超长文本按 EMBED_MAX_CHARS 分段 → 每段独立 embed。
    search 时 query 与所有 chunk 的 cosine 取 max（max-pool）。
    """

    def __init__(self, text: str, note: dict, model, max_chars: int = EMBED_MAX_CHARS):
        self._text = text
        self._note = note
        self._chunks: list[dict] = []
        self._model = model
        self._max_chars = max_chars
        self._embed_chunks()

    @property
    def chunks(self) -> list[dict]:
        return list(self._chunks)

    @property
    def note(self) -> dict:
        return dict(self._note)

    def _embed_chunks(self) -> None:
        text = self._text.strip()
        if not text or self._model is None:
            return
        # 按 max_chars 分段
        segments = _chunk_text(text, self._max_chars)
        for seg in segments:
            try:
                emb = None
                content_hash = hashlib.sha256(seg.encode("utf-8")).hexdigest()[:32]
                cached = _load_embed_cache(content_hash)
                if cached is not None:
                    emb = cached
                else:
                    emb = self._model.encode(seg)
                    _save_embed_cache(content_hash, emb)
                self._chunks.append({"text": seg[:200], "embedding": emb})
            except Exception:
                pass

    def max_similarity(self, query_embedding) -> float:
        """计算 query 与所有 chunk 的最大 cosine 相似度。"""
        import numpy as np

        best = 0.0
        for ch in self._chunks:
            emb = ch.get("embedding")
            if emb is None:
                continue
            sim = float(
                np.dot(query_embedding, emb)
                / (np.linalg.norm(query_embedding) * np.linalg.norm(emb))
            )
            if sim > best:
                best = sim
        return best


class SemanticMemory:
    """基于 SentenceTransformer embedding 的 episodic 语义检索。"""

    def __init__(self):
        self.model = _get_semantic_model()
        self._notes: list[dict] = []

    @property
    def available(self) -> bool:
        """语义模型是否已成功加载。"""
        return self.model is not None

    def add(self, note: dict):
        """为 note 计算 embedding 并缓存（短文本单 embed，长文本 chunk+max-pool）。"""
        if not self.available:
            return
        text = note.get("text", "")
        if not text:
            return
        try:
            if len(text) <= EMBED_MAX_CHARS:
                # 短文本：单 embedding
                truncated = _truncate_head_tail(text)
                content_hash = hashlib.sha256(truncated.encode("utf-8")).hexdigest()[:32]
                embedding = _load_embed_cache(content_hash)
                if embedding is not None:
                    self._notes.append({**note, "embedding": embedding})
                    return
                embedding = self.model.encode(truncated)
                _save_embed_cache(content_hash, embedding)
                self._notes.append({**note, "embedding": embedding})
            else:
                # 长文本：chunk + max-pool
                cmn = ChunkedMemoryNote(text, note, self.model)
                self._notes.append(cmn)
        except Exception:
            pass

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        """按 cosine 相似度检索 top_k notes（sim > 0.3，chunk note 使用 max-pool）。"""
        if not self.available or not self._notes:
            return []
        try:
            import numpy as np

            short_query = derive_embed_query(query)
            query_emb = self.model.encode(short_query)
            scores = []
            for note in self._notes:
                if isinstance(note, ChunkedMemoryNote):
                    sim = note.max_similarity(query_emb)
                    if sim > 0.3:
                        scores.append((sim, note.note))
                else:
                    emb = note.get("embedding")
                    if emb is None:
                        continue
                    sim = float(
                        np.dot(query_emb, emb)
                        / (np.linalg.norm(query_emb) * np.linalg.norm(emb))
                    )
                    if sim > 0.3:
                        scores.append((sim, note))
            scores.sort(key=lambda x: x[0], reverse=True)
            return [note for _, note in scores[:top_k]]
        except Exception:
            return []


def _chunk_text(text: str, max_chars: int = EMBED_MAX_CHARS) -> list[str]:
    """将长文本按 max_chars 分段（在空白边界切分，避免截断单词）。"""
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    while len(text) > max_chars:
        # 在 max_chars 范围内找最近的空白边界
        cut = max_chars
        for sep in ("\n", ". ", " "):
            idx = text.rfind(sep, 0, max_chars)
            if idx > max_chars // 2:
                cut = idx + len(sep)
                break
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


def _truncate_head_tail(text: str, max_chars: int = EMBED_MAX_CHARS) -> str:
    """按 head + tail 截断长文本，保留头（异常类型）和尾（Traceback）。"""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return f"{text[:half]}\n...\n{text[-half:]}"


def derive_embed_query(user_message: str, task_summary: str = "") -> str:
    """从 user request 提取 embedding 搜索关键词（~100 chars）。

    抽取规则：异常类型 → 文件名 → 函数名 → task_summary fallback。
    """
    import re

    parts: list[str] = []
    # 1. 异常类型
    exc = re.findall(r"(\w+(?:Error|Exception|Warning))", user_message)
    parts.extend(exc[:2])
    # 2. 文件名
    files = re.findall(r'File\s+"([^"]+\.py)"', user_message)
    fnames = [f.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for f in files]
    parts.extend(fnames[:2])
    # 3. 函数名
    funcs = re.findall(r"def\s+(\w+)|'(\w+)'|\"(\w+)\"|\s(\w+)\(", user_message)
    for g in funcs:
        name = next((x for x in g if x and len(x) > 2), None)
        if name and name not in parts:
            parts.append(name)
    # 4. fallback
    if not parts and task_summary:
        parts.append(task_summary[:100])
    if not parts:
        parts.append(user_message[:100])
    return " ".join(parts)[:200].strip()


def retrieval_candidates_semantic(state: dict, query: str, limit: int = 3) -> list[dict]:
    """合并 keyword 与 semantic 检索结果并去重。"""
    from agent_runtime.features.memory.episodic import retrieval_candidates

    kw_results = retrieval_candidates(state, query, limit)
    sem = SemanticMemory()
    for note in state.get("episodic_notes", [])[-20:]:
        sem.add(note)
    sem_results = sem.search(query, limit)
    seen = set()
    merged = []
    for note in kw_results + sem_results:
        idx = note.get("note_index")
        if idx is not None and idx not in seen:
            seen.add(idx)
            merged.append(note)
    return merged[:limit]
