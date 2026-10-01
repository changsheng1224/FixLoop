"""Production L2 repair: model delegation, actual reads, owner edit and host pytest."""

import json
import threading
import time

import pytest

from agent_runtime.providers.clients import FakeModelClient, FakeNativeToolClient
from agent_runtime.providers.contracts import ProviderCapabilities
from tests.plan_l2_support import repair_fixture
from tests.test_exploration_runtime import REQUESTS, DiscoveryClient


class OwnerClient(FakeModelClient):
    def __init__(self, root):
        self.root = root
        self.exploration = None
        super().__init__(
            [
                '{"conclusion":"Inspect answer in value.py, discover related tests, and change return 1 to 2."}',
                "<tool>"
                + json.dumps({"name": "delegate_exploration", "args": {"tasks": REQUESTS}})
                + "</tool>",
                "COLLECT",
                '<tool>{"name":"read_file","args":{"path":"value.py"}}</tool>',
                '<tool>{"name":"read_file","args":{"path":"test_value.py"}}</tool>',
                '<tool>{"name":"patch_file","args":{"path":"value.py","old_text":"return 1","new_text":"return 2"}}</tool>',
                "<final>Reviewed both source findings and applied the fix. Run the existing test.</final>",
            ]
        )

    def complete(self, prompt, max_new_tokens=512, prompt_cache_key=""):
        if self._index == 2:
            from src.collaboration.exploration_store import ExplorationStore

            tasks = [
                t
                for t in ExplorationStore(str(self.root)).list_tasks("plan-fixture-run")
                if t.role == "explorer"
            ]
            assert len(tasks) == 2
            self._outputs[2] = (
                "<tool>"
                + json.dumps(
                    {
                        "name": "collect_exploration",
                        "args": {
                            "handles": [t.task_id for t in tasks],
                            "wait_ms": 1000,
                        },
                    }
                )
                + "</tool>"
            )
        return super().complete(prompt, max_new_tokens, prompt_cache_key)


class NativeOwnerClient(OwnerClient):
    @property
    def capabilities(self):
        return ProviderCapabilities(provider="fake", model="fake", native_tools=True, usage=True)

    def complete_turn(self, request):
        return FakeNativeToolClient.complete_turn(self, request)


@pytest.mark.parametrize("native", [False, True], ids=["text", "native"])
def test_owner_delegates_collects_reviews_updates_plan_and_alone_edits(tmp_path, native):
    orch, state, _ = repair_fixture(tmp_path)
    barrier, clients = threading.Barrier(2), []
    owner = NativeOwnerClient(tmp_path) if native else OwnerClient(tmp_path)
    orch.patcher.model_client = owner

    def factory(task):
        child = DiscoveryClient(task, barrier=barrier)
        clients.append(child)
        return child

    orch.patcher._exploration_client_factory = factory
    try:
        patches, meta = orch._run_patcher_toolized(
            state, "Delegate source and test discovery, then fix answer()", {}
        )
        assert patches, (meta, state.agent_errors, state.node_timings)
        binding = orch._plan_binding
        assert binding.session.plan.node("edit").status == "succeeded"
        assert len(clients) == 2 and all(len(c.requests) == 2 for c in clients)
        tasks = binding.exploration.tasks()
        assert len(tasks) == 2 and all(
            t.payload["exploration"]["status"] == "completed" for t in tasks
        )
        reviews = binding.session.store.latest("exploration_review", "review_id")
        assert len(reviews) == 2, (reviews, binding.session.store.events())
        assert all(
            binding.session.evidence.valid(ref, historical=True)
            for r in reviews.values()
            for ref in r["owner_evidence_refs"]
        )
        assert len(binding.session.long_task_state.key_decisions) >= 2
        operations = binding.session.store.latest("operation", "operation_id").values()
        assert len([o for o in operations if o["effect"] == "write"]) == 1
        result = orch._run_verifier(state)
        assert result.all_passed and result.total_tests == 1, result.failure_logs
        assert binding.session.plan.status == "completed"
        assert (tmp_path / "value.py").read_text() == "def answer():\n    return 2\n"
        events = binding.exploration.store.progress_events(state.repair_run_id)
        assert any(e["event"] == "subagent_result_collected" for e in events)
        progress = state.node_timings["exploration_progress"]
        assert not progress["progress_replay_incomplete"]
        assert len(next(iter(progress["turns"].values()))["calls"]) == 2
        manifest = {
            "plan": binding.session.plan.to_dict(),
            "owner_reviews": list(reviews.values()),
            "subagent_tasks": [t.to_dict() for t in tasks],
            "events": events,
            "owner_write_calls": 1,
            "pytest_tests": result.total_tests,
            "model_mode": "controlled ModelClient responses; real Agent/tool/Plan loops",
        }
        (binding.session.store.root / "subagent_acceptance.json").write_text(
            json.dumps(manifest, indent=2)
        )
    finally:
        if orch._plan_binding:
            orch._plan_binding.close()


def test_owner_lease_is_kept_alive_during_exploration_binding_initialization(tmp_path, monkeypatch):
    from agent_runtime.run_coordination import RunCoordinator
    from src.repair.plan_binding import RepairPlanBinding

    orch, state, _ = repair_fixture(tmp_path)
    coordinator = RunCoordinator(
        str(tmp_path), state.repair_run_id, state.repair_run_id, lease_seconds=0.3
    )
    coordinator.acquire()
    orch._entry_coordinator = coordinator
    original = RepairPlanBinding._open

    def slow_open(self, context, *, defer_plan):
        time.sleep(0.4)  # Longer than the original owner lease, before first dispatch.
        original(self, context, defer_plan=defer_plan)

    monkeypatch.setattr(RepairPlanBinding, "_open", slow_open)
    binding = RepairPlanBinding(orch, state)
    try:
        coordinator.assert_can_dispatch()
        assert binding.session.plan is not None
    finally:
        binding.close()
