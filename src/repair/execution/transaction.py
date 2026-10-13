"""Patch application and lossless rollback, fenced by execution cleanup."""

from __future__ import annotations

from pathlib import Path

from src.repair.execution.patch_applier import PatchApplier
from src.repair.repo_snapshot import RollbackUnconfirmedError, restore_repo_snapshot, snapshot_repo


class PatchTransactionMixin:
    def _patch_applier(self) -> PatchApplier:
        return PatchApplier(self._repo_root)

    def _resolve_repo_file(self, file_path: str) -> Path | None:
        return self._patch_applier().resolve_repo_file(file_path)

    def _snapshot_repo(self) -> dict[str, str]:
        return snapshot_repo(self._repo_root, sandbox_context=self._sandbox_context)

    def _restore_repo_snapshot(self, snapshot: dict[str, str]) -> None:
        from agent_runtime.test_execution import ProcessCleanupUnconfirmedError

        try:
            self._restore_repo_snapshot_impl(snapshot)
        except ProcessCleanupUnconfirmedError:
            raise
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            raise RollbackUnconfirmedError(str(exc)) from exc

    def _restore_repo_snapshot_impl(self, snapshot: dict[str, str]) -> None:
        from agent_runtime.test_execution import ProcessCleanupUnconfirmedError

        service = getattr(self, "_verify_service", None)
        if service is not None and (service.active.is_set() or not service.cleanup_confirmed):
            raise ProcessCleanupUnconfirmedError(
                "rollback blocked by active/unconfirmed verification"
            )
        if self._sandbox_context is not None:
            restore_repo_snapshot(self._repo_root, snapshot, sandbox_context=self._sandbox_context)
            return
        binding = self._plan_binding
        if binding is None:
            restore_repo_snapshot(self._repo_root, snapshot)
            return
        from agent_runtime.plan_runtime.workspace import IGNORED
        from agent_runtime.plan_runtime.workspace import snapshot as content_snapshot

        expected = snapshot.expected_versions(content_snapshot(self._repo_root), ignored=IGNORED)
        binding.rollback(lambda: restore_repo_snapshot(self._repo_root, snapshot), expected)
