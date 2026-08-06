"""调用官方 swebench.harness.run_evaluation（native 或 WSL）。"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from src.benchmark.swebench.dev_instances import DATASET_NAME
from src.benchmark.swebench.wsl_util import (
    is_windows,
    preferred_wsl_distro,
    probe_wsl,
    resolve_wsl_python,
    run_wsl,
    win_to_wsl_path,
    wsl_proxy_env,
)


@dataclass
class HarnessResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    report_path: Path | None = None
    resolved_ids: list[str] | None = None
    error: str = ""
    backend: str = ""  # native | wsl | none


def native_harness_importable() -> bool:
    if is_windows():
        return False
    try:
        import swebench  # noqa: F401

        return True
    except ImportError:
        return shutil.which("swebench") is not None


def resolve_harness_backend(requested: str = "auto") -> tuple[str, str]:
    """返回 (backend, error)。backend in native|wsl|"" 。"""
    req = (requested or "auto").strip().lower()
    if req == "native":
        if native_harness_importable():
            return "native", ""
        return "", "native harness unavailable (Windows or swebench missing)"
    if req == "wsl":
        probe = probe_wsl()
        if probe.available:
            return "wsl", ""
        return "", probe.error or "WSL harness unavailable"
    # auto
    if native_harness_importable():
        return "native", ""
    if is_windows():
        probe = probe_wsl()
        if probe.available:
            return "wsl", ""
        return "", (
            probe.error
            or "WSL harness unavailable"
        ) + (f" | {probe.note}" if probe.note else "")
    return "", "swebench package not installed (pip install swebench)"


def run_official_harness(
    predictions_path: Path | str,
    *,
    run_id: str,
    dataset_name: str = DATASET_NAME,
    instance_ids: list[str] | None = None,
    max_workers: int = 1,
    cwd: Path | str | None = None,
    timeout_s: int = 3600,
    backend: str = "auto",
    wsl_distro: str | None = None,
    wsl_python: str = "python3",
) -> HarnessResult:
    """官方评价入口：Linux native，或 Windows 下经 WSL。"""
    predictions_path = Path(predictions_path).resolve()
    work = Path(cwd).resolve() if cwd else predictions_path.parent

    chosen, err = resolve_harness_backend(backend)
    if not chosen:
        return HarnessResult(
            ok=False,
            returncode=127,
            stdout="",
            stderr="",
            error=err,
            backend="none",
        )

    if chosen == "wsl":
        return _run_harness_wsl(
            predictions_path,
            run_id=run_id,
            dataset_name=dataset_name,
            instance_ids=instance_ids,
            max_workers=max_workers,
            cwd=work,
            timeout_s=timeout_s,
            distro=wsl_distro or preferred_wsl_distro(),
            python_bin=resolve_wsl_python(wsl_python if wsl_python != "python3" else None),
        )

    return _run_harness_native(
        predictions_path,
        run_id=run_id,
        dataset_name=dataset_name,
        instance_ids=instance_ids,
        max_workers=max_workers,
        cwd=work,
        timeout_s=timeout_s,
    )


def _eval_argv(
    *,
    dataset_name: str,
    predictions_path: str,
    max_workers: int,
    run_id: str,
    instance_ids: list[str] | None,
    python_bin: str,
) -> list[str]:
    cmd = [
        python_bin,
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        dataset_name,
        "--predictions_path",
        predictions_path,
        "--max_workers",
        str(max_workers),
        "--run_id",
        run_id,
    ]
    if instance_ids:
        cmd.append("--instance_ids")
        cmd.extend(instance_ids)
    return cmd


def _run_harness_native(
    predictions_path: Path,
    *,
    run_id: str,
    dataset_name: str,
    instance_ids: list[str] | None,
    max_workers: int,
    cwd: Path,
    timeout_s: int,
) -> HarnessResult:
    cmd = _eval_argv(
        dataset_name=dataset_name,
        predictions_path=str(predictions_path),
        max_workers=max_workers,
        run_id=run_id,
        instance_ids=instance_ids,
        python_bin=sys.executable,
    )
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    report = _find_report(run_id, cwd=cwd)
    resolved = _parse_resolved(report) if report else []
    err = ""
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or f"harness exit {proc.returncode}")[:8000]
    return HarnessResult(
        ok=proc.returncode == 0,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        report_path=report,
        resolved_ids=resolved,
        error=err,
        backend="native",
    )


def _run_harness_wsl(
    predictions_path: Path,
    *,
    run_id: str,
    dataset_name: str,
    instance_ids: list[str] | None,
    max_workers: int,
    cwd: Path,
    timeout_s: int,
    distro: str | None,
    python_bin: str,
) -> HarnessResult:
    if not distro:
        return HarnessResult(
            ok=False,
            returncode=127,
            stdout="",
            stderr="",
            error="no WSL distro configured (set FIXLOOP_WSL_DISTRO or install Ubuntu)",
            backend="wsl",
        )

    pred_wsl = win_to_wsl_path(predictions_path)
    cwd_wsl = win_to_wsl_path(cwd)

    # 预检 swebench
    pre = run_wsl(
        [python_bin, "-c", "import swebench, resource; print('swebench-ok')"],
        distro=distro,
        timeout_s=60,
    )
    if pre.returncode != 0:
        return HarnessResult(
            ok=False,
            returncode=pre.returncode or 127,
            stdout=pre.stdout,
            stderr=pre.stderr,
            error=(
                f"WSL ({distro}) missing swebench: {(pre.stderr or pre.stdout)[:400]}. "
                f"Run: wsl -d {distro} -- {python_bin} -m pip install swebench datasets"
            ),
            backend="wsl",
        )

    cmd = _eval_argv(
        dataset_name=dataset_name,
        predictions_path=pred_wsl,
        max_workers=max_workers,
        run_id=run_id,
        instance_ids=instance_ids,
        python_bin=python_bin,
    )
    result = run_wsl(
        cmd,
        distro=distro,
        cwd_wsl=cwd_wsl,
        timeout_s=timeout_s,
        env_exports=wsl_proxy_env(distro),
    )
    report = _find_report(run_id, cwd=cwd)
    resolved = _parse_resolved(report) if report else []
    err = ""
    if result.returncode != 0:
        err = (result.stderr or result.stdout or f"wsl harness exit {result.returncode}")[:8000]
    return HarnessResult(
        ok=result.returncode == 0,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        report_path=report,
        resolved_ids=resolved,
        error=err,
        backend="wsl",
    )


def filter_predictions_with_patch(
    predictions_path: Path | str,
    *,
    instance_ids: list[str] | None = None,
    out_path: Path | str | None = None,
    require_verified: bool = True,
) -> tuple[Path, list[str]]:
    """写出可用于 harness 的 JSONL；返回 (path, kept_ids)。

    默认 ``require_verified=True``：仅保留非空且 ``verified=true`` 的行
    （FixLoop verifier 通过后才进官方评测）。
    """
    from src.benchmark.swebench.patch_export import normalize_patch_lf

    src = Path(predictions_path)
    rows = []
    for line in src.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    kept = []
    for row in rows:
        iid = str(row.get("instance_id") or "")
        if instance_ids and iid not in instance_ids:
            continue
        patch = normalize_patch_lf(str(row.get("model_patch") or ""))
        if not patch.strip():
            continue
        if require_verified and not bool(row.get("verified")):
            continue
        row = dict(row)
        row["model_patch"] = patch
        kept.append(row)
    dest = Path(out_path) if out_path else src.with_name(src.stem + ".harness.jsonl")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in kept) + ("\n" if kept else ""),
        encoding="utf-8",
    )
    return dest, [str(r["instance_id"]) for r in kept]


def _find_report(run_id: str, *, cwd: Path) -> Path | None:
    candidates = list(cwd.rglob(f"*{run_id}*.json"))
    for p in candidates:
        if "report" in p.name.lower() or "result" in p.name.lower():
            return p
    return candidates[0] if candidates else None


def _parse_resolved(report_path: Path) -> list[str]:
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        for key in ("resolved_ids", "resolved", "resolved_instances"):
            val = data.get(key)
            if isinstance(val, list):
                return [str(x) for x in val]
        resolved = []
        for k, v in data.items():
            if isinstance(v, bool) and v:
                resolved.append(str(k))
            if isinstance(v, dict) and v.get("resolved") is True:
                resolved.append(str(k))
        return resolved
    return []
