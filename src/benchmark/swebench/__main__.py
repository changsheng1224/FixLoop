"""CLI：``python -m src.benchmark.swebench``。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.benchmark.swebench.dev_instances import DEV_INSTANCE_IDS
from src.benchmark.swebench.runner import AdapterConfig, SweBenchAdapter

DJANGO_SMOKE_ID = "django__django-11099"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.benchmark.swebench",
        description="SWE-bench Lite Benchmark Adapter v1",
    )
    p.add_argument(
        "--output-dir",
        default="artifacts/swebench_lite_dev",
        help="Manifest / predictions / report 输出目录",
    )
    p.add_argument(
        "--work-root",
        default="artifacts/swebench_repos",
        help="clone/checkout 工作根目录",
    )
    p.add_argument(
        "--instances-jsonl",
        default="",
        help="本地实例 JSONL（跳过 HuggingFace）",
    )
    p.add_argument(
        "--instance-ids",
        nargs="*",
        default=None,
        help="覆盖默认 DEV 5 题；默认使用固定 DEV_INSTANCE_IDS",
    )
    p.add_argument("--limit", type=int, default=0, help="仅跑前 N 题（在选定 ids 上截断）")
    p.add_argument("--dry-run", action="store_true", help="只写 Manifest + 转换 Issue，不 clone/repair")
    p.add_argument("--fake", action="store_true", help="使用 FakeGoldPatchOrchestrator（无 API）")
    p.add_argument("--skip-clone", action="store_true", help="不 clone，使用 work-root 已有目录")
    p.add_argument("--run-harness", action="store_true", help="repair 后调用官方/WSL harness")
    p.add_argument(
        "--harness-only",
        action="store_true",
        help="不对 Agent，只对已有 predictions.jsonl 跑 harness（P0）",
    )
    p.add_argument(
        "--reexport",
        action="store_true",
        help="binary-safe 重导出 snapshots→predictions（P0）",
    )
    p.add_argument(
        "--django-smoke",
        action="store_true",
        help="快捷：仅 django__django-11099 + harness-only（需先有 predictions）",
    )
    p.add_argument(
        "--probe-wsl",
        action="store_true",
        help="探测 WSL 发行版 / python3 / resource 是否可用",
    )
    p.add_argument(
        "--harness-backend",
        default="auto",
        choices=("auto", "native", "wsl"),
        help="harness 后端（Windows 默认 auto→wsl）",
    )
    p.add_argument("--wsl-distro", default="", help="覆盖 FIXLOOP_WSL_DISTRO")
    p.add_argument(
        "--skip-verify",
        action="store_true",
        help="repair 阶段跳过 FixLoop verify（默认关闭：会跑 verify）",
    )
    p.add_argument(
        "--allow-unverified-harness",
        action="store_true",
        help="允许未 verify 的 patch 进入官方 harness（默认禁止；与 --skip-verify 联用或旧 predictions）",
    )
    p.add_argument("--model", default="", help="模型名；默认读 DEEPSEEK_MODEL / 内置默认")
    p.add_argument(
        "--provider",
        default="anthropic_compat",
        help="fake | anthropic_compat | openai | ollama",
    )
    p.add_argument("--model-name", default="fixloop", help="predictions 中的 model_name_or_path")
    p.add_argument("--max-retries", type=int, default=1)
    p.add_argument("--repair-timeout-s", type=int, default=900)
    p.add_argument("--max-workers", type=int, default=1)
    p.add_argument(
        "--prepare-images",
        action="store_true",
        help="经 WSL 预构建官方评测镜像（已存在则跳过，可复用）",
    )
    p.add_argument(
        "--list-images",
        action="store_true",
        help="列出本机 SWE-bench 相关 Docker 镜像",
    )
    p.add_argument(
        "--force-rebuild-images",
        action="store_true",
        help="与 --prepare-images 联用：强制重建镜像",
    )
    p.add_argument(
        "--predictions",
        default="",
        help="harness-only 时指定 predictions.jsonl（默认 output-dir/predictions.jsonl）",
    )
    return p


def _make_factory(args):
    if args.fake or args.provider == "fake":
        from src.benchmark.swebench.fake import FakeGoldPatchOrchestrator

        def factory(repo: str):
            return FakeGoldPatchOrchestrator(repo, gold_patch="")

        return factory

    from agent_runtime.bootstrap import create_model_client, load_dotenv
    from src.repair_factory import make_orchestrator_factory

    load_dotenv()
    client = create_model_client(
        provider=args.provider,
        model=args.model or None,
    )
    return make_orchestrator_factory(
        model_client=client,
        skip_verify=bool(args.skip_verify),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.probe_wsl:
        from src.benchmark.swebench.wsl_util import probe_wsl

        probe = probe_wsl()
        print(json.dumps(probe.__dict__, ensure_ascii=False, indent=2))
        return 0 if probe.available else 2

    if args.list_images:
        from src.benchmark.swebench.prepare_images import list_swebench_images_wsl

        payload = list_swebench_images_wsl()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("ok") else 2

    if args.prepare_images:
        from src.benchmark.swebench.prepare_images import prepare_images_wsl

        ids = list(args.instance_ids) if args.instance_ids else list(DEV_INSTANCE_IDS)
        if args.django_smoke:
            ids = [DJANGO_SMOKE_ID]
        if args.limit and args.limit > 0:
            ids = ids[: args.limit]
        payload = prepare_images_wsl(
            ids,
            force_rebuild=args.force_rebuild_images,
            max_workers=args.max_workers,
        )
        print(json.dumps({k: payload[k] for k in payload if k not in ("stdout_tail", "stderr_tail")}, indent=2))
        if payload.get("stdout_tail"):
            print("--- stdout ---")
            print(payload["stdout_tail"])
        if payload.get("stderr_tail") and not payload.get("ok"):
            print("--- stderr ---")
            print(payload["stderr_tail"])
        return 0 if payload.get("ok") else 1

    if args.reexport:
        from src.benchmark.swebench.reexport import reexport_from_snapshots

        summary = reexport_from_snapshots(
            output_dir=Path(args.output_dir),
            work_root=Path(args.work_root),
            model_name_or_path=args.model_name,
        )
        print(
            json.dumps(
                {
                    "nonempty_patches": summary.get("nonempty_patches"),
                    "patches_changed": summary.get("patches_changed"),
                    "instances": summary.get("instances"),
                },
                indent=2,
            )
        )
        print(f"reexport: {Path(args.output_dir) / 'reexport_report.json'}")
        return 0

    ids = list(args.instance_ids) if args.instance_ids else list(DEV_INSTANCE_IDS)
    if args.django_smoke:
        ids = [DJANGO_SMOKE_ID]
        args.harness_only = True
        # 基础设施冒烟：旧 predictions 可能无 verified 字段
        args.allow_unverified_harness = True
    if args.limit and args.limit > 0:
        ids = ids[: args.limit]

    cfg = AdapterConfig(
        output_dir=Path(args.output_dir),
        work_root=Path(args.work_root),
        instances_jsonl=Path(args.instances_jsonl) if args.instances_jsonl else None,
        instance_ids=ids,
        model_name=args.model_name,
        provider=args.provider,
        model=args.model,
        max_retries=args.max_retries,
        repair_timeout_s=args.repair_timeout_s,
        run_harness=args.run_harness,
        dry_run=args.dry_run,
        skip_clone=args.skip_clone,
        skip_verify=bool(args.skip_verify),
        allow_unverified_harness=bool(args.allow_unverified_harness),
        max_workers=args.max_workers,
        harness_backend=args.harness_backend,
        wsl_distro=args.wsl_distro or None,
    )

    adapter = SweBenchAdapter(
        cfg,
        orchestrator_factory=None if (args.dry_run or args.harness_only) else _make_factory(args),
    )

    if args.harness_only:
        preds = Path(args.predictions) if args.predictions else None
        report = adapter.run_harness_only(predictions_path=preds)
        print(
            json.dumps(
                {
                    "ok": report.get("ok"),
                    "failure_summary": report.get("failure_summary"),
                    "harness": {
                        k: report.get("harness", {}).get(k)
                        for k in ("ok", "backend", "error", "resolved_ids", "instance_ids")
                    },
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        print(f"harness_only: {cfg.output_dir / 'harness_only_report.json'}")
        return 0 if report.get("ok") else 2

    report = adapter.run()
    print(
        json.dumps(
            {"failure_summary": report.get("failure_summary"), "ok": report.get("ok")},
            indent=2,
        )
    )
    print(f"report: {cfg.output_dir / 'adapter_report.json'}")
    print(f"manifest: {cfg.output_dir / 'manifest.json'}")
    print(f"predictions: {cfg.output_dir / 'predictions.jsonl'}")
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
