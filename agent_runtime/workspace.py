"""工作区上下文：采集 git 仓库信息和白名单文档。"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

# 白名单文档：启动时自动加载内容
DOC_NAMES = ("AGENTS.md", "README.md", "pyproject.toml", "CLAUDE.md")

# fingerprint 忽略的 untracked/dirty 路径前缀（噪声目录）
FINGERPRINT_EXCLUDE_PREFIXES = (
    ".agent/",
    "__pycache__/",
    ".pytest_cache/",
)


@dataclass
class WorkspaceContext:
    """工作区快照，提供给 Agent 作为上下文。

    包含：cwd、repo_root、git branch、git status、最近 commit、白名单文档内容。
    """

    cwd: str = ""
    repo_root: str = ""
    branch: str = ""
    git_status: str = ""
    recent_commits: str = ""
    doc_contents: dict = None  # type: ignore
    head: str = ""
    dirty_file_hashes: dict = None  # type: ignore

    @classmethod
    def build(cls, cwd: str = ".") -> "WorkspaceContext":
        """从给定目录采集工作区信息。

        Args:
            cwd: 工作目录路径，默认为当前目录。

        Returns:
            WorkspaceContext 实例。
        """
        root = Path(cwd).resolve()
        repo_root = cls._find_repo_root(root)
        branch = cls._get_branch(repo_root)
        git_status = cls._get_git_status(repo_root)
        recent_commits = cls._get_recent_commits(repo_root)
        doc_contents = cls._load_docs(repo_root)
        head = cls._get_head(repo_root) if cls._is_git_repo(repo_root) else ""
        dirty_file_hashes = cls._dirty_file_hashes(repo_root) if head else {}

        return cls(
            cwd=str(root),
            repo_root=str(repo_root),
            branch=branch,
            git_status=git_status,
            recent_commits=recent_commits,
            doc_contents=doc_contents,
            head=head,
            dirty_file_hashes=dirty_file_hashes,
        )

    def text(self) -> str:
        """格式化为 Agent prompt 中的 Workspace 部分。"""
        lines = [
            "Workspace:",
            f"  cwd: {self.cwd}",
            f"  repo_root: {self.repo_root}",
        ]
        if self.branch:
            lines.append(f"  branch: {self.branch}")
        if self.git_status:
            lines.append(f"  git_status: {self.git_status}")
        if self.recent_commits:
            lines.append(f"  recent_commits:\n{self.recent_commits}")
        # 白名单文档
        if self.doc_contents:
            doc_lines = ["  docs:"]
            for name, content in self.doc_contents.items():
                preview = content[:200].replace("\n", " ")
                doc_lines.append(f"    {name}: {preview}")
            lines.extend(doc_lines)
        return "\n".join(lines)

    def fingerprint(self) -> str:
        """工作区语义指纹（SHA256），用于 prefix_hashes / trace 观测。

        基于 HEAD + dirty 文件内容 hash + 白名单文档全文 hash；
        与 ``text()`` 展示解耦，避免 git status 文本顺序 / recent_commits 噪声。
        """
        payload = self._fingerprint_payload()
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _fingerprint_payload(self) -> dict:
        dirty = dict(sorted((self.dirty_file_hashes or {}).items()))
        docs = {
            name: _sha256_hex(content)
            for name, content in sorted((self.doc_contents or {}).items())
        }
        return {
            "repo_root": self.repo_root,
            "branch": self.branch,
            "head": self.head,
            "dirty": dirty,
            "docs": docs,
        }

    # ---- 内部方法 ----

    @staticmethod
    def _find_repo_root(path: Path) -> Path:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=str(path),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return Path(result.stdout.strip())
        except Exception:
            pass
        return path

    @staticmethod
    def _is_git_repo(repo_root: Path) -> bool:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _get_branch(repo_root: Path) -> str:
        try:
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _get_head(repo_root: Path) -> str:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _get_git_status(repo_root: Path) -> str:
        try:
            result = subprocess.run(
                ["git", "status", "--short"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _get_recent_commits(repo_root: Path) -> str:
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "-5"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _load_docs(repo_root: Path) -> dict:
        docs = {}
        for doc_name in DOC_NAMES:
            doc_path = repo_root / doc_name
            if doc_path.is_file():
                try:
                    docs[doc_name] = doc_path.read_text(encoding="utf-8")
                except Exception:
                    pass
        return docs

    @classmethod
    def _dirty_file_hashes(cls, repo_root: Path) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for status, path in cls._parse_porcelain_status(repo_root):
            if cls._should_exclude_dirty_path(path):
                continue
            if status[0] == "D" or status[1] == "D":
                hashes[path] = _sha256_hex(b"")
                continue
            full = repo_root / path
            hashes[path] = _content_hash(full)
        return dict(sorted(hashes.items()))

    @staticmethod
    def _parse_porcelain_status(repo_root: Path) -> list[tuple[str, str]]:
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain", "-z"],
                cwd=str(repo_root),
                capture_output=True,
                timeout=5,
            )
            if result.returncode != 0:
                return []
        except Exception:
            return []

        entries: list[tuple[str, str]] = []
        tokens = [token for token in result.stdout.split(b"\0") if token]
        idx = 0
        while idx < len(tokens):
            line = tokens[idx].decode("utf-8", errors="replace")
            if len(line) < 3 or line[2] != " ":
                idx += 1
                continue
            status = line[:2]
            path = line[3:]
            if "R" in status or "C" in status:
                if idx + 1 < len(tokens):
                    path = tokens[idx + 1].decode("utf-8", errors="replace")
                    idx += 2
                else:
                    idx += 1
            else:
                idx += 1
            if path:
                entries.append((status, path))
        return entries

    @staticmethod
    def _should_exclude_dirty_path(path: str) -> bool:
        normalized = path.replace("\\", "/")
        for prefix in FINGERPRINT_EXCLUDE_PREFIXES:
            bare = prefix.rstrip("/")
            if normalized == bare or normalized.startswith(prefix):
                return True
        return False


def _sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _content_hash(path: Path) -> str:
    if not path.is_file():
        return _sha256_hex(b"")
    try:
        return _sha256_hex(path.read_bytes())
    except OSError:
        return _sha256_hex(b"")
