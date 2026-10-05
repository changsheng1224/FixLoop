"""Workspace-scoped edit policy injected by the application layer."""

from typing import Protocol


class EditPolicy(Protocol):
    allowed_edit: set[str]
    write_serial: bool
    write_done_this_turn: bool
    edit_lint_reject_count: int
    apply_patch_ok_count: int

    def mark_read(self, path: str) -> bool: ...
    def check_write(self, path: str) -> tuple[bool, str]: ...
    def begin_turn(self) -> None: ...
    def mark_write_done(self) -> None: ...
