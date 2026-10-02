"""Real L2 Agent tools and host pytest; deterministic model output only."""

import json
import time
from pathlib import Path

from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.workspace import WorkspaceContext
from src.agents.factory import create_repair_agent
from src.orchestrator import Orchestrator
from src.state import RepairPlan, RepairState, SuspectLocation
from tests.repair_support import build_repository


def repair_fixture(root, *, resume=False, full=False):
    root = Path(root)
    root.mkdir(exist_ok=True)
    if not resume:
        build_repository(
            root,
            {
                "value.py": "def answer():\n    return 1\n",
                "test_value.py": "from value import answer\ndef test_answer():\n    assert answer() == 2\n",
            },
        )
    responses = (
        [
            '{"conclusion":"value.py returns 1; the requested expectation is 2. Change the return literal and run the existing test."}',
            '<tool>{"name":"read_file","args":{"path":"value.py"}}</tool>',
            "<tool>"
            + json.dumps(
                {
                    "name": "patch_file",
                    "args": {
                        "path": "value.py",
                        "old_text": "return 1",
                        "new_text": "return 2",
                    },
                }
            )
            + "</tool>",
            "<final>Applied the requested fix.</final>",
        ]
        if not resume
        else ["<final>Unexpected model call after durable patch.</final>"]
    )
    client = FakeModelClient(responses)
    agent = create_repair_agent("patcher", client, WorkspaceContext.build(str(root)), cwd=str(root))
    agent.config.approval = "auto"
    orch = Orchestrator(agent, use_pytest_verify=True, sandbox_policy="disabled")
    state = RepairState(
        issue_input="Fix value.py: answer() returns 1 but should return 2.",
        repair_run_id="plan-fixture-run",
        repair_plan=RepairPlan(suspect_files=["value.py"]),
        suspect_locations=[SuspectLocation(file_path="value.py", start_line=1, end_line=2)],
    )
    agent.shared_run_id = state.repair_run_id
    if not full:
        from src.repair.run_context import RepairRunContext

        orch._repair_ctx = RepairRunContext(repair_started_at=time.time())
        orch._merge_blackboard_for_patch = lambda state: None
    return orch, state, client
