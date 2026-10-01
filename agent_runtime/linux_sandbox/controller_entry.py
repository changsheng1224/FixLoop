"""Trusted Linux entrypoint: receive one request over stdin, never over shell argv."""

from __future__ import annotations

import json
import sys
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
package = types.ModuleType("agent_runtime")
package.__path__ = [str(Path(__file__).resolve().parents[2] / "agent_runtime")]
sys.modules["agent_runtime"] = package

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
    raw_request = json.loads(sys.stdin.readline())
    if (
        isinstance(raw_request, dict)
        and set(raw_request) == {"control", "call_id"}
        and raw_request["control"] == "inspect_receipt"
    ):
        print(json.dumps({"receipt": backend.inspect_receipt(raw_request["call_id"])}), flush=True)
        return 0
    request = SandboxRequest.from_wire(raw_request)
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
