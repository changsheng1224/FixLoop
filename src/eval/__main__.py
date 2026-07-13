"""CLI: python -m src.eval.runner"""

from __future__ import annotations

import argparse
import sys

from agent_runtime.logging_setup import add_log_level_argument, setup_logging_from_args
from src.eval.cli_helpers import print_eval_report, run_eval
from src.eval.runner import DEFAULT_CASES_DIR


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="src.eval.runner", description="FixLoop 评测 Runner")
    parser.add_argument("--all", action="store_true", help="运行全部 Case")
    parser.add_argument("--case", action="append", dest="cases", help="指定 case_id，可重复")
    parser.add_argument("--cases-dir", default=str(DEFAULT_CASES_DIR), help="Case 根目录")
    parser.add_argument("--output", default="eval_results", help="报告目录或 .json 路径")
    parser.add_argument("--verbose", action="store_true", help="打印每个 Case 摘要")
    add_log_level_argument(parser)
    parser.add_argument(
        "--fake",
        action="store_true",
        help="Fake Orchestrator（应用 expected_patch，无需 API）",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="禁用 Verifier 重试（默认启用 pytest/Docker 验证）",
    )
    parser.add_argument(
        "--with-verify",
        action="store_true",
        help="（已默认启用）显式开启 Verifier",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI 模式：--all --fake --skip-verify，输出到 eval_results/ci",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="断点续跑：跳过已完成的 case（读取 eval_results/.checkpoint.json）",
    )
    parser.add_argument(
        "--pass-at-k",
        type=int,
        default=1,
        metavar="K",
        help="Pass@k 评测：每个 case 重复 K 次（默认 1）。报告 pass@1/pass@3",
    )
    parser.add_argument(
        "--with-judge",
        action="store_true",
        help="启用 LLM-as-Judge 评分（需 API key）",
    )
    args = parser.parse_args(argv)
    setup_logging_from_args(args)

    if args.ci:
        args.all = True
        args.fake = True
        args.skip_verify = True
        if args.output == "eval_results":
            args.output = "eval_results/ci"

    if not args.all and not args.cases:
        parser.error("请指定 --all、--case case_XXX 或 --ci")

    if not args.fake and not args.ci:
        print("提示: 未指定 --fake 时将调用真实 API（费用较高）", file=sys.stderr)

    report, report_path, code = run_eval(
        case_ids=None if args.all else args.cases,
        cases_dir=args.cases_dir,
        output=args.output,
        verbose=args.verbose,
        fake=args.fake,
        skip_verify=args.skip_verify,
        resume=args.resume,
        pass_at_k=args.pass_at_k,
        with_judge=args.with_judge,
    )
    print_eval_report(report, verbose=args.verbose, report_path=report_path)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
