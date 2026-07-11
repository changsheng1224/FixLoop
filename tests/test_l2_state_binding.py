"""L1/L2 State 关联集成单测。"""

from __future__ import annotations

import json

from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.workspace import WorkspaceContext
from src.agents.localizer import create_localizer
from src.agents.patcher import create_patcher
from src.agents.retriever import create_retriever
from src.orchestrator import Orchestrator


class TestL2StateBindingIntegration:
    def test_repair_records_agent_asks_and_trace_l2_fields(self, temp_workspace):
        (temp_workspace / "calc.py").write_text("old\n", encoding="utf-8")
        ws = WorkspaceContext.build(str(temp_workspace))
        loc = FakeModelClient(
            [
                '<final>[{"file_path":"calc.py","start_line":1,"end_line":1,'
                '"reason":"x","confidence":0.9}]</final>',
            ]
        )
        ret = FakeModelClient(['<final>{"related_tests":[]}</final>'])
        pat = FakeModelClient(
            ['<final>[{"file_path":"calc.py","diff":"-old\\n+new","explanation":"fix"}]</final>']
        )
        orch = Orchestrator(
            create_localizer(loc, ws),
            create_retriever(ret, ws),
            create_patcher(pat, ws),
        )
        state = orch.repair("TypeError at calc.py:1")
        assert state.repair_run_id
        assert len(state.agent_asks) >= 3

        agents = {ref.agent for ref in state.agent_asks}
        assert "localizer" in agents
        assert "retriever" in agents
        assert "patcher" in agents

        for ref in state.agent_asks:
            assert ref.run_id == state.repair_run_id
            assert ref.task_id.startswith(state.repair_run_id)

        run_dir = temp_workspace / ".agent" / "runs" / state.repair_run_id
        events = [
            json.loads(line)
            for line in run_dir.joinpath("trace.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        started = [e for e in events if e.get("event") == "agent_ask_started"]
        finished = [e for e in events if e.get("event") == "agent_ask_finished"]
        assert len(started) >= 3
        assert len(finished) >= 3
        assert started[0]["payload"].get("l2_phase") in ("localize", "retrieve", "patch")

        run_events = [e for e in events if e.get("event") == "run_started"]
        assert run_events
        assert run_events[0]["payload"].get("l2_phase") or run_events[0]["payload"].get(
            "repair_run_id"
        )

        report = json.loads(run_dir.joinpath("report.json").read_text(encoding="utf-8"))
        assert report.get("l2_binding_schema_version") == 1
        assert len(report.get("agent_asks", [])) >= 3

        ts_path = run_dir / "task_state.localizer.json"
        assert ts_path.is_file()
        ts_body = json.loads(ts_path.read_text(encoding="utf-8"))
        assert ts_body.get("l2_agent") == "localizer"
        assert ts_body.get("l2_phase") == "localize"
        assert ts_body.get("l2_repair_run_id") == state.repair_run_id
