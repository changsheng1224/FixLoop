"""敏感产物加密接线单测：FIXLOOP_ENCRYPT_KEY → 落盘非明文 + 可读回。"""

import json
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def encryption_key():
    """生成临时 Fernet 密钥。"""
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


class TestEncryptTaskState:
    def test_no_key_writes_plaintext(self, temp_workspace):
        """未设 FIXLOOP_ENCRYPT_KEY 时 task_state 明文。"""
        from agent_runtime.run_store import RunStore
        from agent_runtime.task_state import TaskState

        store = RunStore(root=str(temp_workspace))
        ts = TaskState.create(user_request="sensitive issue data")
        ts.finish_success("sensitive patch content")

        store.write_task_state(ts)
        run_dir = store.runs_dir / ts.run_id
        data = json.loads((run_dir / "task_state.json").read_text(encoding="utf-8"))
        assert data["user_request"] == "sensitive issue data"
        assert data["final_answer"] == "sensitive patch content"

    def test_with_key_writes_ciphertext(self, temp_workspace, encryption_key, monkeypatch):
        """设置 FIXLOOP_ENCRYPT_KEY 时 task_state 加密。"""
        monkeypatch.setenv("FIXLOOP_ENCRYPT_KEY", encryption_key)
        # reload crypto_utils to pick up the key
        import importlib
        import agent_runtime.crypto_utils as cu
        importlib.reload(cu)

        from agent_runtime.run_store import RunStore
        from agent_runtime.task_state import TaskState

        store = RunStore(root=str(temp_workspace))
        ts = TaskState.create(user_request="sensitive issue data")
        ts.finish_success("sensitive patch content")

        store.write_task_state(ts)
        run_dir = store.runs_dir / ts.run_id
        data = json.loads((run_dir / "task_state.json").read_text(encoding="utf-8"))

        # 落盘非明文
        assert data["user_request"] != "sensitive issue data"
        assert data["final_answer"] != "sensitive patch content"
        assert data["_encrypted"] is True

        # 可读回
        assert cu.decrypt(data["user_request"]) == "sensitive issue data"
        assert cu.decrypt(data["final_answer"]) == "sensitive patch content"

    def test_encryption_flag_present(self, temp_workspace, encryption_key, monkeypatch):
        """加密时 _encrypted=True 标记存在。"""
        monkeypatch.setenv("FIXLOOP_ENCRYPT_KEY", encryption_key)
        import importlib
        import agent_runtime.crypto_utils as cu
        importlib.reload(cu)

        from agent_runtime.run_store import RunStore
        from agent_runtime.task_state import TaskState

        store = RunStore(root=str(temp_workspace))
        ts = TaskState.create(user_request="test")
        ts.finish_success("done")
        store.write_task_state(ts)

        run_dir = store.runs_dir / ts.run_id
        data = json.loads((run_dir / "task_state.json").read_text(encoding="utf-8"))
        assert data["_encrypted"] is True

    def test_empty_fields_not_encrypted(self, temp_workspace, encryption_key, monkeypatch):
        """空字段不加密。"""
        monkeypatch.setenv("FIXLOOP_ENCRYPT_KEY", encryption_key)
        import importlib
        import agent_runtime.crypto_utils as cu
        importlib.reload(cu)

        from agent_runtime.run_store import RunStore
        from agent_runtime.task_state import TaskState

        store = RunStore(root=str(temp_workspace))
        ts = TaskState.create(user_request="")
        ts.final_answer = ""
        store.write_task_state(ts)

        run_dir = store.runs_dir / ts.run_id
        data = json.loads((run_dir / "task_state.json").read_text(encoding="utf-8"))
        assert data["user_request"] == ""
        assert data["final_answer"] == ""
