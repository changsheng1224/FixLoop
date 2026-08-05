"""P0：binary-safe 重导出 predictions（避免 UTF-8 假阴性）。"""

from __future__ import annotations

import json
from pathlib import Path

from src.benchmark.swebench.patch_export import collect_repo_diff_safe, normalize_patch_lf
from src.benchmark.swebench.predictions import write_predictions_jsonl
from src.benchmark.swebench.types import FailureClass, InstanceResult


def reexport_from_snapshots(
    *,
    output_dir: Path | str,
    work_root: Path | str,
    model_name_or_path: str = "fixloop",
) -> dict:
    """用 ``snapshots/<id>/original`` vs ``work_root/<id>`` 重算 patch。

    更新 ``predictions.jsonl``、``instances/*/model_patch.diff``，并写
    ``reexport_report.json``。
    """
    out = Path(output_dir)
    work = Path(work_root)
    report_path = out / "adapter_report.json"
    old_results: list[dict] = []
    if report_path.is_file():
        try:
            old_results = json.loads(report_path.read_text(encoding="utf-8")).get("results") or []
        except json.JSONDecodeError:
            old_results = []

    # 发现 instance 目录
    ids: list[str] = []
    snap_root = out / "snapshots"
    if snap_root.is_dir():
        ids = sorted(p.name for p in snap_root.iterdir() if p.is_dir())
    if not ids and old_results:
        ids = [str(r.get("instance_id") or "") for r in old_results if r.get("instance_id")]

    by_old = {str(r.get("instance_id")): r for r in old_results}
    results: list[InstanceResult] = []
    changed = 0
    for iid in ids:
        if not iid:
            continue
        original = snap_root / iid / "original"
        modified = work / iid.replace("/", "__")
        if not modified.is_dir():
            modified = work / iid
        patch = ""
        if original.is_dir() and modified.is_dir():
            # E12: 默认限文件数/字节；超限返回空，避免脏仓整 diff
            patch = collect_repo_diff_safe(original, modified)
        old = by_old.get(iid, {})
        prev = str(old.get("model_patch") or "")
        if patch.strip() and patch.strip() != prev.strip():
            changed += 1
        # 新 diff 空时保留旧 patch，但旧 patch 也须通过 size/unified 门禁
        from src.benchmark.swebench.patch_export import gate_export_size

        final_patch = gate_export_size(
            normalize_patch_lf(patch if patch.strip() else prev)
        )
        fc = FailureClass.NONE if final_patch.strip() else FailureClass.AGENT
        detail = "reexported" if final_patch.strip() else "empty_after_reexport"
        if old.get("failure_class") == "env" and final_patch.strip():
            fc = FailureClass.ENV
            detail = str(old.get("failure_detail") or detail)
        ir = InstanceResult(
            instance_id=iid,
            model_name_or_path=str(old.get("model_name_or_path") or model_name_or_path),
            model_patch=final_patch,
            resolved=old.get("resolved"),
            failure_class=fc,
            failure_detail=detail,
            duration_ms=int(old.get("duration_ms") or 0),
            repair_status=str(old.get("repair_status") or ""),
            repair_run_id=str(old.get("repair_run_id") or ""),
            repo_path=str(old.get("repo_path") or modified),
            trace_hint=str(old.get("trace_hint") or ""),
            error="",
        )
        results.append(ir)
        case_dir = out / "instances" / iid
        case_dir.mkdir(parents=True, exist_ok=True)
        if final_patch.strip():
            (case_dir / "model_patch.diff").write_text(final_patch, encoding="utf-8")
        (case_dir / "result.json").write_text(
            json.dumps(ir.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    preds = write_predictions_jsonl(out / "predictions.jsonl", results)
    summary = {
        "ok": True,
        "instances": len(results),
        "nonempty_patches": sum(1 for r in results if r.model_patch.strip()),
        "patches_changed": changed,
        "predictions": str(preds),
        "results": [r.to_dict() for r in results],
    }
    (out / "reexport_report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary
