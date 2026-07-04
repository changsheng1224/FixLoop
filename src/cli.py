"""Layer 2 CLI：多 Agent 修复与评测命令。"""

import argparse
import sys
from pathlib import Path

from agent_runtime.bootstrap import load_dotenv
from src.eval.cli_helpers import print_ablation_report, print_eval_report, run_ablation, run_eval
from src.eval.runner import DEFAULT_CASES_DIR
from src.repair_factory import make_orchestrator_factory


def main() -> int:
    parser = argparse.ArgumentParser(prog="src.cli", description="多 Agent 代码修复")
    sub = parser.add_subparsers(dest="command")

    p_repair = sub.add_parser("repair", help="执行修复")
    p_repair.add_argument("--issue", required=True, help="Issue 描述（含堆栈）")
    p_repair.add_argument("--repo", default=".", help="仓库路径")
    p_repair.add_argument("--verbose", action="store_true", help="详细输出")
    p_repair.add_argument("--dry-run", action="store_true", help="演习模式")
    p_repair.add_argument("--skip-verify", action="store_true", help="跳过 Docker 验证")

    p_eval = sub.add_parser("eval", help="运行评测 Case")
    p_eval.add_argument("--all", action="store_true", help="运行全部 Case")
    p_eval.add_argument("--case", action="append", dest="cases", help="指定 case_id")
    p_eval.add_argument("--cases-dir", default=str(DEFAULT_CASES_DIR), help="Case 目录")
    p_eval.add_argument("--output", default="eval_results", help="报告目录或 .json 路径")
    p_eval.add_argument("--verbose", action="store_true", help="打印每个 Case 详情")
    p_eval.add_argument(
        "--fake",
        action="store_true",
        help="Fake Orchestrator（应用 expected_patch，无需 API）",
    )
    p_eval.add_argument(
        "--skip-verify",
        action="store_true",
        help="禁用 Verifier 重试（默认启用 pytest/Docker 验证）",
    )
    p_eval.add_argument(
        "--with-verify",
        action="store_true",
        help="（已默认启用）显式开启 Verifier 重试",
    )
    p_eval.add_argument(
        "--markdown",
        nargs="?",
        const="report.md",
        metavar="PATH",
        help="生成 Markdown 指标报告（默认 output/report.md）",
    )

    p_ablation = sub.add_parser("ablation", help="运行消融实验")
    p_ablation.add_argument("--all", action="store_true", help="运行全部 Case")
    p_ablation.add_argument("--case", action="append", dest="cases", help="指定 case_id")
    p_ablation.add_argument("--cases-dir", default=str(DEFAULT_CASES_DIR), help="Case 目录")
    p_ablation.add_argument("--output", default="eval_results", help="报告目录或 .json 路径")
    p_ablation.add_argument("--verbose", action="store_true", help="打印每个变体摘要")
    p_ablation.add_argument(
        "--fake",
        action="store_true",
        help="Fake Orchestrator（应用 expected_patch，无需 API）",
    )
    p_ablation.add_argument(
        "--skip-verify",
        action="store_true",
        help="禁用 Verifier 重试（默认启用 pytest/Docker 验证）",
    )
    p_ablation.add_argument(
        "--with-verify",
        action="store_true",
        help="（已默认启用）显式开启 Verifier 重试",
    )
    p_ablation.add_argument(
        "--repetitions",
        type=int,
        default=3,
        help="每个变体 × Case 重复次数（默认 3）",
    )
    p_ablation.add_argument(
        "--variant",
        action="append",
        dest="variants",
        choices=["full", "single", "no_retriever"],
        help="指定变体（可多次指定；默认全部）",
    )
    p_ablation.add_argument(
        "--no-progress",
        action="store_true",
        help="不打印逐条运行进度",
    )
    p_ablation.add_argument(
        "--markdown",
        nargs="?",
        const="report.md",
        metavar="PATH",
        help="生成 Markdown 指标报告（默认 output/report.md）",
    )

    args = parser.parse_args()
    if args.command == "repair":
        return _repair(args)
    if args.command == "eval":
        return _eval(args)
    if args.command == "ablation":
        return _ablation(args)
    parser.print_help()
    return 1


