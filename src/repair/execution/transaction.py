"""Patch application and lossless rollback, fenced by execution cleanup."""

from __future__ import annotations

from pathlib import Path

from src.repair.execution.patch_applier import PatchApplier
from src.repair.repo_snapshot import RollbackUnconfirmedError, restore_repo_snapshot, snapshot_repo
from src.state import CandidatePatch


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

    def _apply_patches_on_disk(
        self, patches: list[CandidatePatch], *, state=None
    ) -> list[CandidatePatch]:
        # faithfulness 闸口：过滤操作无关文件的幻觉 patch（soft_keep 防空导出）
        if state is not None:
            from src.repair.failure_tags import check_patch_faithfulness

            if not state.node_timings.get("_repo_root_hint"):
                state.node_timings["_repo_root_hint"] = str(self._repo_root or "")
            patches, rejected = check_patch_faithfulness(
                patches, state, soft_keep=True, repo_root=str(self._repo_root or "")
            )
            if rejected:
                state.agent_errors["patcher_hallucinated_file"] = rejected[0]
                import sys

                print(
                    f"  [patcher] ⚠ faithfulness gate: 拒绝无关文件 patch {rejected}",
                    file=sys.stderr,
                    flush=True,
                )
            if state.node_timings.get("faithfulness_soft") or state.node_timings.get(
                "faithfulness_promoted"
            ):
                print(
                    "  [patcher] faithfulness soft/promoted — kept existing impl files",
                    file=sys.stderr,
                    flush=True,
                )
        # Patcher-primary：allowed_edit + 未读不可写
        lock = getattr(self, "_edit_lock", None)
        if lock is not None and patches:
            kept: list[CandidatePatch] = []
            for p in patches:
                ok, reason = lock.check_write(p.file_path or "")
                if ok:
                    kept.append(p)
                else:
                    import sys

                    print(
                        f"  [edit_lock] reject {p.file_path}: {reason}",
                        file=sys.stderr,
                        flush=True,
                    )
                    if state is not None:
                        state.agent_errors["edit_lock"] = reason
                        state.node_timings["unread_write_reject_count"] = (
                            lock.unread_write_reject_count
                        )
                        state.node_timings["apply_path_reject_count"] = lock.apply_path_reject_count
            patches = kept
        applier = self._patch_applier()
        allowlist = None
        if lock is not None and getattr(lock, "allowed_edit", None):
            allowlist = set(lock.allowed_edit)
        applied = applier.apply_patches(patches, allowed_paths=allowlist)
        self._last_sibling_warnings = list(applier.last_sibling_warnings or [])
        self._last_apply_errors = list(getattr(applier, "last_apply_errors", None) or [])
        return applied
