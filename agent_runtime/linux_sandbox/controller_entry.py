"""Trusted Linux entrypoint: receive one request over stdin, never over shell argv."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_runtime.linux_sandbox.backend import LinuxSandboxBackend  # noqa: E402
from agent_runtime.linux_sandbox.models import SandboxRequest  # noqa: E402
from agent_runtime.linux_sandbox.policy import SandboxPolicy  # noqa: E402


def main() -> int:
    config_path = Path(sys.argv[1]).resolve(strict=True)
    if not config_path.is_relative_to(Path(__file__).resolve().parents[2]):
        raise ValueError("policy_denied: config outside trusted controller")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if set(config) != {"workspace", "state_root", "toolchain", "helper"}:
        raise ValueError("policy_denied: controller config")
    backend = LinuxSandboxBackend(
        SandboxPolicy(**{key: Path(value) for key, value in config.items()})
    )
    backend.policy.validate()
    request = SandboxRequest.from_wire(json.loads(sys.stdin.readline()))
    done = threading.Event()

    def cancel_on_eof():
        sys.stdin.read(1)
        while not done.wait(0.05) and not backend.cancel(request.call_id):
            pass

    watcher = threading.Thread(target=cancel_on_eof, daemon=True)
    watcher.start()
    try:
        result = backend.execute(request)
        print(json.dumps(result.to_wire()), flush=True)
    finally:
        done.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
