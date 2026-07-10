"""CLI 入口：命令行参数解析 + Provider 装配。"""

import argparse
import os
import sys
from pathlib import Path

from agent_runtime.bootstrap import create_model_client
from agent_runtime.bootstrap import load_dotenv as _load_dotenv
from agent_runtime.config import AgentConfig
from agent_runtime.logging_setup import add_log_level_argument, setup_logging_from_args
from agent_runtime.repl_input import read_repl_input
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


def _make_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器。默认值来自 AgentConfig。"""
    cfg = AgentConfig()
    p = argparse.ArgumentParser(prog="agent_runtime", description="手写的 LLM Agent 运行时内核")
    p.add_argument("prompt", nargs="?", default=None, help="用户输入（缺省进入 REPL 模式）")
    p.add_argument("--cwd", default=".", help="工作目录")
    p.add_argument("--provider", default=cfg.provider, help=f"模型 Provider（默认 {cfg.provider}）")
    p.add_argument("--model", default=None, help=f"模型名称（默认 {cfg.model}）")
    p.add_argument(
        "--max-steps", type=int, default=cfg.max_steps, help=f"最大工具步数（默认 {cfg.max_steps}）"
    )
    p.add_argument(
        "--tool-timeout",
        type=int,
        default=cfg.tool_timeout_s,
        help=f"单工具执行超时秒数，0=禁用（默认 {cfg.tool_timeout_s}）",
    )
    p.add_argument(
        "--step-timeout",
        type=int,
        default=cfg.step_timeout_s,
        help=f"单步 wall-clock 超时秒数，0=禁用（默认 {cfg.step_timeout_s}）",
    )
    p.add_argument(
        "--temperature",
        type=float,
        default=cfg.temperature,
        help=f"模型温度（默认 {cfg.temperature}）",
    )
    p.add_argument("--api-key", default=None, help="API Key（覆盖 .env）")
    p.add_argument("--base-url", default=None, help="API Base URL（覆盖 .env）")
    p.add_argument(
        "--approval",
        default=cfg.approval,
        choices=["auto", "ask", "never"],
        help=f"审批策略（默认 {cfg.approval}）",
    )
    p.add_argument("--quota-writes", type=int, default=20, help="每会话最大写入次数（默认 20）")
    p.add_argument(
        "--quota-shell", type=int, default=10, help="每会话最大 Shell 调用次数（默认 10）"
    )
    p.add_argument("--quota-total", type=int, default=50, help="每会话最大工具调用总数（默认 50）")
    p.add_argument("--light-provider", default=None, help="轻量模型 Provider（如 ollama）")
    p.add_argument("--light-model", default="qwen3.5:9b", help="轻量模型名称（默认 qwen3.5:9b）")
    p.add_argument("--dry-run", action="store_true", help="Dry-run 模式：不实际修改文件")
    p.add_argument(
        "--profile",
        default=None,
        choices=["dev", "prod", "ci"],
        help="预设配置: dev(宽松)/prod(默认)/ci(严格)",
    )
    p.add_argument("--health", action="store_true", help="健康检查：检查所有模块状态并退出")
    p.add_argument("--resume", default=None, help="恢复会话（latest / session_id）")
    add_log_level_argument(p)
    return p


def _make_config(args) -> AgentConfig:
    """从 CLI args 构建 Config。"""
    cfg_kw = {
        "provider": args.provider,
        "model": args.model or os.environ.get("DEEPSEEK_MODEL", AgentConfig().model),
        "max_steps": args.max_steps,
        "tool_timeout_s": args.tool_timeout,
        "step_timeout_s": args.step_timeout,
        "temperature": args.temperature,
        "approval": args.approval,
    }
    return AgentConfig(**{k: v for k, v in cfg_kw.items() if v is not None})


def _make_agent(args) -> Agent:
    """装配完整 Agent 管线：Config → Workspace → ModelClient → Agent。"""
    _load_dotenv()
    config = _make_config(args)
    workspace = WorkspaceContext.build(args.cwd)
    model_client = _build_model_client(args, config)
    return _build_agent(args, config, workspace, model_client)


def main() -> int:
    """命令行入口，one-shot 模式。"""
    parser = _make_parser()
    args = parser.parse_args()
    setup_logging_from_args(args)

    if args.health:
        return _health_check()

    if args.prompt is None:
        return _repl_mode(args)

    agent = _make_agent(args)

    if args.dry_run:
        print("\033[34m[agent_runtime] DRY-RUN MODE\033[0m — 不会实际修改文件", file=sys.stderr)
    cfg = agent.config
    print(f"[agent_runtime] provider={cfg.provider} model={cfg.model}", file=sys.stderr)
    print(f"[agent_runtime] workspace={agent.workspace.repo_root}", file=sys.stderr)

    from agent_runtime.callbacks import CLIProgressCallback

    answer = agent.ask(args.prompt, callback=CLIProgressCallback())
    print(answer)
    return 0


def _build_agent(args, config, workspace, model_client) -> Agent:
    """装配 Agent，支持 --resume 恢复会话。"""
    if args.resume:
        from agent_runtime.checkpoint import evaluate_resume_state
        from agent_runtime.session_store import SessionStore

        store = SessionStore(root=workspace.repo_root)
        session_id = store.latest() if args.resume == "latest" else args.resume
        if session_id is None:
            print("[agent_runtime] 无可恢复的 session，创建新会话", file=sys.stderr)
        else:
            agent = Agent.from_session(
                model_client, workspace, store, session_id, config=config, cwd=args.cwd
            )
            if agent:
                agent.dry_run = args.dry_run
                resume_state = evaluate_resume_state(agent)
                status = resume_state["status"]
                agent.session.setdefault("resume_status", status)
                print(f"[agent_runtime] 恢复会话 {session_id} (status={status})", file=sys.stderr)
                if resume_state["stale_files"]:
                    print(f"  ⚠ 文件已变更: {resume_state['stale_files']}", file=sys.stderr)
                return agent

    # 默认：创建新 Agent
    agent = Agent(
        config=config,
        model_client=model_client,
        workspace=workspace,
        cwd=args.cwd,
        light_client=_build_light_client(args),
        dry_run=args.dry_run,
    )
    # 应用 profile（覆盖配额和审批）
    if args.profile:
        _apply_profile(agent, args.profile)
    # 应用 CLI 配额参数（profile 之后，允许 CLI 覆盖）
    agent.quota._limits["write"] = args.quota_writes
    agent.quota._limits["shell"] = args.quota_shell
    agent.quota._limits["total"] = args.quota_total
    return agent


def _apply_profile(agent, profile: str):
    """应用预设配置 profile。"""
    if profile == "ci":
        agent.config.approval = "never"
        agent.quota._limits["write"] = 0
        agent.quota._limits["shell"] = 0
        agent.quota._limits["total"] = 0
        agent.dry_run = True
        print("[agent_runtime] CI profile: approval=never, quota=0, dry_run", file=sys.stderr)
    elif profile == "dev":
        agent.config.approval = "auto"
        agent.quota._limits["write"] = 100
        agent.quota._limits["shell"] = 50
        agent.quota._limits["total"] = 300
        print("[agent_runtime] DEV profile: approval=auto, high quotas", file=sys.stderr)


def _health_check() -> int:
    """健康检查：输出所有模块状态 JSON。"""
    import json as _json
    import shutil as _shutil
    import sys as _sys

    result = {}

    # Python version
    result["python"] = f"{_sys.version_info.major}.{_sys.version_info.minor}"
    result["status"] = "ok"

    # Git
    result["git"] = "ok" if _shutil.which("git") else "missing"

    # Ripgrep
    result["rg"] = "ok" if _shutil.which("rg") else "missing"

    # TikToken
    try:
        import tiktoken

        tiktoken.get_encoding("cl100k_base")
        result["tiktoken"] = "ok"
    except Exception:
        result["tiktoken"] = "error"
        result["status"] = "degraded"

    # Storage
    try:
        store_path = Path(".agent")
        store_path.mkdir(parents=True, exist_ok=True)
        test_file = store_path / ".health_check"
        test_file.write_text("ok")
        test_file.unlink()
        result["storage"] = "ok"
    except Exception:
        result["storage"] = "error"
        result["status"] = "degraded"

    # Semantic model
    from agent_runtime.features.memory.semantic import _get_semantic_model

    try:
        model = _get_semantic_model()
        result["semantic_model"] = "ok" if model else "unavailable"
    except Exception:
        result["semantic_model"] = "error"

    # Config
    try:
        _ = AgentConfig()
        result["config"] = "ok"
    except Exception:
        result["config"] = "error"
        result["status"] = "degraded"

    print(_json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _build_model_client(args, config: AgentConfig):
    """根据 Provider 创建模型客户端。"""
    if args.provider == "fake":
        return create_model_client(provider="fake")
    return create_model_client(
        model=config.model,
        base_url=args.base_url,
        api_key=args.api_key,
        temperature=config.temperature,
    )


def _build_light_client(args):
    """根据 --light-provider 创建轻量模型客户端（用于摘要等简单任务）。"""
    if not args.light_provider:
        return None

    if args.light_provider == "ollama":
        from agent_runtime.providers.clients import OllamaModelClient

        host = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
        return OllamaModelClient(model=args.light_model, host=host)
    elif args.light_provider in ("deepseek", "anthropic", "openai"):
        return _build_model_client(args, AgentConfig(provider=args.light_provider))

    return None


def _repl_mode(args) -> int:
    """交互式 REPL 模式。"""
    agent = _make_agent(args)

    cfg = agent.config
    print(f"agent_runtime REPL | provider={cfg.provider} model={cfg.model}", file=sys.stderr)
    print(f"workspace={agent.workspace.repo_root}", file=sys.stderr)
    if args.dry_run:
        print("\033[34m⚠ DRY-RUN MODE\033[0m", file=sys.stderr)
    print("输入 /help 查看命令，/exit 退出", file=sys.stderr)
    print("─" * 50, file=sys.stderr)

    while True:
        try:
            user_input = read_repl_input()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            return 0

        if not user_input.strip():
            continue

        # 内置命令
        if user_input.startswith("/"):
            result = _handle_command(user_input, agent)
            if result == "exit":
                return 0
            continue

        # 发送给 Agent
        from agent_runtime.callbacks import CLIProgressCallback

        print("", end="", flush=True)
        answer = agent.ask(user_input, callback=CLIProgressCallback())
        print(answer)


def _handle_command(cmd: str, agent: Agent) -> str:
    """处理 REPL 内置命令。"""
    parts = cmd.strip().split()
    name = parts[0].lower()

    if name == "/exit":
        return "exit"

    if name == "/help":
        print("""可用命令:
  /help     显示此帮助
  /memory   显示工作记忆（M3 完整实现）
  /session  显示当前会话信息
  /reset    清空对话历史
  /exit     退出""")
    elif name == "/memory":
        # M3 接入工作记忆
        print("工作记忆（M3 实现）:")
        print("  (当前暂无持久记忆条目)")
    elif name == "/session":
        sid = agent.session.get("id", "?")
        history_len = len(agent.session.get("history", []))
        print(f"会话 ID: {sid}")
        print(f"对话轮数: {history_len}")
        print(f"approval_policy: {agent.config.approval}")
        print(f"max_steps: {agent.config.max_steps}")
        print(f"dry_run: {agent.dry_run}")
        cb = agent.circuit_breaker
        import time

        if cb.state == "open":
            remain = cb.recovery_timeout - (time.time() - cb._opened_at)
            print(f"CB: OPEN (恢复 {remain:.0f}s, 失败 {cb._failure_count}/{cb.failure_threshold})")
        elif cb.state == "half_open":
            print(
                f"CB: HALF_OPEN (探测 {cb.half_open_success_count}/"
                f"{cb.half_open_success_threshold})"
            )
        else:
            print(f"CB: CLOSED (失败 {cb._failure_count}/{cb.failure_threshold})")
        if hasattr(agent.model_client, "latency_stats"):
            stats = agent.model_client.latency_stats()
            if stats["count"] > 0:
                print(
                    f"API latency: avg={stats['avg']}s p50={stats['p50']}s "
                    f"p99={stats['p99']}s ({stats['count']} calls)"
                )
    elif name == "/reset":
        agent.session["history"] = []
        print("对话历史已清空。")
    else:
        print(f"未知命令: {name}，输入 /help 查看可用命令。")

    return ""


if __name__ == "__main__":
    sys.exit(main())
