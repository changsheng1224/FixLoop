"""评测 Manifest：锁定 Case / 模型 / 预算 / 版本。"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from agent_runtime.harness_engineering import build_harness_manifest
from src.benchmark.swebench.dev_instances import DATASET_NAME, DATASET_SPLIT, DEV_INSTANCE_IDS


def _git_head(repo: Path | None = None) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo) if repo else None,
            capture_output=True,
            text=True,
            check=False,
        )
        return (proc.stdout or "").strip() if proc.returncode == 0 else ""
    except OSError:
        return ""


def build_manifest(
    *,
    instance_ids: list[str] | None = None,
    model: str = "fake",
    provider: str = "fake",
    max_retries: int = 1,
    repair_timeout_s: int = 600,
    tool_profile: str = "repair_canonical",
    harness_dataset: str = DATASET_NAME,
    fixloop_root: Path | str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ids = list(instance_ids or DEV_INSTANCE_IDS)
    root = Path(fixloop_root) if fixloop_root else Path.cwd()
    manifest = {
        "schema_version": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dataset": {
            "name": harness_dataset,
            "split": DATASET_SPLIT,
            "instance_ids": ids,
            "count": len(ids),
        },
        "model": {
            "provider": provider,
            "model": model,
        },
        "budget": {
            "max_retries": max_retries,
            "repair_timeout_s": repair_timeout_s,
        },
        "tools": {
            "profile": tool_profile,
        },
        "versions": {
            "fixloop_commit": _git_head(root),
            "adapter": "swebench-adapter-v1",
        },
        "notes": (
            "Dev set only — not a final score. "
            "Do not swap instance_ids after first freeze for a report series."
        ),
    }
    manifest["harness"] = build_harness_manifest(
        root,
        run_id=f"swebench-manifest-{int(time.time())}",
        model=model,
        provider=provider,
        config={
            "dataset": harness_dataset,
            "instance_count": len(ids),
            "max_retries": max_retries,
            "repair_timeout_s": repair_timeout_s,
            "tool_profile": tool_profile,
        },
    )
    if extra:
        manifest["extra"] = extra
    return manifest


def write_manifest(path: Path | str, manifest: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
