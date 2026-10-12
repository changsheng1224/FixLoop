"""S6 fixture: production service/executors; optional process-death injection only."""

import argparse
import json
import logging
import os
import sys
import traceback
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.workbench.chat import ChatExecutor  # noqa: E402
from src.workbench.http import WorkbenchHTTPServer  # noqa: E402
from src.workbench.project import ProjectExecutor  # noqa: E402
from src.workbench.service import WorkbenchService  # noqa: E402
from tests.workbench_e2e_support import ModelServer  # noqa: E402


class _SyntheticFailureTrace(logging.Filter):
    def filter(self, record):
        error = sys.exception()
        if error is not None:
            traceback.print_exception(error, file=sys.stderr)
        return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--provider-url")
    parser.add_argument("--project-lock-dir", type=Path)
    parser.add_argument("--timeout", type=float, default=40)
    parser.add_argument("--crash-restore", action="store_true")
    parser.add_argument(
        "--ui-hardening", action="store_true", help="Seed UI states; no repair execution"
    )
    args = parser.parse_args()
    logging.getLogger("src.workbench.service").addFilter(_SyntheticFailureTrace())
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)

    def fault(where, index):
        if args.crash_restore and where == "after_step":
            os._exit(77)  # Real death between file replacement and journal acknowledgement.

    with ExitStack() as stack:
        provider = args.provider_url or stack.enter_context(ModelServer(root / "model")).url
        options = dict(
            provider="ollama", model="s6-local", base_url=provider, timeout_s=args.timeout
        )
        service = stack.enter_context(
            WorkbenchService(
                root / "state",
                executor=ChatExecutor(**options),
                project_executor=ProjectExecutor(**options),
                project_lock_dir=args.project_lock_dir or root / "locks",
                restore_fault=fault,
            )
        )
        cases = None
        if args.ui_hardening:
            from tests.workbench_hardening_ui_fixture import seed

            cases = seed(service, root)
        server = stack.enter_context(
            WorkbenchHTTPServer(("127.0.0.1", 0), service, token="s6-local-test-only-bearer-token")
        )
        startup = {
            "url": f"http://127.0.0.1:{server.server_port}",
            "token": "s6-local-test-only-bearer-token",
            "provider": provider,
            "root": str(root),
            "ui_cases": cases,
        }
        (root / "ready.json").write_text(json.dumps(startup), encoding="utf-8")
        print(json.dumps(startup), flush=True)
        try:
            server.serve_forever(poll_interval=0.05)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
