#!/usr/bin/env python3
"""Memory 检索质量评估：recall@5 / precision@5（keyword vs semantic baseline）。

用法:
    python scripts/eval_memory_retrieval.py [--output eval_results/memory_retrieval.json]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 确保项目根目录在 sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

LABELS_PATH = _PROJECT_ROOT / "tests" / "fixtures" / "memory_retrieval_labels.jsonl"
DEFAULT_OUTPUT = _PROJECT_ROOT / "eval_results" / "memory_retrieval.json"
ALL_TOPICS = ["project-conventions", "key-decisions", "dependency-facts", "user-preferences"]


def load_labels(path: Path) -> list[dict]:
    """加载标注数据集。"""
    labels = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                labels.append(json.loads(line))
    return labels


def populate_memory(store, labels: list[dict]) -> None:
    """将所有标注中的 relevant_texts 写入 memory store。"""
    seen: set[str] = set()
    for label in labels:
        for topic in label["relevant_topics"]:
            for text in label["relevant_texts"]:
                key = f"{topic}:{text}"
                if key not in seen:
                    seen.add(key)
                    store.promote([(topic, text)])


def setup_episodic_notes(labels: list[dict]) -> tuple[dict, list[dict]]:
    """创建含 episodic notes 的 memory state 用于 semantic 检索。"""
    from agent_runtime.features.memory.core import default_memory_state

    state = default_memory_state()
    seen: set[str] = set()
    idx = 0
    for label in labels:
        for text in label["relevant_texts"]:
            if text not in seen:
                seen.add(text)
                state["episodic_notes"].append({
                    "text": text,
                    "tags": [],
                    "source": "eval",
                    "created_at": 0,
                    "note_index": idx,
                    "kind": "observation",
                    "retrieve_count": 0,
                })
                idx += 1
    return state, labels


def keyword_retrieval(store, query: str, k: int = 5) -> list[str]:
    """Keyword baseline: DurableMemoryStore.retrieval。"""
    results = store.retrieval(query, limit=k)
    return [r["text"] for r in results]


def semantic_retrieval(state: dict, query: str, k: int = 5) -> list[str]:
    """Semantic retrieval: retrieval_candidates_semantic。"""
    from agent_runtime.features.memory.semantic import retrieval_candidates_semantic

    results = retrieval_candidates_semantic(state, query, limit=k)
    return [r.get("text", "") for r in results]


def compute_metrics(
    retrieved: list[str],
    relevant: list[str],
    relevant_topics: list[str],
    all_topics: list[str],
) -> dict:
    """计算 recall@k / precision@k。"""
    k = len(retrieved)
    relevant_set = set(relevant)
    retrieved_set = set(retrieved)
    hits = retrieved_set & relevant_set

    recall = len(hits) / len(relevant_set) if relevant_set else 0.0
    precision = len(hits) / k if k > 0 else 0.0

    # topic-level 精度：检索结果的 topic 是否在 relevant_topics 中
    relevant_topic_set = set(relevant_topics)

    return {
        "recall": round(recall, 3),
        "precision": round(precision, 3),
        "hits": len(hits),
        "total_relevant": len(relevant_set),
        "retrieved_count": k,
        "relevant_topics": list(relevant_topic_set),
    }


def main(output_path: Path | None = None) -> dict:
    import tempfile

    labels = load_labels(LABELS_PATH)
    print(f"加载 {len(labels)} 条标注查询")

    # --- Keyword baseline ---
    with tempfile.TemporaryDirectory() as tmp:
        from agent_runtime.features.memory.durable import DurableMemoryStore

        store = DurableMemoryStore(tmp)
        populate_memory(store, labels)

        kw_metrics_list = []
        for label in labels:
            retrieved = keyword_retrieval(store, label["query"], k=5)
            metrics = compute_metrics(
                retrieved, label["relevant_texts"],
                label["relevant_topics"], ALL_TOPICS,
            )
            metrics["query"] = label["query"]
            metrics["method"] = "keyword"
            kw_metrics_list.append(metrics)

        kw_avg_recall = round(sum(m["recall"] for m in kw_metrics_list) / len(kw_metrics_list), 3)
        kw_avg_precision = round(sum(m["precision"] for m in kw_metrics_list) / len(kw_metrics_list), 3)

    # --- Semantic baseline ---
    state, _ = setup_episodic_notes(labels)
    sem_metrics_list = []
    for label in labels:
        try:
            retrieved = semantic_retrieval(state, label["query"], k=5)
        except Exception:
            retrieved = []
        metrics = compute_metrics(
            retrieved, label["relevant_texts"],
            label["relevant_topics"], ALL_TOPICS,
        )
        metrics["query"] = label["query"]
        metrics["method"] = "semantic"
        sem_metrics_list.append(metrics)

    sem_avg_recall = round(sum(m["recall"] for m in sem_metrics_list) / max(len(sem_metrics_list), 1), 3)
    sem_avg_precision = round(sum(m["precision"] for m in sem_metrics_list) / max(len(sem_metrics_list), 1), 3)

    report = {
        "dataset": str(LABELS_PATH),
        "num_queries": len(labels),
        "keyword_baseline": {
            "avg_recall@5": kw_avg_recall,
            "avg_precision@5": kw_avg_precision,
            "details": kw_metrics_list,
        },
        "semantic": {
            "avg_recall@5": sem_avg_recall,
            "avg_precision@5": sem_avg_precision,
            "details": sem_metrics_list,
        },
        "comparison": {
            "recall_delta": round(sem_avg_recall - kw_avg_recall, 3),
            "precision_delta": round(sem_avg_precision - kw_avg_precision, 3),
        },
    }

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n报告写入: {output_path}")

    print(f"\n=== 检索质量评估 ===")
    print(f"Keyword  baseline: recall@5={kw_avg_recall:.3f}  precision@5={kw_avg_precision:.3f}")
    print(f"Semantic         : recall@5={sem_avg_recall:.3f}  precision@5={sem_avg_precision:.3f}")
    print(f"Δ (semantic-kw)  : recall={sem_avg_recall - kw_avg_recall:+.3f}  precision={sem_avg_precision - kw_avg_precision:+.3f}")

    return report


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    main(out)
