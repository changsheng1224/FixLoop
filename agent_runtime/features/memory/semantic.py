"""语义记忆 — Semantic Memory：基于 embedding 的同义词检索。

GFW 下设置环境变量 HF_ENDPOINT=https://hf-mirror.com 即可从镜像下载。
"""

import os

_SEMANTIC_MODEL = None


def _get_semantic_model():
    global _SEMANTIC_MODEL
    if _SEMANTIC_MODEL is not None:
        return _SEMANTIC_MODEL
    try:
        # 支持 HF 镜像（解决 GFW 下载问题）
        # export HF_ENDPOINT=https://hf-mirror.com
        if os.environ.get("HF_ENDPOINT"):
            os.environ.setdefault("HF_ENDPOINT", os.environ["HF_ENDPOINT"])

        from sentence_transformers import SentenceTransformer
        _SEMANTIC_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
        return _SEMANTIC_MODEL
    except Exception:
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