def _repair(args) -> int:
    load_dotenv()
    factory = make_orchestrator_factory(skip_verify=args.skip_verify, dry_run=args.dry_run)
    repo = str(Path(args.repo).resolve())
    orch = factory(repo)

    if args.dry_run:
        print("⚠ DRY-RUN MODE", file=sys.stderr)
    if args.verbose:
        if orch.verifier:
            print("[Orchestrator] Verifier 已接入 (Docker)", file=sys.stderr)
        elif not args.skip_verify:
            print("[Orchestrator] Docker 不可用，跳过验证", file=sys.stderr)
        print("[Orchestrator] 开始修复...", file=sys.stderr)

    state = orch.repair(args.issue)
    _print_repair_result(state, verbose=args.verbose)
    return 0


def _eval(args) -> int:
    if not args.all and not args.cases:
        print("错误: 请指定 --all 或 --case case_XXX", file=sys.stderr)
        return 2

    skip_verify = args.skip_verify
    case_ids = None if args.all else args.cases
    report, report_path, code = run_eval(
        case_ids=case_ids,
        cases_dir=args.cases_dir,
        output=args.output,
        verbose=args.verbose,
        fake=args.fake,
        skip_verify=skip_verify,
        markdown=getattr(args, "markdown", None),
    )
    print_eval_report(report, verbose=args.verbose, report_path=report_path)
    return code


def _ablation(args) -> int:
    if not args.all and not args.cases:
        print("错误: 请指定 --all 或 --case case_XXX", file=sys.stderr)
        return 2

    load_dotenv()
    skip_verify = args.skip_verify
    case_ids = None if args.all else args.cases
    report, report_path, code = run_ablation(
        case_ids=case_ids,
        cases_dir=args.cases_dir,
        output=args.output,
        verbose=args.verbose,
        fake=args.fake,
        skip_verify=skip_verify,
        repetitions=args.repetitions,
        variant_names=args.variants,
        progress=not args.no_progress,
        markdown=getattr(args, "markdown", None),
    )
    print_ablation_report(report, verbose=args.verbose, report_path=report_path)
    return code


def _print_repair_result(state, verbose: bool) -> None:
    if verbose:
        plan = state.repair_plan
        print(
            f"[Orchestrator] 识别: {plan.language}, {plan.issue_type}, {plan.suspect_files}",
            file=sys.stderr,
        )
        print(f"[Localizer] 定位 {len(state.suspect_locations)} 个嫌疑位置", file=sys.stderr)
        if state.retrieved_context:
            print(
                f"[Retriever] 找到 {len(state.retrieved_context.related_tests)} 个相关测试",
                file=sys.stderr,
            )
        print(f"[Patcher] 生成 {len(state.candidate_patches)} 个补丁", file=sys.stderr)
        print("--- Timing ---", file=sys.stderr)
        wall = state.node_timings.get("localize_retrieve_ms")
        if wall:
            print(f"  localize+retrieve (parallel wall): {wall}ms", file=sys.stderr)
        for agent in ("localizer", "retriever", "patcher", "verifier"):
            total = state.node_timings.get(f"{agent}_ms", 0)
            if total == 0:
                continue
            intern = state.node_timings.get(f"{agent}_internal", {})
            print(
                f"  {agent}: {total}ms "
                f"(prompt={intern.get('prompt_build_ms', 0)}ms, "
                f"model={intern.get('model_call_ms', 0)}ms, "
                f"tool={intern.get('tool_exec_ms', 0)}ms)",
                file=sys.stderr,
            )

    if state.status in ("fixed", "patched") and state.candidate_patches:
        emoji = "✅" if state.status == "fixed" else "⚠"
        print(f"\n{emoji} 修复完成! 状态={state.status}")
        for patch in state.candidate_patches:
            print(f"\n--- {patch.file_path} ---")
            if patch.diff:
                print(patch.diff)
            if patch.explanation:
                print(f"说明: {patch.explanation}")
        if state.verification_result:
            vr = state.verification_result
            print(
                f"\n验证: {vr.passed}/{vr.total_tests} 通过"
                + (f", {vr.failed} 失败" if vr.failed else "")
            )
    else:
        print(f"❌ 修复未完成 (status={state.status})")


if __name__ == "__main__":
    sys.exit(main())
