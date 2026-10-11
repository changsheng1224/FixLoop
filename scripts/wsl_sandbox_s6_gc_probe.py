"""S6 native admission boundaries and disposable environment retirement."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.linux_sandbox.environment import (  # noqa: E402
    EnvironmentError,
    SandboxEnvironmentManager,
    load_release,
)
from agent_runtime.linux_sandbox.task_context import (  # noqa: E402
    SandboxTaskBinding,
    SandboxTaskSpec,
)
from agent_runtime.linux_sandbox.workspace_backend import (  # noqa: E402
    GuardedWindowsWorkspaceBackend,
)

NATIVE = r'''
import sys,types,tempfile,json,os
from pathlib import Path
from types import SimpleNamespace
m=types.ModuleType('agent_runtime')
m.__path__=['/opt/fixloop/runtime/agent_runtime']
sys.modules['agent_runtime']=m
sys.path.insert(0,'/opt/fixloop/runtime')
from agent_runtime.linux_sandbox import task_dependencies as tasks
from agent_runtime.linux_sandbox import resource_supervisor as supervisor
from agent_runtime.linux_sandbox.receipts import _read
checks={}
def check(name,value):
    checks[name]=bool(value)
    assert value,name
def fails(callback,code):
    try: callback()
    except ValueError as exc: return str(exc).startswith(code)
    return False
with tempfile.TemporaryDirectory(prefix='fixloop-s6-boundaries-') as raw:
    root=Path(raw)
    os.chown(root,1000,1000)
    home=root/'home'
    home.mkdir(mode=0o700)
    os.chown(home,1000,1000)
    tasks.TASK_BASE=home/'tasks'
    tasks.TASK_BASE.mkdir(mode=0o700)
    os.chown(tasks.TASK_BASE,1000,1000)
    for index in range(128):
        path=tasks.TASK_BASE/('active-'+str(index))
        path.mkdir(mode=0o700)
        os.chown(path,1000,1000)
    request=SimpleNamespace(workspace_id='probe',task_id='never-started',run_id='never-started')
    measured={'runtime_digest':'a'*64}
    def as_task(retiring):
        pid=os.fork()
        if pid==0:
            try:
                os.setgroups([]); os.setgid(1000); os.setuid(1000)
                with tasks.task_environment(request,measured,retiring=retiring): pass
                os._exit(0)
            except ValueError as exc:
                os._exit(2 if str(exc).startswith('resource_limit_exceeded') else 3)
        return os.waitpid(pid,0)[1]>>8
    check('active_task_admission_bounded',as_task(False)==2)
    result=supervisor.retire_dependencies(vars(request),measured)
    task=tasks.TASK_BASE/tasks.task_key(request,measured)
    check('never_started_retirement_at_active_limit',result['cleanup']=='confirmed')
    check('retirement_does_not_create_dependencies',not (task/'deps').exists())
    check('retired_task_fenced',as_task(False)==3)
    check('native_retirement_idempotent',supervisor.retire_dependencies(vars(request),measured)==result)
    # Retired evidence consumes the total budget, not an active execution slot.
    (tasks.TASK_BASE/'active-0'/'retired.json').write_text('{}')
    request.task_id=request.run_id='next-task'
    check('retired_task_frees_active_slot',as_task(False)==0)
    for index in range(4096-len(list(tasks.TASK_BASE.iterdir()))+1):
        path=tasks.TASK_BASE/('retained-'+str(index))
        path.mkdir(mode=0o700)
        (path/'retired.json').write_text('{}')
        os.chown(path,1000,1000)
    request.task_id=request.run_id='beyond-journal-budget'
    check('total_task_evidence_bounded',as_task(False)==2)
    data=root/'quota'
    data.mkdir()
    with (data/'large').open('wb') as stream: stream.truncate(257*1024**2)
    check('dependency_byte_budget',fails(lambda:supervisor.dependency_usage(data),'resource_limit_exceeded'))
    (data/'large').unlink()
    for index in range(8193): (data/str(index)).touch()
    check('dependency_entry_budget',fails(lambda:supervisor.dependency_usage(data),'resource_limit_exceeded'))
print(json.dumps(checks))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = {"status": "running", "checks": {}}

    def save():
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    def check(name, value):
        report["checks"][name] = bool(value)
        save()
        assert value, name

    try:
        release = load_release()
        release["version"] = "s6-gc-v1"
        manager = SandboxEnvironmentManager(
            args.workspace,
            release=release,
            progress=lambda stage: print("environment: " + stage, flush=True),
        )
        before = manager.transport.inventory()
        environment = manager.ensure()
        report.update(
            release_id=environment.release_id,
            runtime_digest=environment.identity.runtime_digest,
            distribution=environment.identity.distribution,
        )
        checks = json.loads(
            manager.transport.execute(
                environment.identity.distribution,
                ["/usr/bin/python3.14", "-I", "-B", "-"],
                user="root",
                data=NATIVE.encode(),
                timeout=30,
            )
        )
        for name, value in checks.items():
            check(name, value)
        fixture = args.workspace.resolve() / ".tmp" / ("s6-gc-" + uuid.uuid4().hex)
        repository = fixture / "repo"
        repository.mkdir(parents=True)
        (repository / "user.txt").write_text("preserve me", encoding="utf-8")
        binding = SandboxTaskBinding.create(
            repository,
            repository,
            "s6-gc-idle",
            SandboxTaskSpec(
                environment.identity,
                "s6-gc-v1",
                hashlib.sha256(b"s6-gc-v1").hexdigest(),
                str(fixture / "state"),
            ),
        )
        binding.save()
        backend = GuardedWindowsWorkspaceBackend(binding)
        try:
            manager.retire()
            blocked = False
        except EnvironmentError as exc:
            blocked = exc.code == "environment_in_use"
        check("idle_pin_blocks_environment_collection", blocked)
        check("idle_task_retired", backend.retire()["state"] == "retired")
        check("exact_environment_unregistered", manager.retire()["stage"] == "retired")
        check("environment_retirement_idempotent", manager.retire()["stage"] == "retired")
        check("other_distributions_unchanged", manager.transport.inventory() == before)
        check("installation_and_recovery_evidence_retained", manager.journal.exists())
        check(
            "user_workspace_and_task_state_retained",
            (repository / "user.txt").read_text() == "preserve me"
            and (Path(binding.state_root) / "sandbox-task.json").exists(),
        )
        try:
            manager.ensure()
            blocked = False
        except EnvironmentError as exc:
            blocked = exc.code == "environment_retired"
        check("retired_environment_cannot_reopen", blocked)
        report["status"] = "passed"
    except Exception as exc:
        report.update(status="failed", error=type(exc).__name__ + ": " + str(exc))
        raise
    finally:
        save()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
