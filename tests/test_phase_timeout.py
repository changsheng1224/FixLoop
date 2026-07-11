"""分阶段 repair 超时集成单测。"""

from __future__ import annotations

import time

from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.workspace import WorkspaceContext
from src.agents.localizer import create_localizer
from src.agents.patcher import create_patcher
from src.agents.retriever import create_retriever
from src.orchestrator import Orchestrator
from src.repair.phase_clock import PhaseTimeoutConfig
from src.repair.termination import RepairTerminalStatus


def _slow_client(outputs, delay_s: float = 2.0) -> FakeModelClient:
    class SlowClient(FakeModelClient):
        def complete(self, prompt, max_new_tokens=None, prompt_cache_key=""):
            time.sleep(delay_s)
            return super().complete(
                prompt, max_new_tokens=max_new_tokens, prompt_cache_key=prompt_cache_key
            )

    return SlowClient(outputs)


class TestPhaseTimeoutIntegration:
    def test_localize_phase_timeout(self, temp_workspace):
        (temp_workspace / "calc.py").write_text("x = 1\n", encoding="utf-8")
        ws = WorkspaceContext.build(str(temp_workspace))
        loc = _slow_client(
            [
                '<final>[{"file_path":"calc.py","start_line":1,"end_line":1,'
                '"reason":"x","confidence":0.9}]</final>',
            ],
            delay_s=2.0,
        )
        ret = FakeModelClient(['<final>{"related_tests":[]}</final>'])
        pat = FakeModelClient(['<final>[]</final>'])
        orch = Orchestrator(
            create_localizer(loc, ws),
            create_retriever(ret, ws),
            create_patcher(pat, ws),
        )
        cfg = PhaseTimeoutConfig(localize_s=1, patch_s=0, verify_s=0, repair_total_s=0)
        state = orch.repair(
            "TypeError at calc.py:1",
            repair_timeout_s=0,
            phase_timeouts=cfg,
        )
        assert state.status == RepairTerminalStatus.TIMEOUT
        assert state.node_timings.get("phase_timeout") == "localize"
        assert "phase timeout" in state.agent_errors.get("orchestrator", "").lower()
        assert state.failure_tags == ["timeout"]

    def test_patch_phase_timeout_cumulative(self, temp_workspace):
        (temp_workspace / "calc.py").write_text("old\n", encoding="utf-8")
        ws = WorkspaceContext.build(str(temp_workspace))
        loc = FakeModelClient(
            [
                '<final>[{"file_path":"calc.py","start_line":1,"end_line":1,'
                '"reason":"x","confidence":0.9}]</final>',
            ]
        )
        ret = FakeModelClient(['<final>{"related_tests":[]}</final>'])
        pat = _slow_client(
            ['<final>[{"file_path":"calc.py","diff":"-old\\n+new","explanation":"fix"}]</final>'],
            delay_s=2.0,
        )
        orch = Orchestrator(
            create_localizer(loc, ws),
            create_retriever(ret, ws),
            create_patcher(pat, ws),
        )
        cfg = PhaseTimeoutConfig(localize_s=0, patch_s=1, verify_s=0, repair_total_s=0)
        state = orch.repair(
            "TypeError at calc.py:1",
            repair_timeout_s=0,
            phase_timeouts=cfg,
        )
        assert state.status == RepairTerminalStatus.TIMEOUT
        assert state.node_timings.get("phase_timeout") == "patch"
