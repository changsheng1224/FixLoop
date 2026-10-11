"""Controller transport bounds; these tests do not attest WSL isolation."""

import contextlib
import io
import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from agent_runtime.linux_sandbox.models import SandboxRequest
from agent_runtime.linux_sandbox.workspace_controller import run_worker, worker_argv


def test_integrity_measurement_precedes_namespace_and_broker(monkeypatch):
    import agent_runtime.linux_sandbox.environment_worker as environment
    import agent_runtime.linux_sandbox.workspace_controller as controller

    events = []
    identity = dict(runtime_digest="a" * 64, distribution_version="test", runtime_version="s3")
    request = SandboxRequest(
        "ws",
        "task",
        "run",
        "call",
        "workspace",
        (),
        schema_version="2",
        tool_name="read_file",
        tool_args={"path": "a.txt"},
    )
    frame = json.dumps(dict(request=request.to_wire(), environment=identity)) + "\n"
    monkeypatch.setattr(controller.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(frame.encode())))

    def measure():
        events.append("measure")
        return identity

    monkeypatch.setattr(environment, "measure", measure)
    import agent_runtime.linux_sandbox.task_dependencies as dependencies

    monkeypatch.setattr(dependencies, "task_environment", lambda *_: contextlib.nullcontext(None))
    monkeypatch.setattr(
        controller, "enter_namespaces", lambda: events.append("namespaces"), raising=False
    )

    def broker():
        events.append("broker")
        return SimpleNamespace(close=lambda: events.append("closed"))

    monkeypatch.setattr(controller, "ControllerBroker", broker)
    monkeypatch.setattr(controller, "private_binfmt", contextlib.nullcontext)
    monkeypatch.setattr(
        controller,
        "FuseView",
        lambda *_: SimpleNamespace(mounted=lambda: contextlib.nullcontext("view")),
    )
    monkeypatch.setattr(controller, "worker_argv", lambda *_: [])
    monkeypatch.setattr(controller, "run_worker", lambda *_: (0, False, {}))
    assert controller.main() == 0
    assert events == ["measure", "namespaces", "broker", "closed"]


def test_worker_disables_nested_namespaces_with_explicit_user_namespace(tmp_path):
    argv = worker_argv(tmp_path)
    assert "--unshare-all" in argv
    assert argv.index("--unshare-user") < argv.index("--disable-userns")


def test_namespace_maps_task_user_and_disables_mount_propagation(monkeypatch):
    import agent_runtime.linux_sandbox.workspace_controller as controller

    events = []
    monkeypatch.setattr(controller.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(controller.os, "getgid", lambda: 1000, raising=False)
    monkeypatch.setattr(
        controller.Path, "write_text", lambda p, value: events.append((p.as_posix(), value))
    )
    libc = SimpleNamespace(
        unshare=lambda flag: events.append(("unshare", flag)) or 0,
        mount=lambda *args: events.append(("mount", args)) or 0,
    )
    monkeypatch.setattr(controller.ctypes, "CDLL", lambda *_a, **_kw: libc)
    controller.enter_namespaces()
    assert events == [
        ("unshare", 0x10000000),
        ("/proc/self/setgroups", "deny"),
        ("/proc/self/uid_map", "0 1000 1\n"),
        ("/proc/self/gid_map", "0 1000 1\n"),
        ("unshare", 0x00020000),
        ("mount", (None, b"/", None, 0x44000, None)),
    ]


def test_namespace_transition_failure_never_mounts(monkeypatch):
    import agent_runtime.linux_sandbox.workspace_controller as controller

    monkeypatch.setattr(controller.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(controller.os, "getgid", lambda: 1000, raising=False)
    monkeypatch.setattr(
        controller.ctypes, "CDLL", lambda *_a, **_kw: SimpleNamespace(unshare=lambda _: -1)
    )
    with pytest.raises(OSError, match="private user namespace failed"):
        controller.enter_namespaces()


def test_broker_reader_stops_with_host_channel_still_open(monkeypatch):
    import agent_runtime.linux_sandbox.workspace_controller as controller

    monkeypatch.setattr(controller.sys, "stdin", SimpleNamespace(fileno=lambda: 0))

    def idle_select(*_):
        time.sleep(0.01)
        return [], [], []

    monkeypatch.setattr(controller, "select", SimpleNamespace(select=idle_select), raising=False)
    broker = controller.ControllerBroker()
    broker.close()
    assert not broker.receiver.is_alive()


def test_broker_reader_reassembles_fragmented_frames(monkeypatch):
    import agent_runtime.linux_sandbox.workspace_controller as controller

    chunks = iter([b'{"res', b'ult": 7}\n', b""])
    monkeypatch.setattr(controller.sys, "stdin", SimpleNamespace(fileno=lambda: 0))
    monkeypatch.setattr(controller.select, "select", lambda *_: ([0], [], []))
    monkeypatch.setattr(controller.os, "read", lambda *_: next(chunks))
    broker = controller.ControllerBroker()
    broker.receiver.join(timeout=1)
    assert broker.responses.get_nowait() == {"result": 7}
    broker.close()


def test_worker_not_reading_input_cannot_block_deadline():
    request = SandboxRequest(
        "ws",
        "task",
        "run",
        "call",
        "workspace",
        (),
        timeout_s=0.1,
        schema_version="2",
        tool_name="write_file",
        tool_args={"path": "a.txt", "content": "x" * 500_000},
        base_versions={"a.txt": None},
    )
    started = time.monotonic()
    code, cancelled, reply = run_worker(
        [sys.executable, "-I", "-c", "import time; time.sleep(30)"],
        request,
        SimpleNamespace(disconnected=threading.Event()),
    )
    assert time.monotonic() - started < 5
    assert cancelled and code != 0 and reply is None


def test_failed_worker_start_closes_pipes():
    request = SandboxRequest(
        "ws",
        "task",
        "run",
        "call",
        "workspace",
        (),
        timeout_s=1,
        schema_version="2",
        tool_name="read_file",
        tool_args={"path": "a.txt"},
    )
    code, cancelled, reply = run_worker(
        [sys.executable, "-I", "-c", "raise SystemExit(2)"],
        request,
        SimpleNamespace(disconnected=threading.Event()),
    )
    assert code == 2 and not cancelled and reply is None


def test_worker_transport_failure_is_not_a_success():
    request = SandboxRequest(
        "ws",
        "task",
        "run",
        "call",
        "workspace",
        (),
        timeout_s=1,
        schema_version="2",
        tool_name="write_file",
        tool_args={"path": "a.txt", "content": "x" * 500_000},
        base_versions={"a.txt": None},
    )
    with pytest.raises(ValueError, match="worker transport failed"):
        run_worker(
            [sys.executable, "-I", "-c", "raise SystemExit(0)"],
            request,
            SimpleNamespace(disconnected=threading.Event()),
        )
