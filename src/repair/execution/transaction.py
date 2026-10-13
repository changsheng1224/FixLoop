from __future__ import annotations

from pathlib import Path

from agent_runtime.logging_setup import get_logger
from src.repair.repo_snapshot import RollbackUnconfirmedError, restore_repo_snapshot, snapshot_repo
from src.state import RepairState

log = get_logger("repair.pipeline")


class PatchTransaction:
    def __init__(self, session):
        self.session = session

    def resolve_repo_file(self, file_path: str) -> Path | None:
        from src.repair.path_resolve import resolve_repo_file

        return resolve_repo_file(self.session.repo_root, file_path)

    def snapshot_repo(self) -> dict[str, str]:
        return snapshot_repo(self.session.repo_root, sandbox_context=self.session.sandbox_context)

    def restore_repo_snapshot(self, snapshot: dict[str, str]) -> None:
        from agent_runtime.test_execution import ProcessCleanupUnconfirmedError

        try:
            self.restore_repo_snapshot_impl(snapshot)
        except ProcessCleanupUnconfirmedError:
            raise
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            raise RollbackUnconfirmedError(str(exc)) from exc

    def restore_repo_snapshot_impl(self, snapshot: dict[str, str]) -> None:
        from agent_runtime.test_execution import ProcessCleanupUnconfirmedError

        service = getattr(self.session, "verify_service", None)
        if service is not None and (service.active.is_set() or not service.cleanup_confirmed):
            raise ProcessCleanupUnconfirmedError(
                "rollback blocked by active/unconfirmed verification"
            )
        if self.session.sandbox_context is not None:
            restore_repo_snapshot(
                self.session.repo_root, snapshot, sandbox_context=self.session.sandbox_context
            )
            return
        binding = self.session.plan_binding
        if binding is None:
            restore_repo_snapshot(self.session.repo_root, snapshot)
            return
        from agent_runtime.plan_runtime.workspace import IGNORED
        from agent_runtime.plan_runtime.workspace import snapshot as content_snapshot

        expected = snapshot.expected_versions(
            content_snapshot(self.session.repo_root), ignored=IGNORED
        )
        binding.rollback(lambda: restore_repo_snapshot(self.session.repo_root, snapshot), expected)

    def salvage_patches_from_disk(self, state: RepairState, initial_snapshot: dict) -> list:
        """对比初始快照与当前工作树，回收已落盘但未登记的补丁。"""
        try:
            after = self.snapshot_repo()
        except Exception as e:
            log.warning("[timeout] salvage snapshot failed: %s", e)
            return []
        from src.repair.execution.edit_from_disk import patches_from_snapshot_diff
        from src.repair.failure_tags import check_patch_faithfulness
        from src.repair.path_resolve import is_impl_py_path

        patches = patches_from_snapshot_diff(initial_snapshot, after, explanation="timeout_salvage")
        if not patches:
            return []
        kept, _ = check_patch_faithfulness(
            patches, state, soft_keep=True, repo_root=str(self.session.repo_root or "")
        )
        if kept:
            return kept
        impl = [p for p in patches if p.file_path and is_impl_py_path(p.file_path)]
        return impl[:8] or patches[:4]

    def reject_protected_interface_changes(
        self, state: RepairState, initial_snapshot: dict
    ) -> bool:
        """Enforce explicitly protected interfaces, not all AST definition sets."""
        from src.repair.execution.interface_policy import protected_interface_changes

        protected = self.session.repair_config.protected_interfaces
        for patch in state.candidate_patches:
            changed = protected_interface_changes(
                patch.original_lines, patch.patched_lines, protected.get(patch.file_path, ())
            )
            if not changed:
                continue
            detail = f"{patch.file_path}: {', '.join(changed)}"
            state.agent_errors["protected_interface"] = detail
            state.feedback = f"受保护接口被修改: {detail}。请保留这些接口的签名和定义。"
            self.restore_repo_snapshot(initial_snapshot)
            state.candidate_patches = []
            state.retry_count += 1
            self.session.events.checkpoint_progress(state)
            return True
        return False
