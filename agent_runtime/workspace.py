"""工作区上下文：采集 git 仓库信息和白名单文档。"""

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

# 白名单文档：启动时自动加载内容
DOC_NAMES = ("AGENTS.md", "README.md", "pyproject.toml", "CLAUDE.md")


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

        return cls(
            cwd=str(root),
            repo_root=str(repo_root),
            branch=branch,
            git_status=git_status,
            recent_commits=recent_commits,
            doc_contents=doc_contents,
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
        """计算工作区指纹（SHA256），用于 prompt cache key 等场景。"""
        content = self.text().encode("utf-8")
        return hashlib.sha256(content).hexdigest()

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
