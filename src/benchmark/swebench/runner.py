"""SWE-bench Lite 开发跑批：checkout → repair → predictions → harness。"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from src.benchmark.swebench.classify import (
    classify_failure,
    classify_post_repair,
    summarize_failures,
)
from src.benchmark.swebench.convert import instance_to_issue
from src.benchmark.swebench.dataset import DatasetError, load_instances
from src.benchmark.swebench.dev_instances import DATASET_NAME, DEV_INSTANCE_IDS
from src.benchmark.swebench.harness import run_official_harness
from src.benchmark.swebench.manifest import build_manifest, write_manifest
from src.benchmark.swebench.patch_export import export_model_patch
from src.benchmark.swebench.predictions import write_predictions_jsonl
from src.benchmark.swebench.repo_prep import RepoPrepError, preflight_repo, prepare_repo
from src.benchmark.swebench.types import FailureClass, InstanceResult, SweInstance


@dataclass
class AdapterConfig:
    output_dir: Path
    work_root: Path
    instances_jsonl: Path | None = None
    instance_ids: list[str] = field(default_factory=lambda: list(DEV_INSTANCE_IDS))
    model_name: str = "fixloop"
    provider: str = "fake"
    model: str = "fake"
    max_retries: int = 1
    repair_timeout_s: int = 600
    run_harness: bool = False
    dry_run: bool = False
    skip_clone: bool = False
    skip_verify: bool = False
    allow_unverified_harness: bool = False
    dataset_name: str = DATASET_NAME
    max_workers: int = 1
    harness_backend: str = "auto"  # auto | native | wsl
    wsl_distro: str | None = None
    harness_only_with_patch: bool = True
    allow_gold_patch_injection: bool = False


class SweBenchAdapter:
    """Benchmark Adapter v1。"""

    def __init__(
        self,
        config: AdapterConfig,
        orchestrator_factory: Callable[[str], object] | None = None,
    ):
        self.config = config
        self.orchestrator_factory = orchestrator_factory

    def run(self) -> dict:
        cfg = self.config
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        cfg.work_root.mkdir(parents=True, exist_ok=True)
        manifest = build_manifest(
            instance_ids=cfg.instance_ids,
            model=cfg.model,
            provider=cfg.provider,
            max_retries=cfg.max_retries,
            repair_timeout_s=cfg.repair_timeout_s,
            harness_dataset=cfg.dataset_name,
            extra={
                "dry_run": cfg.dry_run,
                "run_harness": cfg.run_harness,
                "skip_verify": cfg.skip_verify,
                "allow_unverified_harness": cfg.allow_unverified_harness,
                "repair_runtime": "patcher_v2",
                "baseline_policy": "strict_preflight_v1",
                "gold_patch_visibility": "assisted"
                if cfg.allow_gold_patch_injection
                else "strict",
            },
        )
        write_manifest(cfg.output_dir / "manifest.json", manifest)

        try:
            instances = load_instances(
                instances_jsonl=cfg.instances_jsonl,
                dataset_name=cfg.dataset_name,
                instance_ids=cfg.instance_ids,
            )
        except DatasetError as e:
            report = {
                "ok": False,
                "error": str(e),
                "failure_summary": {FailureClass.ENV.value: len(cfg.instance_ids)},
                "results": [],
            }
            self._write_report(report)
            return report

        from src.repair.progress import progress_emitter_from_env

        progress = progress_emitter_from_env()
        results: list[InstanceResult] = []
        for idx, inst in enumerate(instances, 1):
            progress.emit(
                "instance_progress",
                summary=f"({idx}/{len(instances)}) start {inst.instance_id}",
            )
            results.append(self._run_one(inst))
            r = results[-1]
            progress.emit(
                "instance_progress",
                summary=(
                    f"({idx}/{len(instances)}) done {inst.instance_id} "
                    f"class={r.failure_class} patch_bytes={len(r.model_patch)} "
                    f"ms={r.duration_ms}"
                ),
            )

        preds_path = write_predictions_jsonl(cfg.output_dir / "predictions.jsonl", results)

        harness_meta: dict = {}
        if cfg.run_harness and not cfg.dry_run:
            harness_meta = self._apply_harness(results, preds_path)

        report = {
            "ok": True,
            "manifest": str(cfg.output_dir / "manifest.json"),
            "predictions": str(preds_path),
            "failure_summary": summarize_failures(results),
            "harness": harness_meta,
            "results": [r.to_dict() for r in results],
        }
        self._write_report(report)
        # per-instance artifacts
        for r in results:
            case_dir = cfg.output_dir / "instances" / r.instance_id
            case_dir.mkdir(parents=True, exist_ok=True)
            (case_dir / "result.json").write_text(
                json.dumps(r.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            if r.model_patch:
                (case_dir / "model_patch.diff").write_text(r.model_patch, encoding="utf-8")
        return report

    def _apply_harness(self, results: list[InstanceResult], preds_path: Path) -> dict:
        """仅对 verifier 通过的 patch 调用官方/WSL harness，回写 results。"""
        from src.benchmark.swebench.harness import filter_predictions_with_patch

        cfg = self.config
        if cfg.skip_verify and not cfg.allow_unverified_harness:
            return {
                "ok": False,
                "returncode": 2,
                "error": (
                    "harness blocked: --skip-verify without --allow-unverified-harness "
                    "(verify must pass before official eval)"
                ),
                "report_path": "",
                "resolved_ids": [],
                "backend": "none",
                "predictions_used": str(preds_path),
            }

        ids = [r.instance_id for r in results]
        eval_path = preds_path
        eval_ids = ids
        require_verified = not cfg.allow_unverified_harness
        if cfg.harness_only_with_patch:
            eval_path, eval_ids = filter_predictions_with_patch(
                preds_path,
                instance_ids=ids,
                out_path=cfg.output_dir / "predictions.harness.jsonl",
                require_verified=require_verified,
            )
            if not eval_ids:
                msg = (
                    "no verified model_patch to evaluate (FixLoop verify must pass first)"
                    if require_verified
                    else "no nonempty model_patch to evaluate"
                )
                return {
                    "ok": False,
                    "returncode": 2,
                    "error": msg,
                    "report_path": "",
                    "resolved_ids": [],
                    "backend": "none",
                    "predictions_used": str(eval_path),
                }

        hr = run_official_harness(
            eval_path,
            run_id=f"fixloop-{int(time.time())}",
            dataset_name=cfg.dataset_name,
            instance_ids=eval_ids,
            max_workers=cfg.max_workers,
            cwd=cfg.output_dir,
            backend=cfg.harness_backend,
            wsl_distro=cfg.wsl_distro,
        )
        meta = {
            "ok": hr.ok,
            "returncode": hr.returncode,
            "error": hr.error,
            "report_path": str(hr.report_path) if hr.report_path else "",
            "resolved_ids": hr.resolved_ids or [],
            "backend": hr.backend,
            "predictions_used": str(eval_path),
            "instance_ids": eval_ids,
            "require_verified": require_verified,
        }
        resolved_set = set(hr.resolved_ids or [])
        evaluated = set(eval_ids)
        for r in results:
            if r.instance_id not in evaluated:
                continue
            if r.failure_class == FailureClass.AGENT and not r.model_patch.strip():
                continue
            if hr.error and not hr.ok and not resolved_set:
                r.resolved = False
                env_hints = (
                    "not installed",
                    "requires Unix",
                    "resource module",
                    "WSL",
                    "Modal",
                    "no suitable WSL",
                    "missing swebench",
                    "Network is unreachable",
                    "ProxyError",
                    "huggingface.co",
                )
                is_env = any(h in hr.error for h in env_hints)
                r.failure_class = FailureClass.ENV if is_env else FailureClass.EVAL
                r.failure_detail = hr.error
                r.harness_log = hr.error
            else:
                r.resolved = r.instance_id in resolved_set
                fc, detail = classify_failure(
                    model_patch=r.model_patch,
                    resolved=r.resolved,
                    harness_error="" if r.resolved else "not_resolved",
                )
                r.failure_class = fc
                r.failure_detail = detail
        return meta

    def run_harness_only(self, *, predictions_path: Path | None = None) -> dict:
        """仅对已有 predictions 跑 harness（P0 django 冒烟）。"""
        cfg = self.config
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        preds = Path(predictions_path or (cfg.output_dir / "predictions.jsonl"))
        if not preds.is_file():
            return {"ok": False, "error": f"predictions not found: {preds}"}

        from src.benchmark.swebench.predictions import read_predictions_jsonl

        rows = read_predictions_jsonl(preds)
        want = set(cfg.instance_ids) if cfg.instance_ids else None
        results = [
            InstanceResult(
                instance_id=str(r.get("instance_id") or ""),
                model_name_or_path=str(r.get("model_name_or_path") or cfg.model_name),
                model_patch=str(r.get("model_patch") or ""),
                verified=bool(r.get("verified")),
                failure_class=(
                    FailureClass.NONE
                    if (str(r.get("model_patch") or "").strip() and bool(r.get("verified")))
                    else FailureClass.AGENT
                ),
                failure_detail=(
                    "pending_harness"
                    if (str(r.get("model_patch") or "").strip() and bool(r.get("verified")))
                    else (
                        "unverified_patch"
                        if str(r.get("model_patch") or "").strip()
                        else "empty_model_patch"
                    )
                ),
            )
            for r in rows
            if not want or str(r.get("instance_id") or "") in want
        ]
        meta = self._apply_harness(results, preds)
        report = {
            "ok": bool(meta.get("ok")),
            "mode": "harness_only",
            "failure_summary": summarize_failures(results),
            "harness": meta,
            "results": [r.to_dict() for r in results],
        }
        (cfg.output_dir / "harness_only_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return report

    def _write_report(self, report: dict) -> None:
        path = self.config.output_dir / "adapter_report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _run_one(self, inst: SweInstance) -> InstanceResult:
        cfg = self.config
        result = InstanceResult(
            instance_id=inst.instance_id,
            model_name_or_path=cfg.model_name,
        )
        t0 = time.time()

        if cfg.dry_run:
            issue = instance_to_issue(inst)
            result.model_patch = ""
            result.repair_status = "dry_run"
            result.trace_hint = f"issue_chars={len(issue)}"
            result.duration_ms = int((time.time() - t0) * 1000)
            result.failure_class = FailureClass.AGENT
            result.failure_detail = "dry_run_no_patch"
            return result

        repo_path: Path | None = None
        try:
            if cfg.skip_clone:
                # 使用 work_root 下已有目录（测试/本地预置）
                candidate = cfg.work_root / inst.instance_id.replace("/", "__")
                if not candidate.is_dir():
                    raise RepoPrepError(f"skip_clone but missing repo: {candidate}")
                repo_path = candidate
            else:
                repo_path = prepare_repo(inst, cfg.work_root)
            result.repo_path = str(repo_path)
        except RepoPrepError as e:
            result.failure_class = FailureClass.ENV
            result.error = str(e)
            result.failure_detail = str(e)
            result.duration_ms = int((time.time() - t0) * 1000)
            return result

        preflight = preflight_repo(repo_path, base_commit=inst.base_commit)
        result.baseline_preflight = preflight
        if not preflight.get("ok", False):
            reasons = preflight.get("reasons") or []
            detail = ",".join(str(r) for r in reasons) if reasons else "baseline_dirty"
            result.failure_class = FailureClass.BASELINE_DIRTY
            result.error = "baseline_dirty"
            result.failure_detail = detail
            result.duration_ms = int((time.time() - t0) * 1000)
            return result

        # 快照 original（用于 diff）；Agent 直接改 worktree
        original_snap = cfg.output_dir / "snapshots" / inst.instance_id / "original"
        if original_snap.exists():
            shutil.rmtree(original_snap, ignore_errors=True)
        shutil.copytree(
            repo_path,
            original_snap,
            ignore=shutil.ignore_patterns(".git", ".agent", "__pycache__", ".pytest_cache"),
        )

        if self.orchestrator_factory is None:
            result.failure_class = FailureClass.AGENT
            result.error = "no_orchestrator_factory"
            result.failure_detail = "no_orchestrator_factory"
            result.duration_ms = int((time.time() - t0) * 1000)
            return result

        try:
            orch = self.orchestrator_factory(str(repo_path))
            # 若工厂支持 set_gold（测试辅助）
            if cfg.allow_gold_patch_injection and inst.patch:
                setter = getattr(orch, "set_gold_patch", None)
                if callable(setter):
                    setter(inst.patch)
                elif hasattr(orch, "_gold") and inst.patch:
                    orch._gold = inst.patch
            issue = instance_to_issue(inst)
            state = orch.repair(
                issue,
                max_retries=cfg.max_retries,
                repair_timeout_s=cfg.repair_timeout_s,
                verify_test_patch=str(getattr(inst, "test_patch", "") or ""),
            )
            result.repair_status = str(getattr(state, "status", "") or "")
            result.repair_run_id = str(getattr(state, "repair_run_id", "") or "")
            if result.repair_run_id:
                result.trace_hint = f".agent/runs or repairs/{result.repair_run_id}"
            vr = getattr(state, "verification_result", None)
            result.verified = bool(vr is not None and getattr(vr, "all_passed", False))
            result.model_patch = export_model_patch(
                state=state,
                original_repo=original_snap,
                modified_repo=repo_path,
            )
            fc, detail = classify_post_repair(
                model_patch=result.model_patch,
                repair_status=result.repair_status,
                verified=result.verified,
                skip_verify=cfg.skip_verify,
            )
            result.failure_class = fc
            result.failure_detail = detail
            if detail in (
                "empty_model_patch",
                "unverified_patch",
                "patch_without_fixed_status",
                "invalid_patch_format",
            ):
                result.error = detail
            elif not result.model_patch.strip():
                result.error = "empty_model_patch"
        except Exception as e:  # noqa: BLE001
            result.failure_class = FailureClass.AGENT
            result.error = str(e)
            result.failure_detail = str(e)

        result.duration_ms = int((time.time() - t0) * 1000)
        return result
