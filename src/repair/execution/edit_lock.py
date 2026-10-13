"""文件锁定与未读不可写（Patcher Primary Phase A）。"""

from __future__ import annotations

from pathlib import Path

__all__ = ["EditLockState", "normalize_repo_rel"]


def normalize_repo_rel(path: str, repo_root: str | Path | None = None) -> str:
    """归一化为仓库相对 posix 路径；拒绝 ``..`` 逃逸时返回空串。"""
    raw = (path or "").replace("\\", "/").strip()
    if not raw or raw.startswith("/"):
        # 绝对路径：若在 root 下则相对化
        if repo_root and raw:
            try:
                rel = Path(raw).resolve().relative_to(Path(repo_root).resolve())
                return str(rel).replace("\\", "/")
            except (ValueError, OSError):
                return ""
        return raw.lstrip("/")
    parts = []
    for p in raw.split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            return ""
        parts.append(p)
    return "/".join(parts)


class EditLockState:
    """维护 allowed_edit 与成功 Read 集合。"""

    def __init__(
        self,
        *,
        repo_root: str | Path,
        allowed_edit: set[str] | frozenset[str] | list[str] | None = None,
        max_expand: int = 2,
        max_auto_allow: int = 5,
        sandbox_only: bool = False,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.sandbox_only = sandbox_only
        self.allowed_edit: set[str] = {
            normalize_repo_rel(p, self.repo_root) for p in (allowed_edit or []) if p
        }
        self.allowed_edit.discard("")
        self.allowed_create: set[str] = set()
        self.allowed_delete: set[str] = set()
        self.read_set: set[str] = set()
        self._generation = 0
        self._required_read_generation: dict[str, int] = {}
        self._read_generation: dict[str, int] = {}
        self.unread_write_reject_count = 0
        self.apply_path_reject_count = 0
        self.apply_patch_ok_count = 0
        self.edit_lint_reject_count = 0
        self.max_expand = max(0, int(max_expand))
        self.expand_count = 0
        self.max_auto_allow = max(0, int(max_auto_allow))
        # 空种子时：禁止仅靠 read 自动灌锁，须先 expand_lock
        self.require_expand_before_auto = False
        # Phase B：每 turn 至多一次写
        self.write_serial = True
        self.write_done_this_turn = False

    def begin_turn(self) -> None:
        self.write_done_this_turn = False

    def mark_write_done(self) -> None:
        self.write_done_this_turn = True

    def seed_and_preread(self, paths: list[str] | set[str]) -> list[str]:
        """种子路径加入 allowed_edit，并对存在的文件预读进 read_set。"""
        seeded: list[str] = []
        for raw in paths or []:
            rel = normalize_repo_rel(raw, self.repo_root)
            if not rel:
                continue
            self.allowed_edit.add(rel)
            seeded.append(rel)
            if not self.sandbox_only:
                self.mark_read(rel)
        return seeded

    def grounded_paths(self) -> list[str]:
        from src.repair.localization.localize_quality import _is_test_path

        return [
            path
            for path in sorted(self.allowed_edit)
            if path in self.read_set and not _is_test_path(path)
        ]

    def mark_read(self, path: str, *, auto_allow_impl: bool = False) -> bool:
        if self.sandbox_only:
            return False
        rel = normalize_repo_rel(path, self.repo_root)
        if not rel:
            return False
        target = self.repo_root / rel
        try:
            if not target.is_file():
                return False
        except OSError:
            return False
        return self._accept_read(rel, auto_allow_impl=auto_allow_impl)

    def _accept_read(self, rel, *, auto_allow_impl=False):
        self.read_set.add(rel)
        self._read_generation[rel] = self._required_read_generation.get(rel, 0)
        from src.repair.language_capabilities import editable_source

        if auto_allow_impl and editable_source(rel) and not _is_test_path(rel):
            if self.require_expand_before_auto and self.expand_count <= 0:
                return True
            if rel not in self.allowed_edit and len(self.allowed_edit) < self.max_auto_allow:
                self.allowed_edit.add(rel)
        return True

    def _evidence_path(self, path, result):
        from agent_runtime.linux_sandbox.workspace_contract import relative_path

        try:
            rel = relative_path(path)
        except ValueError:
            return ""
        meta = result.metadata
        version = meta.get("workspace_versions", {}).get(rel)
        if (
            not result.ok
            or meta.get("actual_backend") != "linux_sandbox"
            or meta.get("sandbox_cleanup") != "confirmed"
            or not meta.get("sandbox_receipt_id")
            or not isinstance(version, str)
            or len(version) != 64
        ):
            return ""
        return rel

    def mark_read_evidence(self, path, result, *, auto_allow_impl=False):
        rel = self._evidence_path(path, result)
        return bool(rel) and self._accept_read(rel, auto_allow_impl=auto_allow_impl)

    def expand_lock_evidence(self, path, result):
        rel = self._evidence_path(path, result)
        if not rel:
            return False, "sandbox_read_evidence_required"
        return self._expand_checked(rel)

    def expand_lock(self, path: str, *, operation: str = "modify") -> tuple[bool, str]:
        """显式扩锁（默认最多 2 次）；扩后须再 read 才可写。"""
        if self.sandbox_only:
            return False, "sandbox_read_evidence_required"
        rel = normalize_repo_rel(path, self.repo_root)
        if not rel:
            return False, "path_escape_or_invalid"
        target = self.repo_root / rel
        if operation == "create":
            if target.exists() or target.is_symlink():
                return False, f"create_target_exists:{rel}"
            from agent_runtime.path_safety import resolve_under_root

            try:
                resolve_under_root(self.repo_root, rel)
            except ValueError:
                return False, "path_escape_or_invalid"
            return self._grant_operation(rel, operation)
        if operation == "delete":
            if not target.is_file():
                return False, f"not_a_file:{rel}"
            return self._grant_operation(rel, operation)
        if operation != "modify":
            return False, "invalid_edit_operation"
        if rel in self.allowed_edit:
            return True, "already_allowed"
        try:
            if not target.is_file():
                return False, f"not_a_file:{rel}"
        except OSError as e:
            return False, f"stat_failed:{e}"
        return self._expand_checked(rel)

    def _grant_operation(self, rel, operation):
        if operation not in {"create", "delete"}:
            return False, "invalid_edit_operation"
        paths = self.allowed_create if operation == "create" else self.allowed_delete
        if rel in paths:
            return True, "already_allowed"
        if self.expand_count >= self.max_expand:
            return False, f"expand_lock_max:{self.max_expand}"
        paths.add(rel)
        self.expand_count += 1
        self._generation += 1
        self._required_read_generation[rel] = self._generation
        return True, f"expanded:{rel}:{operation}"

    def expand_operation_evidence(self, path, result, operation):
        from agent_runtime.linux_sandbox.workspace_contract import relative_path

        try:
            rel = relative_path(path)
        except ValueError:
            return False, "path_escape_or_invalid"
        meta = result.metadata
        versions = meta.get("workspace_versions", {})
        if (
            not result.ok
            or meta.get("actual_backend") != "linux_sandbox"
            or meta.get("sandbox_cleanup") != "confirmed"
            or not meta.get("sandbox_receipt_id")
            or rel not in versions
        ):
            return False, "sandbox_path_evidence_required"
        if operation == "create" and versions[rel] is not None:
            return False, "create_target_exists"
        if operation == "delete" and not isinstance(versions[rel], str):
            return False, "delete_target_missing"
        return self._grant_operation(rel, operation)

    def _expand_checked(self, rel):
        if rel in self.allowed_edit:
            return True, "already_allowed"
        if self.expand_count >= self.max_expand:
            return False, f"expand_lock_max:{self.max_expand}"
        self.allowed_edit.add(rel)
        self.expand_count += 1
        self._generation += 1
        self._required_read_generation[rel] = self._generation
        return True, f"expanded:{rel}"

    def required_read_generation(self, path: str) -> int:
        rel = normalize_repo_rel(path, self.repo_root)
        return int(self._required_read_generation.get(rel, 0) or 0)

    def check_write(self, path: str, *, operation: str = "modify") -> tuple[bool, str]:
        rel = normalize_repo_rel(path, self.repo_root)
        if not rel:
            self.apply_path_reject_count += 1
            return False, "path_escape_or_invalid"
        paths = {
            "modify": self.allowed_edit,
            "create": self.allowed_create,
            "delete": self.allowed_delete,
        }.get(operation, set())
        if rel not in paths:
            self.apply_path_reject_count += 1
            return False, f"not_in_allowed_edit:{rel}"
        if operation == "create":
            if not self.sandbox_only and (self.repo_root / rel).exists():
                return False, f"create_target_exists:{rel}"
            return True, ""
        required_generation = int(self._required_read_generation.get(rel, 0) or 0)
        read_generation = int(self._read_generation.get(rel, -1) or 0)
        if rel not in self.read_set or read_generation < required_generation:
            self.unread_write_reject_count += 1
            return False, (
                f"unread_before_write:{rel}:"
                f"read_generation={read_generation}:required={required_generation}"
            )
        return True, ""

    def check_patch_paths(self, paths: list[str]) -> tuple[bool, list[str]]:
        bad: list[str] = []
        for p in paths or []:
            ok, _ = self.check_write(p)
            if not ok:
                rel = normalize_repo_rel(p, self.repo_root) or p
                bad.append(rel)
        return (not bad), bad


def _is_test_path(path: str) -> bool:
    p = path.replace("\\", "/").lower()
    return (
        "/tests/" in f"/{p}"
        or p.startswith("tests/")
        or p.startswith("test_")
        or p.endswith("_test.py")
        or p.endswith("/conftest.py")
    )
