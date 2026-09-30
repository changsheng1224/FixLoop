"""P1 protocol and fail-closed path checks; no Linux process is started."""

from dataclasses import replace
from pathlib import Path

import pytest

from agent_runtime.linux_sandbox.models import SandboxRequest
from agent_runtime.linux_sandbox.policy import SandboxPolicy


def request(**changes):
    base = SandboxRequest(
        "ws", "task", "run", "call", "command", ("/toolchain/bin/python", "-I", "-c", "print(1)")
    )
    return replace(base, **changes)


def test_request_wire_rejects_unknown_fields_and_bad_argv():
    wire = request().to_wire()
    assert SandboxRequest.from_wire(wire) == request()
    with pytest.raises(ValueError):
        SandboxRequest.from_wire({**wire, "mount": "/"})
    with pytest.raises(ValueError):
        SandboxRequest.from_wire({**wire, "argv": ["python", "bad\x00arg"]})


def test_policy_rejects_paths_and_untrusted_executable(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        (workspace / "escape").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pass  # Windows without Developer Mode cannot create symlinks.
    policy = SandboxPolicy(
        workspace, tmp_path / "state", tmp_path / "toolchain", tmp_path / "supervisor.py"
    )
    bad_paths = ["../outside", str(tmp_path)]
    if (workspace / "escape").is_symlink():
        bad_paths.append("escape")
    for bad in bad_paths:
        with pytest.raises(ValueError):
            policy.validate_request(request(cwd_relative=bad))
    with pytest.raises(ValueError, match="executable"):
        policy.validate_request(request(argv=("/bin/sh", "-c", "true")))
    with pytest.raises(ValueError, match="identity"):
        policy.validate_request(request(call_id="../collision"))
    with pytest.raises(ValueError, match="pytest profile"):
        policy.validate_request(request(operation="pytest"))


def test_policy_only_lowers_limits(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    policy = SandboxPolicy(root, Path("/state"), Path("/toolchain"), Path("/helper"))
    assert policy.validate_request(request(timeout_s=20, output_limit_bytes=1024)) == Path(".")
    with pytest.raises(ValueError, match="timeout"):
        policy.validate_request(request(timeout_s=121))
    with pytest.raises(ValueError, match="output limit"):
        policy.validate_request(request(output_limit_bytes=1048577))
