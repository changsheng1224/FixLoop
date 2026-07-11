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


class TestWorkspaceFingerprintDenoise:
    """fingerprint() 使用 dirty file-set content hash，与 text() 展示解耦。"""

    def test_fingerprint_changes_when_tracked_content_changes(self, temp_workspace):
        ws_before = WorkspaceContext.build(str(temp_workspace))
        before = ws_before.fingerprint()

        readme = temp_workspace / "README.md"
        readme.write_text(readme.read_text() + "\n# extra line\n")

        ws_after = WorkspaceContext.build(str(temp_workspace))
        assert ws_after.fingerprint() != before

    def test_fingerprint_stable_when_content_restored(self, temp_workspace):
        readme = temp_workspace / "README.md"
        original = readme.read_text()
        ws_clean = WorkspaceContext.build(str(temp_workspace))
        clean_fp = ws_clean.fingerprint()

        readme.write_text(original + "\ntemporary\n")
        WorkspaceContext.build(str(temp_workspace))

        readme.write_text(original)
        ws_restored = WorkspaceContext.build(str(temp_workspace))
        assert ws_restored.fingerprint() == clean_fp

    def test_fingerprint_ignores_excluded_untracked_paths(self, temp_workspace):
        ws_clean = WorkspaceContext.build(str(temp_workspace))
        clean_fp = ws_clean.fingerprint()

        agent_dir = temp_workspace / ".agent" / "runs" / "demo"
        agent_dir.mkdir(parents=True)
        (agent_dir / "trace.jsonl").write_text('{"event":"test"}\n')
        (temp_workspace / "__pycache__").mkdir()
        (temp_workspace / "__pycache__" / "mod.pyc").write_bytes(b"\x00\x01")

        ws_noise = WorkspaceContext.build(str(temp_workspace))
        assert ws_noise.fingerprint() == clean_fp

    def test_fingerprint_detects_real_untracked_file(self, temp_workspace):
        ws_before = WorkspaceContext.build(str(temp_workspace))
        before = ws_before.fingerprint()
        (temp_workspace / "scratch.py").write_text("x = 1\n")
        ws_after = WorkspaceContext.build(str(temp_workspace))
        assert ws_after.fingerprint() != before

    def test_fingerprint_not_tied_to_recent_commits_text(self, temp_workspace):
        import subprocess

        ws_before = WorkspaceContext.build(str(temp_workspace))
        before = ws_before.fingerprint()
        assert ws_before.recent_commits

        (temp_workspace / "note.txt").write_text("note\n")
        subprocess.run(
            ["git", "add", "note.txt"],
            cwd=str(temp_workspace),
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "add note"],
            cwd=str(temp_workspace),
            capture_output=True,
            text=True,
            check=True,
        )

        ws_after = WorkspaceContext.build(str(temp_workspace))
        assert ws_after.recent_commits != ws_before.recent_commits
        assert ws_after.fingerprint() != before

    def test_fingerprint_uses_full_doc_hash_not_preview(self, temp_workspace):
        ws_before = WorkspaceContext.build(str(temp_workspace))
        before = ws_before.fingerprint()

        readme = temp_workspace / "README.md"
        # Change beyond the 200-char preview window used in text()
        readme.write_text(readme.read_text() + ("x" * 250) + "\n")

        ws_after = WorkspaceContext.build(str(temp_workspace))
        assert ws_after.fingerprint() != before

