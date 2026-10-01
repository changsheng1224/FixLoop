"""Windows transport to a fixed, trusted WSL controller entrypoint."""

from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass

from .models import SandboxRequest, SandboxResult


@dataclass(frozen=True)
class WslLauncherConfig:
    distribution: str
    python: str
    controller: str
    config: str

    def argv(self) -> list[str]:
        if self.distribution != "Ubuntu":
            raise ValueError("distribution_mismatch")
        for path in (self.python, self.controller, self.config):
            if not path.startswith("/") or "\x00" in path or ".." in path.split("/"):
                raise ValueError("policy_denied: trusted controller path")
        return [
            "wsl.exe",
            "--distribution",
            self.distribution,
            "--exec",
            self.python,
            "-I",
            self.controller,
            self.config,
        ]


class WindowsWslBackend:
    """One process per call; model-supplied argv is never a Windows command argument."""

    def __init__(self, config: WslLauncherConfig):
        self.config = config
        self._active: dict[str, subprocess.Popen] = {}
        self._guard = threading.Lock()

    def execute(self, request: SandboxRequest) -> SandboxResult:
        try:
            proc = subprocess.Popen(
                self.config.argv(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except (OSError, ValueError):
            return SandboxResult(
                "start_failed", error_code="wsl_unavailable", receipt_id=request.call_id
            )
        answer: list[bytes] = []
        reader = threading.Thread(
            target=lambda: answer.append(proc.stdout.readline(2_000_001)), daemon=True
        )
        with self._guard:
            self._active[request.call_id] = proc
        try:
            proc.stdin.write((json.dumps(request.to_wire()) + "\n").encode("utf-8"))
            proc.stdin.flush()
            reader.start()
            reader.join(timeout=request.timeout_s + 20)
            if reader.is_alive():
                proc.stdin.close()  # EOF asks the Linux controller to cancel and confirm cleanup.
                reader.join(timeout=6)
            if not reader.is_alive():
                proc.wait(timeout=3)
                if proc.returncode == 0 and answer and len(answer[0]) <= 2_000_000:
                    result = SandboxResult(**json.loads(answer[0]))
                    if result.receipt_id == request.call_id and (
                        not request.owner_token
                        or (
                            result.owner_token == request.owner_token
                            and result.generation == request.generation
                            and result.coordination_revision == request.coordination_revision
                        )
                    ):
                        return result
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass
        finally:
            with self._guard:
                self._active.pop(request.call_id, None)
            if not proc.stdin.closed:
                proc.stdin.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            proc.stdout.close()
        return SandboxResult(
            "uncertain", error_code="execution_uncertain", receipt_id=request.call_id
        )

    def cancel(self, call_id: str) -> bool:
        with self._guard:
            proc = self._active.get(call_id)
            if proc is None or proc.stdin.closed:
                return False
            proc.stdin.close()
            return True

    def inspect_receipt(self, call_id: str) -> dict | None:
        """Ask the trusted controller for an exact durable receipt."""
        import re

        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", call_id):
            raise ValueError("receipt_invalid: call_id")
        result = subprocess.run(
            self.config.argv(),
            input=json.dumps({"control": "inspect_receipt", "call_id": call_id}) + "\n",
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 2_000_000:
            raise ValueError("sandbox_receipt_unavailable")
        return json.loads(result.stdout)["receipt"]
