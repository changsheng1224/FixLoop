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
from src.agents.verifier import create_verifier
from src.orchestrator import Orchestrator


def main() -> int:
    parser = argparse.ArgumentParser(prog="src.cli", description="多 Agent 代码修复")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("repair", help="执行修复")
    p.add_argument("--issue", required=True, help="Issue 描述（含堆栈）")
    p.add_argument("--repo", default=".", help="仓库路径")
    p.add_argument("--verbose", action="store_true", help="详细输出")
    p.add_argument("--dry-run", action="store_true", help="演习模式")
    p.add_argument("--skip-verify", action="store_true", help="跳过 Docker 验证")

    args = parser.parse_args()
    if args.command != "repair":
        parser.print_help()
        return 1

    return _repair(args)


def _try_create_verifier(client, ws, repo: str):
    """尝试创建 Verifier，Docker 不可用时返回 None。"""
    try:
        import docker as _docker

        _docker.from_env().ping()
    except Exception:
        return None
    try:
        return create_verifier(client, ws, cwd=repo)
    except Exception:
        return None


def _repair(args) -> int:
    """执行修复流水线。"""
    # 加载 .env
    _load_dotenv()

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic/v1")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")

    client = AnthropicCompatibleModelClient(
        model=model,
        base_url=base_url,
        api_key=api_key,
    )
    ws = WorkspaceContext.build(args.repo)
    repo = str(Path(args.repo).resolve())

    localizer = create_localizer(client, ws, cwd=repo)
    retriever = create_retriever(client, ws, cwd=repo)
    patcher = create_patcher(client, ws, cwd=repo)

    if args.dry_run:
        localizer.dry_run = True
        retriever.dry_run = True
        patcher.dry_run = True
        print("⚠ DRY-RUN MODE", file=sys.stderr)

    orch = Orchestrator(localizer, retriever, patcher)

    # Verifier: 需要 Docker 环境，检测不可用时自动跳过
    if not args.skip_verify:
        verifier = _try_create_verifier(client, ws, args.repo)
        if verifier:
            orch.verifier = verifier
            if args.verbose:
                print("[Orchestrator] Verifier 已接入 (Docker)", file=sys.stderr)
        elif args.verbose:
            print("[Orchestrator] Docker 不可用，跳过验证", file=sys.stderr)

    if args.verbose:
        print("[Orchestrator] 开始修复...", file=sys.stderr)

    state = orch.repair(args.issue)

    if args.verbose:
        plan = state.repair_plan
        print(
            f"[Orchestrator] 识别: {plan.language}, {plan.issue_type}, {plan.suspect_files}",
            file=sys.stderr,
        )
        n_suspects = len(state.suspect_locations)
        print(f"[Localizer] 定位 {n_suspects} 个嫌疑位置", file=sys.stderr)
        if state.retrieved_context:
            n_tests = len(state.retrieved_context.related_tests)
            print(f"[Retriever] 找到 {n_tests} 个相关测试", file=sys.stderr)
        n_patches = len(state.candidate_patches)
        print(f"[Patcher] 生成 {n_patches} 个补丁", file=sys.stderr)
        print("--- Timing ---", file=sys.stderr)
        wall = state.node_timings.get("localize_retrieve_ms")
        if wall:
            print(f"  localize+retrieve (parallel wall): {wall}ms", file=sys.stderr)
        for agent in ("localizer", "retriever", "patcher", "verifier"):
            total = state.node_timings.get(f"{agent}_ms", 0)
            if total == 0:
                continue
            intern = state.node_timings.get(f"{agent}_internal", {})
            pb = intern.get("prompt_build_ms", 0)
            mc = intern.get("model_call_ms", 0)
            te = intern.get("tool_exec_ms", 0)
            print(
                f"  {agent}: {total}ms (prompt={pb}ms, model={mc}ms, tool={te}ms)",
                file=sys.stderr,
            )

    # 输出结果
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
