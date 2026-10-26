"""Offline graph demo: scripted model, governed edits, real pytest and Plan journal."""

import argparse
import difflib
import json
import os
import tempfile
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path


def scripted_attempt(value):
    return [
        '{"conclusion":"Read value.py, change the return literal and verify with test_value.py."}',
        '<tool>{"name":"read_file","args":{"path":"value.py"}}</tool>',
        "<tool>"
        + json.dumps(
            {
                "name": "patch_file",
                "args": {"path": "value.py", "old_text": "return 1", "new_text": f"return {value}"},
            }
        )
        + "</tool>",
        "<final>Applied the scripted candidate; verification decides the result.</final>",
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("success", "retry"), default="success")
    parser.add_argument(
        "--output", type=Path, help="New output directory; existing paths are refused"
    )
    args = parser.parse_args()
    if args.output is not None:
        output = args.output.resolve()
        try:
            output.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            parser.error(f"output already exists: {output}")
    else:
        temporary = Path(__file__).resolve().parents[1] / ".tmp"
        temporary.mkdir(exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix="lg-", dir=temporary))
    for name, value in {
        "LANGSMITH_TRACING": "false",
        "LANGCHAIN_TRACING_V2": "false",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }.items():
        os.environ[name] = value

    from agent_runtime.plan_runtime.session import PlanSession
    from agent_runtime.providers.clients import FakeModelClient
    from agent_runtime.run_coordination.store import RunCoordinationStore
    from src.repair.assembly import create_repair_service
    from src.repair.graph import repair_graph
    from src.repair.progress import ProgressEmitter
    from src.repair.service import RepairRequest

    workspace = output / "workspace"
    workspace.mkdir()
    before = "def answer():\n    return 1\n"
    (workspace / "value.py").write_text(before, encoding="utf-8")
    (workspace / "test_value.py").write_text(
        "from value import answer\ndef test_answer():\n    assert answer() == 2\n", encoding="utf-8"
    )
    responses = scripted_attempt(2)
    if args.scenario == "retry":
        responses = scripted_attempt(3) + responses
    client = FakeModelClient(responses)
    events = []
    service = create_repair_service(
        client,
        str(workspace),
        workspace_policy="provided",
        execution_tier="host",
        state_root=str(output / "state"),
        progress=ProgressEmitter(quiet=True, record=events.append),
        runtime_overrides={
            "provider": "fake",
            "model": "gpt-4o",
            "approval": "auto",
            "auto_context_extraction": False,
        },
    )
    result = service.run(
        RepairRequest(
            "Fix value.py: answer() returns 1 but should return 2; test_value.py checks it.",
            run_id="fixloop-graph-demo",
            max_retries=2,
            timeout_s=60,
            verification_target="test_value.py",
        )
    )
    after = (workspace / "value.py").read_text(encoding="utf-8")
    (output / "repair.diff").write_text(
        "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile="a/value.py",
                tofile="b/value.py",
            )
        ),
        encoding="utf-8",
    )
    (output / "flow.mmd").write_text(repair_graph().get_graph().draw_mermaid(), encoding="utf-8")
    (output / "events.json").write_text(
        json.dumps([asdict(event) for event in events], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with PlanSession(
        str(workspace),
        result.repair_run_id,
        result.repair_run_id,
        service.session.patcher.tools,
        state_root=str(output / "state"),
    ) as plan:
        operations = plan.store.latest("operation", "operation_id").values()
        writes = sum(op["effect"] == "write" for op in operations)
        plan_status = plan.plan.status
        plan_version = plan.plan.plan_version
    coordination = RunCoordinationStore(str(workspace), state_root=str(output / "state")).snapshot(
        result.repair_run_id
    )
    report = {
        "scenario": args.scenario,
        "status": result.status,
        "retry_count": result.retry_count,
        "model_calls": client.session_usage["calls"],
        "verification": result.verification_result.to_dict()
        if result.verification_result
        else None,
        "plan_status": plan_status,
        "plan_version": plan_version,
        "write_operations": writes,
        "coordination_status": coordination.status,
        "owner_released": not coordination.owner_token,
        "langgraph": version("langgraph"),
        "errors": result.agent_errors,
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"output": str(output), **report}, ensure_ascii=False, indent=2))
    expected_retry = int(args.scenario == "retry")
    if not (
        result.status == "fixed"
        and result.retry_count == expected_retry
        and after == "def answer():\n    return 2\n"
        and plan_status == "completed"
        and plan_version == expected_retry + 1
        and writes == expected_retry + 1
        and not coordination.owner_token
    ):
        raise SystemExit(
            "Demo assertions failed; inspect report.json, events.json and state/.agent"
        )


if __name__ == "__main__":
    main()
