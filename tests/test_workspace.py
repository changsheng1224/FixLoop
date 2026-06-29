"""WorkspaceContext 单测：git 仓库、非 git 目录降级、白名单文档。"""

from pathlib import Path

import pytest

from agent_runtime.workspace import DOC_NAMES, WorkspaceContext


class TestWorkspaceInGitRepo:
    """git 仓库中的工作区采集。"""

    def test_repo_root_found(self, temp_workspace):
        ws = WorkspaceContext.build(str(temp_workspace))
        assert Path(ws.repo_root) == temp_workspace.resolve()

    def test_branch_detected(self, temp_workspace):
        ws = WorkspaceContext.build(str(temp_workspace))
        # 默认分支可能是 master 或 main
        assert ws.branch in ("master", "main", "")

    def test_recent_commits(self, temp_workspace):
        ws = WorkspaceContext.build(str(temp_workspace))
        # 至少有一次 commit（initial commit）
        assert len(ws.recent_commits) > 0

    def test_whitelist_docs_loaded(self, temp_workspace):
        ws = WorkspaceContext.build(str(temp_workspace))
        # README.md 和 pyproject.toml 在 conftest fixture 中创建
        assert "README.md" in ws.doc_contents
        assert "pyproject.toml" in ws.doc_contents
        assert "# Test Project" in ws.doc_contents["README.md"]

    def test_text_output_format(self, temp_workspace):
        ws = WorkspaceContext.build(str(temp_workspace))
        text = ws.text()
        assert "Workspace:" in text
        assert "cwd:" in text
        assert "repo_root:" in text
        assert "branch:" in text

    def test_fingerprint_stable(self, temp_workspace):
        ws1 = WorkspaceContext.build(str(temp_workspace))
        ws2 = WorkspaceContext.build(str(temp_workspace))
        # 同目录连续两次 build 应得到相同指纹
        assert ws1.fingerprint() == ws2.fingerprint()
        assert len(ws1.fingerprint()) == 64  # SHA256 hex


class TestWorkspaceNonGit:
    """非 git 目录中的降级行为。"""

    def test_repo_root_fallback(self, non_git_dir):
        ws = WorkspaceContext.build(str(non_git_dir))
        # 非 git 目录 → repo_root 应退化为 cwd
        assert Path(ws.repo_root) == non_git_dir.resolve()
        assert ws.branch == ""
        assert ws.git_status == ""
        assert ws.recent_commits == ""

    def test_no_crash_on_missing_git(self, non_git_dir):
        """确保没有 git 时不会抛异常。"""
        try:
            _ = WorkspaceContext.build(str(non_git_dir))
        except Exception as e:
            pytest.fail(f"WorkspaceContext.build 在非 git 目录抛出异常: {e}")

    def test_text_output_non_git(self, non_git_dir):
        ws = WorkspaceContext.build(str(non_git_dir))
        text = ws.text()
        assert "Workspace:" in text
        assert "cwd:" in text
        # 没有 git 信息时不出现 branch/status 行
        assert "branch:" not in text


class TestDocLoading:
    """白名单文档加载。"""

    def test_doc_names_set(self):
        """验证 DOC_NAMES 包含常用白名单文档。"""
        assert "README.md" in DOC_NAMES
        assert "pyproject.toml" in DOC_NAMES

    def test_missing_docs_handled(self, temp_workspace):
        """缺失的文档不会导致错误。"""
        # 删除 README.md
        (temp_workspace / "README.md").unlink()
        ws = WorkspaceContext.build(str(temp_workspace))
        # README 不应在 doc_contents 中
        assert "README.md" not in ws.doc_contents
        # pyproject.toml 仍应存在
        assert "pyproject.toml" in ws.doc_contents
