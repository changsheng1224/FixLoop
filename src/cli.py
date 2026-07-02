"""Layer 2 CLI：多 Agent 修复命令。"""

import argparse
import os
import sys
from pathlib import Path

from agent_runtime.providers.clients import AnthropicCompatibleModelClient
from agent_runtime.workspace import WorkspaceContext
from src.agents.localizer import create_localizer
from src.agents.patcher import create_patcher
from src.agents.retriever import create_retriever
from src.orchestrator import Orchestrator


def main() -> int:
    parser = argparse.ArgumentParser(prog="src.cli", description="多 Agent 代码修复")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("repair", help="执行修复")
    p.add_argument("--issue", required=True, help="Issue 描述（含堆栈）")
    p.add_argument("--repo", default=".", help="仓库路径")
    p.add_argument("--verbose", action="store_true", help="详细输出")
    p.add_argument("--dry-run", action="store_true", help="演习模式")

    args = parser.parse_args()
    if args.command != "repair":
        parser.print_help()
        return 1

    return _repair(args)


def _repair(args) -> int:
    """执行修复流水线。"""
    # 加载 .env
    _load_dotenv()

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic/v1")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")

    client = AnthropicCompatibleModelClient(
        model=model, base_url=base_url, api_key=api_key,
    )
    ws = WorkspaceContext.build(args.repo)

    localizer = create_localizer(client, ws)
    retriever = create_retriever(client, ws)
    patcher = create_patcher(client, ws)

    if args.dry_run:
        localizer.dry_run = True
        retriever.dry_run = True
        patcher.dry_run = True
        print("⚠ DRY-RUN MODE", file=sys.stderr)

    orch = Orchestrator(localizer, retriever, patcher)

    if args.verbose:
        print("[Orchestrator] 开始修复...", file=sys.stderr)

    state = orch.repair(args.issue)

    if args.verbose:
        plan = state.repair_plan
        print(
            f"[Orchestrator] 识别: {plan.language}, {plan.issue_type}, "
            f"{plan.suspect_files}",
            file=sys.stderr,
        )
        n_suspects = len(state.suspect_locations)
        print(f"[Localizer] 定位 {n_suspects} 个嫌疑位置", file=sys.stderr)
        if state.retrieved_context:
            n_tests = len(state.retrieved_context.related_tests)
            print(f"[Retriever] 找到 {n_tests} 个相关测试", file=sys.stderr)
        n_patches = len(state.candidate_patches)
        print(f"[Patcher] 生成 {n_patches} 个补丁", file=sys.stderr)
        for k, v in state.node_timings.items():
            print(f"  {k}: {v}ms", file=sys.stderr)

    # 输出结果
    if state.status == "patched" and state.candidate_patches:
        print(f"\n✅ 修复完成! 状态={state.status}")
        for patch in state.candidate_patches:
            print(f"\n--- {patch.file_path} ---")
            if patch.diff:
                print(patch.diff)
            if patch.explanation:
                print(f"说明: {patch.explanation}")
    else:
        print(f"❌ 修复未完成 (status={state.status})")

    return 0


def _load_dotenv():
    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if key and key not in os.environ:
                os.environ[key] = val


if __name__ == "__main__":
    sys.exit(main())
