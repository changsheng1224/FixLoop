"""预构建 / 列出 SWE-bench 评测镜像（经 WSL 调用官方 prepare_images）。"""

from __future__ import annotations

import json
from pathlib import Path

from src.benchmark.swebench.dev_instances import DATASET_NAME, DATASET_SPLIT, DEV_INSTANCE_IDS
from src.benchmark.swebench.wsl_util import (
    preferred_wsl_distro,
    resolve_wsl_python,
    run_wsl,
    win_to_wsl_path,
    wsl_proxy_env,
)


def prepare_images_wsl(
    instance_ids: list[str] | None = None,
    *,
    dataset_name: str = DATASET_NAME,
    split: str = DATASET_SPLIT,
    max_workers: int = 1,
    force_rebuild: bool = False,
    timeout_s: int = 7200,
    distro: str | None = None,
    python_bin: str | None = None,
) -> dict:
    """在 WSL 中运行 ``python -m swebench.harness.prepare_images``。

    官方逻辑：已存在的 ``instance_image_key`` 会跳过，故默认可复用。
    """
    ids = list(instance_ids) if instance_ids else list(DEV_INSTANCE_IDS)
    distro = distro or preferred_wsl_distro()
    if not distro:
        return {"ok": False, "error": "no WSL distro", "instance_ids": ids}
    _ = python_bin or resolve_wsl_python()  # ensure venv resolved / side-effect probe
    # prepare_images.py → swebench → benchmark → src → repo root
    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "scripts" / "swebench_prepare_images.sh"
    if not script.is_file():
        return {
            "ok": False,
            "error": f"missing script: {script}",
            "instance_ids": ids,
        }
    script_wsl = win_to_wsl_path(script)
    cmd = ["bash", script_wsl]
    if force_rebuild:
        cmd.append("--force")
    cmd.extend(
        [
            "--dataset",
            dataset_name,
            "--split",
            split,
            "--max-workers",
            str(max_workers),
        ]
    )
    cmd.extend(ids)
    result = run_wsl(
        cmd,
        distro=distro,
        timeout_s=timeout_s,
        env_exports=wsl_proxy_env(distro),
    )
    ok = result.returncode == 0
    return {
        "ok": ok,
        "returncode": result.returncode,
        "instance_ids": ids,
        "dataset_name": dataset_name,
        "force_rebuild": force_rebuild,
        "stdout_tail": (result.stdout or "")[-4000:],
        "stderr_tail": (result.stderr or "")[-4000:],
        "error": "" if ok else ((result.stderr or result.stdout or "prepare_images failed")[:2000]),
    }


def list_swebench_images_wsl(*, distro: str | None = None) -> dict:
    """列出名称含 sweb / swebench 的本地 Docker 镜像。"""
    distro = distro or preferred_wsl_distro()
    if not distro:
        return {"ok": False, "error": "no WSL distro", "images": []}
    result = run_wsl(
        [
            "bash",
            "-lc",
            "docker images --format '{{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.Size}}' | "
            "grep -Ei 'sweb|swebench|swe-bench' || true",
        ],
        distro=distro,
        timeout_s=60,
    )
    lines = [ln for ln in (result.stdout or "").splitlines() if ln.strip()]
    return {
        "ok": result.returncode == 0,
        "images": lines,
        "count": len(lines),
        "stderr_tail": (result.stderr or "")[-500:],
    }


def prepare_images_report_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
