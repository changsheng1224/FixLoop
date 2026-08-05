"""官方 predictions JSONL 写入/读取。"""

from __future__ import annotations

import json
from pathlib import Path

from src.benchmark.swebench.types import InstanceResult


def write_predictions_jsonl(path: Path | str, results: list[InstanceResult]) -> Path:
    from src.benchmark.swebench.patch_export import normalize_patch_lf

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for r in results:
        pred = r.to_prediction()
        pred["model_patch"] = normalize_patch_lf(pred.get("model_patch") or "")
        lines.append(json.dumps(pred, ensure_ascii=False))
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def read_predictions_jsonl(path: Path | str) -> list[dict]:
    path = Path(path)
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def validate_prediction(pred: dict) -> list[str]:
    """返回缺失字段列表；空列表表示合法。"""
    missing = []
    for key in ("instance_id", "model_name_or_path", "model_patch"):
        if key not in pred:
            missing.append(key)
    return missing
