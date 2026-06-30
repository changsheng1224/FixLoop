"""CLI 入口：命令行参数解析 + Provider 装配。"""

import argparse
import os
import sys
from pathlib import Path

from agent_runtime.config import AgentConfig
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


def main() -> int:
    """命令行入口，one-shot 模式。"""
    parser = argparse.ArgumentParser(
        prog="agent_runtime",
        description="手写的 LLM Agent 运行时内核",
    )
    parser.add_argument(
        "prompt", nargs="?", default=None, help="用户输入（缺省进入 REPL 模式）"
    )
    parser.add_argument("--cwd", default=".", help="工作目录")
    parser.add_argument("--provider", default="deepseek", help="模型 Provider: deepseek / fake")
    parser.add_argument("--model", default=None, help="模型名称")
    parser.add_argument("--max-steps", type=int, default=6, help="最大工具调用步数")
    parser.add_argument("--temperature", type=float, default=0.2, help="模型温度")
    parser.add_argument("--api-key", default=None, help="API Key（覆盖 .env）")
    parser.add_argument("--base-url", default=None, help="API Base URL（覆盖 .env）")
    parser.add_argument("--approval", default="ask", choices=["auto","ask","never"],
                        help="高风险工具审批策略: auto/ask/never（默认 ask）")
    parser.add_argument("--dry-run", action="store_true", help="Dry-run 模式：不实际修改文件")
    parser.add_argument("--resume", default=None, help="恢复会话（latest / session_id）")

    args = parser.parse_args()

    if args.prompt is None:
        return _repl_mode(args)

    # 加载 .env
    _load_dotenv()

    # 装配 Config
    config = AgentConfig(
        provider=args.provider,
        model=args.model or os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"),
        max_steps=args.max_steps,
        temperature=args.temperature,
        approval=args.approval,
    )

    # 装配 Workspace
    workspace = WorkspaceContext.build(args.cwd)

    # 装配 ModelClient
    model_client = _build_model_client(args, config)

    # 装配 Agent（支持 --resume）
    agent = _build_agent(args, config, workspace, model_client)

    if args.dry_run:
        print("[agent_runtime] DRY-RUN MODE — 不会实际修改文件", file=sys.stderr)

    print(f"[agent_runtime] provider={config.provider} model={config.model}", file=sys.stderr)
    print(f"[agent_runtime] workspace={workspace.repo_root}", file=sys.stderr)

    # 执行
    answer = agent.ask(args.prompt)
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
                agent._dry_run = args.dry_run
                resume_state = evaluate_resume_state(agent)
                status = resume_state["status"]
                print(f"[agent_runtime] 恢复会话 {session_id} (status={status})", file=sys.stderr)
                if resume_state["stale_files"]:
                    print(f"  ⚠ 文件已变更: {resume_state['stale_files']}", file=sys.stderr)
                return agent

    # 默认：创建新 Agent
    agent = Agent(config=config, model_client=model_client, workspace=workspace, cwd=args.cwd)
    agent._dry_run = args.dry_run
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


def _repl_mode(args) -> int:
    """交互式 REPL 模式。"""
    import os

    _load_dotenv()

    config = AgentConfig(
        provider=args.provider,
        model=args.model or os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"),
        max_steps=args.max_steps,
        temperature=args.temperature,
        approval=args.approval,
    )

    workspace = WorkspaceContext.build(args.cwd)
    model_client = _build_model_client(args, config)
    agent = _build_agent(args, config, workspace, model_client)

    print(f"agent_runtime REPL | provider={config.provider} model={config.model}", file=sys.stderr)
    print(f"workspace={workspace.repo_root}", file=sys.stderr)
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
        print("", end="", flush=True)  # 换行
        answer = agent.ask(user_input)
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
        print(f"dry_run: {getattr(agent, '_dry_run', False)}")
    elif name == "/reset":
        agent.session["history"] = []
        print("对话历史已清空。")
    else:
        print(f"未知命令: {name}，输入 /help 查看可用命令。")

    return ""


if __name__ == "__main__":
    sys.exit(main())
