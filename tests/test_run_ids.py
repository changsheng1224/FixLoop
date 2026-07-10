"""Unified run_id generation (Bonus6 §19.1)."""

import uuid

from agent_runtime.run_ids import is_valid_run_id, new_run_id
from agent_runtime.task_state import TaskState


class TestRunIds:
    def test_new_run_id_is_uuid_v4(self):
        rid = new_run_id()
        parsed = uuid.UUID(rid)
        assert parsed.version == 4
        assert is_valid_run_id(rid)

    def test_new_run_id_unique(self):
        ids = {new_run_id() for _ in range(20)}
        assert len(ids) == 20

    def test_is_valid_run_id_rejects_empty(self):
        assert not is_valid_run_id("")
        assert not is_valid_run_id("not-a-uuid")

    def test_task_state_create_uses_uuid(self):
        ts = TaskState.create(user_request="test")
        assert is_valid_run_id(ts.run_id)
        assert ts.task_id == ts.run_id

    def test_task_state_create_respects_injected_run_id(self):
        injected = "550e8400-e29b-41d4-a716-446655440000"
        ts = TaskState.create(user_request="test", run_id=injected)
        assert ts.run_id == injected
