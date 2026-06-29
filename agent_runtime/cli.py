"""CLI 入口：命令行参数解析 + Provider 装配。"""

import argparse
import sys


def main() -> int:
    """命令行入口，one-shot 模式。"""
    parser = argparse.ArgumentParser(
        prog="agent_runtime",
        description="手写的 LLM Agent 运行时内核",
    )
    parser.add_argument("prompt", nargs="?", default=None, help="用户输入（缺省进入 REPL 模式）")
    parser.add_argument("--cwd", default=".", help="工作目录")
    parser.add_argument("--provider", default="deepseek", help="模型 Provider")
    parser.add_argument("--model", default=None, help="模型名称")
    parser.add_argument("--max-steps", type=int, default=6, help="最大工具调用步数")
    parser.add_argument("--temperature", type=float, default=0.2, help="模型温度")
    parser.add_argument("--dry-run", action="store_true", help="Dry-run 模式：不实际修改文件")

    args = parser.parse_args()

    if args.prompt is None:
        print("[REPL] 交互模式尚未实现，请提供 prompt 参数。")
        print('使用 --help 查看参数说明。')
        return 0

    print(f"[agent_runtime] provider={args.provider} model={args.model} max_steps={args.max_steps}")
    print("[agent_runtime] TODO: Agent 运行时尚未实现")
    return 0


if __name__ == "__main__":
    sys.exit(main())
