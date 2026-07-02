"""CLI 入口：命令行参数解析 + Provider 装配。"""

import argparse
import os
import sys
from pathlib import Path

from agent_runtime.config import AgentConfig
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


def _make_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器。默认值来自 AgentConfig。"""
    cfg = AgentConfig()
    p = argparse.ArgumentParser(prog="agent_runtime", description="手写的 LLM Agent 运行时内核")
    p.add_argument("prompt", nargs="?", default=None, help="用户输入（缺省进入 REPL 模式）")
    p.add_argument("--cwd", default=".", help="工作目录")
    p.add_argument("--provider", default=cfg.provider,
                   help=f"模型 Provider（默认 {cfg.provider}）")
    p.add_argument("--model", default=None, help=f"模型名称（默认 {cfg.model}）")
    p.add_argument("--max-steps", type=int, default=cfg.max_steps,
                   help=f"最大工具步数（默认 {cfg.max_steps}）")
    p.add_argument("--temperature", type=float, default=cfg.temperature,
                   help=f"模型温度（默认 {cfg.temperature}）")
    p.add_argument("--api-key", default=None, help="API Key（覆盖 .env）")
    p.add_argument("--base-url", default=None, help="API Base URL（覆盖 .env）")
    p.add_argument("--approval", default=cfg.approval,
                   choices=["auto", "ask", "never"],
                   help=f"审批策略（默认 {cfg.approval}）")
    p.add_argument("--quota-writes", type=int, default=20,
                   help="每会话最大写入次数（默认 20）")
    p.add_argument("--quota-shell", type=int, default=10,
                   help="每会话最大 Shell 调用次数（默认 10）")
    p.add_argument("--quota-total", type=int, default=50,
                   help="每会话最大工具调用总数（默认 50）")
    p.add_argument("--light-provider", default=None,
                   help="轻量模型 Provider（如 ollama）")
    p.add_argument("--light-model", default="qwen3.5:9b",
                   help="轻量模型名称（默认 qwen3.5:9b）")
    p.add_argument("--dry-run", action="store_true",
                   help="Dry-run 模式：不实际修改文件")
    p.add_argument("--resume", default=None,
                   help="恢复会话（latest / session_id）")
    return p


def _make_config(args) -> AgentConfig:
    """从 CLI args 构建 Config。"""
    cfg_kw = {
        "provider": args.provider,
        "model": args.model or os.environ.get("DEEPSEEK_MODEL", AgentConfig().model),
        "max_steps": args.max_steps,
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

    if args.prompt is None:
        return _repl_mode(args)

    agent = _make_agent(args)

    if args.dry_run:
        print("[agent_runtime] DRY-RUN MODE — 不会实际修改文件", file=sys.stderr)
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
    agent = Agent(config=config, model_client=model_client, workspace=workspace,
                  cwd=args.cwd, light_client=_build_light_client(args),
                  dry_run=args.dry_run)
    # 应用 CLI 配额参数
    agent.quota._limits["write"] = args.quota_writes
    agent.quota._limits["shell"] = args.quota_shell
    agent.quota._limits["total"] = args.quota_total
    return agent


def _load_dotenv():
    """从项目根目录的 .env 加载环境变量（不覆盖已有）。"""
    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if key and key not in os.environ:
                os.environ[key] = val


def _build_model_client(args, config: AgentConfig):
    """根据 Provider 创建模型客户端。"""
    if args.provider == "fake":
        from agent_runtime.providers.clients import FakeModelClient

        return FakeModelClient(
            ["<final>FakeClient 未预设输出，请指定 --provider 为真实 Provider。</final>"]
        )

    # 默认：Anthropic Compatible
    from agent_runtime.providers.clients import AnthropicCompatibleModelClient

    api_key = args.api_key or os.environ.get("DEEPSEEK_API_KEY", "")
    base_url = args.base_url or os.environ.get(
        "DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic/v1"
    )

    return AnthropicCompatibleModelClient(
        model=config.model,
        base_url=base_url,
        api_key=api_key,
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
        print("⚠ DRY-RUN MODE", file=sys.stderr)
    print('输入 /help 查看命令，/exit 退出', file=sys.stderr)
    print("─" * 50, file=sys.stderr)

    while True:
        try:
            user_input = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            return 0

        if not user_input:
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
