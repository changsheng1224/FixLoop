"""Run the local workbench: python -m src.workbench."""

import argparse
import os
import secrets
import webbrowser
from pathlib import Path

from agent_runtime.bootstrap import load_dotenv
from src.workbench.chat import ChatExecutor
from src.workbench.http import WorkbenchHTTPServer
from src.workbench.project import ProjectExecutor
from src.workbench.service import WorkbenchService


def add_workbench_arguments(parser):
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".fixloop" / "workbench")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--provider",
        choices=["anthropic_compat", "openai", "ollama", "fake"],
        default="anthropic_compat",
    )
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--repair-tier", choices=["host", "container"], default="host")
    parser.add_argument("--open-browser", action="store_true", help="Open the local workbench UI")
    return parser


def run_workbench(args):
    if not 0 <= args.port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    load_dotenv()
    executor = ChatExecutor(
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        timeout_s=args.timeout,
        max_output_tokens=args.max_output_tokens,
    )
    # API token is separate from provider credentials. Never include it in URLs.
    token = secrets.token_urlsafe(32)
    project_executor = ProjectExecutor(
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        timeout_s=args.timeout,
        max_output_tokens=args.max_output_tokens,
        execution_tier=args.repair_tier,
    )
    with WorkbenchService(
        args.state_dir, executor=executor, project_executor=project_executor
    ) as service:
        token_path = service.state_dir / "api-token"
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as file:
            file.write(token)
        try:
            with WorkbenchHTTPServer(("127.0.0.1", args.port), service, token=token) as server:
                print(f"FixLoop workbench: http://127.0.0.1:{server.server_port}", flush=True)
                print(f"Bearer token file: {token_path}", flush=True)
                if args.open_browser:
                    webbrowser.open(f"http://127.0.0.1:{server.server_port}/")
                try:
                    server.serve_forever(poll_interval=0.1)
                except KeyboardInterrupt:
                    server.stopping.set()
        finally:
            token_path.unlink(missing_ok=True)
    return 0


def main(argv=None):
    parser = add_workbench_arguments(
        argparse.ArgumentParser(description="FixLoop local workbench JSON/SSE service")
    )
    return run_workbench(parser.parse_args(argv))


if __name__ == "__main__":
    main()
