"""Fake Orchestrator：应用 expected_patch.diff，供 Runner 单测与 --fake 模式。"""

from __future__ import annotations

from pathlib import Path

from src.eval.runner import DEFAULT_CASES_DIR, apply_expected_patch_to_repo
from src.state import RepairState


class FakePatchOrchestrator:
    """将 case 的 expected_patch 应用到临时 repo。"""

    def __init__(self, repo_path: str, cases_dir: Path | None = None):
        self._repo = Path(repo_path)
        self._cases_dir = Path(cases_dir or DEFAULT_CASES_DIR)
        self._case_id: str | None = None

    def repair(self, issue: str, max_retries: int = 3, repair_timeout_s: int = 180) -> RepairState:
        """应用 expected_patch.diff（需事先设置 _case_id）。"""
        state = RepairState(issue_input=issue, max_retries=max_retries)
        case_id = self._case_id
        if not case_id:
            state.status = "failed"
            state.agent_errors["orchestrator"] = "fake: case_id not set"
            return state

        case_dir = self._cases_dir / case_id
        try:
            apply_expected_patch_to_repo(self._repo, case_dir)
            state.status = "fixed"
            state.retry_count = 0
        except Exception as exc:
            state.status = "failed"
            state.agent_errors["orchestrator"] = str(exc)
        return state


def fake_orchestrator_factory(cases_dir: str | Path):
    """返回 FakePatchOrchestrator 工厂，供 --fake eval 使用。"""
    cases_path = Path(cases_dir)

    def factory(repo_path: str) -> FakePatchOrchestrator:
        return FakePatchOrchestrator(repo_path, cases_path)

    return factory
