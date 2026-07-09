"""Layer 2 CLI：多 Agent 修复与评测命令。"""

import argparse
import sys
from pathlib import Path

from agent_runtime.bootstrap import load_dotenv
from agent_runtime.logging_setup import add_log_level_argument, setup_logging_from_args
from src.cli_exit_codes import (
    REPAIR_EXIT_CONFIG,
    repair_config_error,
    repair_exit_code,
)
from src.eval.cli_helpers import print_ablation_report, print_eval_report, run_ablation, run_eval
from src.eval.runner import DEFAULT_CASES_DIR
from src.repair_factory import make_orchestrator_factory


def main() -> int:
    parser = argparse.ArgumentParser(prog="src.cli", description="多 Agent 代码修复")
    add_log_level_argument(parser)
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
    setup_logging_from_args(args)
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
    repo = str(Path(args.repo).resolve())
    config_err = repair_config_error(repo)
    if config_err:
        print(config_err, file=sys.stderr)
        return REPAIR_EXIT_CONFIG

    try:
        factory = make_orchestrator_factory(skip_verify=args.skip_verify, dry_run=args.dry_run)
        orch = factory(repo)
    except Exception as exc:
        print(f"错误: 配置/初始化失败: {exc}", file=sys.stderr)
        return REPAIR_EXIT_CONFIG

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
    return repair_exit_code(state)


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
        from src.repair.timing_schema import get_phase_ms

        wall = state.node_timings.get("localize_retrieve_ms") or (
            state.node_timings.get("parallel_wall_ms") or {}
        ).get("localize_retrieve_ms")
        if wall:
            print(f"  localize+retrieve (parallel wall): {wall}ms", file=sys.stderr)
        phase_labels = (
            ("localize", "localizer"),
            ("retrieve", "retriever"),
            ("patch", "patcher"),
            ("verify", "verifier"),
        )
        for phase, agent in phase_labels:
            total = get_phase_ms(state.node_timings, phase)
            if total == 0:
                continue
            legacy_internal = f"{agent}_internal"
            intern = (state.node_timings.get("phases_internal") or {}).get(phase) or state.node_timings.get(
                legacy_internal, {}
            )
            print(
                f"  {agent}: {total}ms "
                f"(prompt={intern.get('prompt_build_ms', 0)}ms, "
                f"model={intern.get('model_call_ms', 0)}ms, "
                f"tool={intern.get('tool_exec_ms', 0)}ms)",
                file=sys.stderr,
            )
        repair_total = get_phase_ms(state.node_timings, "repair_total")
        if repair_total:
            print(f"  repair_total: {repair_total}ms", file=sys.stderr)
        by_agent = state.node_timings.get("token_usage_by_agent") or {}
        tool_by_agent = state.node_timings.get("tool_usage_by_agent") or {}
        if by_agent:
            print("--- Token & tool usage (by agent) ---", file=sys.stderr)
            for agent, usage in sorted(by_agent.items()):
                tools = tool_by_agent.get(agent, usage.get("tool_steps", 0))
                print(
                    f"  {agent}: {usage.get('total_tokens', 0)} tokens "
                    f"(in={usage.get('input_tokens', 0)}, out={usage.get('output_tokens', 0)}), "
                    f"tools={tools}",
                    file=sys.stderr,
                )
            total = state.node_timings.get("total_tokens", 0)
            if total:
                print(f"  total: {total} tokens", file=sys.stderr)
            total_tools = state.node_timings.get("total_tool_steps")
            if total_tools is not None:
                print(f"  total: {total_tools} tool calls", file=sys.stderr)
        if state.repair_run_id:
            print(f"--- Trace: .agent/runs/{state.repair_run_id}/trace.jsonl ---", file=sys.stderr)

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
