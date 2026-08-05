"""数据集加载：本地 JSONL 优先，可选 HuggingFace datasets。"""

from __future__ import annotations

import json
from pathlib import Path

from src.benchmark.swebench.dev_instances import DATASET_NAME, DATASET_SPLIT
from src.benchmark.swebench.types import SweInstance


class DatasetError(RuntimeError):
    """数据集不可用（归为 env 失败）。"""


def load_instances_from_jsonl(path: Path | str) -> list[SweInstance]:
    path = Path(path)
    if not path.is_file():
        raise DatasetError(f"JSONL not found: {path}")
    out: list[SweInstance] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(SweInstance.from_dict(json.loads(line)))
    return out


def load_instances_from_hf(
    *,
    dataset_name: str = DATASET_NAME,
    split: str = DATASET_SPLIT,
) -> list[SweInstance]:
    try:
        from datasets import load_dataset
    except ImportError as e:
        raise DatasetError(
            "huggingface `datasets` not installed; "
            "pip install datasets 或使用 --instances-jsonl"
        ) from e
    try:
        ds = load_dataset(dataset_name, split=split)
    except Exception as e:  # noqa: BLE001 — 网络/缓存统一归 env
        raise DatasetError(f"load_dataset failed: {e}") from e
    return [SweInstance.from_dict(dict(row)) for row in ds]


def filter_instances(
    instances: list[SweInstance],
    *,
    instance_ids: list[str] | None = None,
    limit: int | None = None,
) -> list[SweInstance]:
    if instance_ids:
        want = set(instance_ids)
        selected = [i for i in instances if i.instance_id in want]
        missing = want - {i.instance_id for i in selected}
        if missing:
            raise DatasetError(f"instance_ids not found: {sorted(missing)}")
        # 保持调用方顺序
        by_id = {i.instance_id: i for i in selected}
        ordered = [by_id[i] for i in instance_ids if i in by_id]
    else:
        ordered = list(instances)
    if limit is not None:
        ordered = ordered[: max(0, int(limit))]
    return ordered


def load_instances(
    *,
    instances_jsonl: Path | str | None = None,
    dataset_name: str = DATASET_NAME,
    split: str = DATASET_SPLIT,
    instance_ids: list[str] | None = None,
    limit: int | None = None,
) -> list[SweInstance]:
    if instances_jsonl:
        raw = load_instances_from_jsonl(instances_jsonl)
    else:
        raw = load_instances_from_hf(dataset_name=dataset_name, split=split)
    return filter_instances(raw, instance_ids=instance_ids, limit=limit)
