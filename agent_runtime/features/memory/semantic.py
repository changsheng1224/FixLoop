"""语义记忆 — Semantic Memory：基于 embedding 的同义词检索。

离线 / GFW 策略（按优先级）：
1. ``HF_HUB_OFFLINE=1`` — 强制离线，仅用本地缓存
2. 本地已有模型缓存 — 自动设置 ``HF_HUB_OFFLINE=1``，避免启动时访问 huggingface.co
3. ``HF_ENDPOINT=https://hf-mirror.com`` — 首次下载走国内镜像
"""

import os
import threading
from pathlib import Path

_SEMANTIC_MODEL = None
_SEMANTIC_LOCK = threading.Lock()
_SEMANTIC_INIT_LOGGED = False
_SEMANTIC_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"


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
                print("[agent_runtime] 加载语义模型 (~90MB)...",
                      file=_sys.stderr, end="", flush=True)

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


class SemanticMemory:
    def __init__(self):
        self.model = _get_semantic_model()
        self._notes: list[dict] = []

    @property
    def available(self) -> bool:
        return self.model is not None

    def add(self, note: dict):
        if not self.available:
            return
        text = note.get("text", "")
        if not text:
            return
        try:
            embedding = self.model.encode(text)
            self._notes.append({**note, "embedding": embedding})
        except Exception:
            pass

    def search(self, query: str, top_k: int = 3) -> list[dict]:
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
                sim = float(np.dot(query_emb, emb) /
                            (np.linalg.norm(query_emb) * np.linalg.norm(emb)))
                if sim > 0.3:
                    scores.append((sim, note))
            scores.sort(key=lambda x: x[0], reverse=True)
            return [note for _, note in scores[:top_k]]
        except Exception:
            return []


def retrieval_candidates_semantic(state: dict, query: str, limit: int = 3) -> list[dict]:
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
