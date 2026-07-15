"""QuotaEnforcer 单测。"""

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.tool_executor import QuotaEnforcer, ToolExecutor
from agent_runtime.workspace import WorkspaceContext


@pytest.fixture
def agent(temp_workspace):
    config = AgentConfig(provider="fake", max_steps=3, approval="auto")
    ws = WorkspaceContext.build(str(temp_workspace))
    client = FakeModelClient(["<final>ok</final>"])
    return Agent(config=config, model_client=client, workspace=ws)


class TestQuotaEnforcer:
    """QuotaEnforcer 独立测试。"""

    def test_initial_status(self):
        q = QuotaEnforcer(max_writes=5, max_shell=3, max_total=10)
        assert q.check("write_file") is True
        assert q.check("list_files") is True

    def test_write_quota_exceeded(self):
        q = QuotaEnforcer(max_writes=2, max_total=10)
        q.record("write_file")
        q.record("write_file")
        assert q.check("write_file") is False

    def test_total_quota_exceeded(self):
        q = QuotaEnforcer(max_total=2)
        q.record("list_files")
        q.record("search")
        assert q.check("list_files") is False  # total 超限

    def test_readonly_unlimited(self):
        q = QuotaEnforcer(max_total=1)
        q.record("list_files")  # total 用完
        # readonly 也受限（total 超了）
        assert q.check("read_file") is False

    def test_status_string(self):
        q = QuotaEnforcer(max_writes=20, max_shell=10, max_total=50)
        q.record("write_file")
        status = q.status()
        assert "1/20" in status
        assert "1/50" in status


class TestQuotaIntegration:
    """QuotaEnforcer 集成到 ToolExecutor 测试。"""

    def test_quota_rejects_after_limit(self, agent):
        quota = QuotaEnforcer(max_writes=1, max_total=10)
        executor = ToolExecutor(agent=agent, approval_policy="auto", quota=quota)

        # 第一次成功
        r1 = executor.execute("write_file", {"path": "a.txt", "content": "x"})
        assert r1.metadata["tool_status"] == "success"

        # 第二次被拒
        r2 = executor.execute("write_file", {"path": "b.txt", "content": "y"})
        assert r2.metadata["tool_status"] == "rejected"
        assert r2.metadata["tool_error_code"] == "quota_exceeded"

    def test_quota_does_not_block_readonly(self, agent):
        quota = QuotaEnforcer(max_writes=0, max_total=10)
        executor = ToolExecutor(agent=agent, approval_policy="auto", quota=quota)

        r = executor.execute("list_files", {"path": "."})
        assert r.metadata["tool_status"] == "success"

    def test_denied_run_shell_releases_concurrent_slot(self, agent):
        quota = QuotaEnforcer(max_shell=10, max_total=10, max_concurrent_shell=1)
        executor = ToolExecutor(agent=agent, approval_policy="auto", quota=quota)

        result = executor.execute("run_shell", {"command": "echo hi", "timeout": 1})

        assert result.metadata["tool_status"] == "rejected"
        assert result.metadata["tool_error_code"] == "approval_denied"
        assert quota.acquire_shell() is True


class TestConcurrentShellLimit:
    def test_acquire_blocks_when_full(self):
        from agent_runtime.tool_executor import QuotaEnforcer

        q = QuotaEnforcer(max_concurrent_shell=2)
        assert q.acquire_shell() is True
        assert q.acquire_shell() is True
        assert q.acquire_shell() is False  # 第 3 个被拒
        q.release_shell()
        assert q.acquire_shell() is True  # 释放后可用

    def test_release_without_acquire_is_safe(self):
        from agent_runtime.tool_executor import QuotaEnforcer

        q = QuotaEnforcer()
        q.release_shell()  # 不应抛异常


# ---------------------------------------------------------------------------
# 分 Agent 配额（V1.5-Bonus5）
# ---------------------------------------------------------------------------


class TestRoleQuotas:
    def test_localizer_readonly_no_writes(self):
        from agent_runtime.runtime import _role_quota

        q = _role_quota("localizer")
        assert not q.check("write_file")
        assert not q.check("patch_file")
        assert not q.check("run_shell")
        assert q.check("read_file")

    def test_patcher_can_write(self):
        from agent_runtime.runtime import _role_quota

        q = _role_quota("patcher")
        assert q.check("write_file")
        assert q.check("patch_file")

    def test_localizer_depletion_does_not_affect_patcher(self):
        """Localizer 配额独立于 Patcher。"""
        from agent_runtime.runtime import _role_quota

        loc_q = _role_quota("localizer")
        pat_q = _role_quota("patcher")

        # 耗尽 localizer 的 total
        for _ in range(12):
            loc_q.record("read_file")
        assert not loc_q.check("read_file")

        # patcher 仍然可以 write
        assert pat_q.check("write_file")
        assert pat_q.check("patch_file")

    def test_verifier_sandbox_only(self):
        from agent_runtime.runtime import _role_quota

        q = _role_quota("verifier")
        # verifier 只能 run_shell (sandbox)，不能写文件
        assert not q.check("write_file")
        assert q.check("run_shell")

    def test_retriever_readonly(self):
        from agent_runtime.runtime import _role_quota

        q = _role_quota("retriever")
        assert not q.check("write_file")
        assert q.check("read_file")
        assert q.check("search")

    def test_all_roles_distinct(self):
        """四角色配额互不相同。"""
        from agent_runtime.runtime import _role_quota

        quotas = {
            role: _role_quota(role) for role in ("localizer", "retriever", "patcher", "verifier")
        }
        # 每个角色是独立实例
        ids = {id(q) for q in quotas.values()}
        assert len(ids) == 4
