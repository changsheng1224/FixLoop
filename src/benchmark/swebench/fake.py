"""无 API 的 SWE-bench 冒烟 Orchestrator：应用 gold patch 或写入占位 diff。"""

from __future__ import annotations

from pathlib import Path

from src.eval.patch_utils import apply_unified_patch
from src.state import CandidatePatch, RepairState, VerificationResult


class FakeGoldPatchOrchestrator:
    """将 instance.gold patch 应用到 repo，供 Adapter --fake。"""

    def __init__(self, repo_path: str, gold_patch: str = ""):
        self._repo = Path(repo_path)
        self._gold = gold_patch or ""

    def repair(self, issue: str, max_retries: int = 3, repair_timeout_s: int = 180, **kwargs) -> RepairState:
        state = RepairState(issue_input=issue, max_retries=max_retries)
        del repair_timeout_s, kwargs
        patch = self._gold.strip()
        if not patch:
            # 无 gold 时写一个可 diff 的占位文件，保证 predictions 非空
            marker = self._repo / ".fixloop_swebench_fake.txt"
            marker.write_text("fake-patch\n", encoding="utf-8")
            patch = (
                "diff --git a/.fixloop_swebench_fake.txt b/.fixloop_swebench_fake.txt\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                "+++ b/.fixloop_swebench_fake.txt\n"
                "@@ -0,0 +1 @@\n"
                "+fake-patch\n"
            )
            state.candidate_patches = [
                CandidatePatch(file_path=".fixloop_swebench_fake.txt", diff=patch)
            ]
            state.status = "patched"
            return state

        try:
            apply_unified_patch(self._repo, patch)
            state.candidate_patches = [CandidatePatch(diff=patch)]
            state.status = "fixed"
            # 冒烟：gold 应用成功视为已通过 FixLoop verify，才可进 harness 过滤
            state.verification_result = VerificationResult(all_passed=True)
        except Exception as exc:  # noqa: BLE001
            state.status = "failed"
            state.agent_errors["orchestrator"] = str(exc)
        return state


def make_fake_factory(gold_by_id: dict[str, str]):
    """按 instance 目录名 / 当前不绑定 id：闭包在 runner 内更好。

    CLI 使用：factory 忽略 gold，写占位；测试可直接构造 FakeGoldPatchOrchestrator。
    """

    def factory(repo_path: str) -> FakeGoldPatchOrchestrator:
        # repo 目录名即 instance_id（sanitize 后）
        name = Path(repo_path).name
        gold = gold_by_id.get(name, "")
        return FakeGoldPatchOrchestrator(repo_path, gold)

    return factory
