"""Real local Git fixtures for checkout and independently applicable exports."""

import subprocess
from pathlib import Path

import pytest

from agent_runtime.cancellation import CancellationToken, CancelledError
from src.repair_workspace import (
    GitSnapshot,
    RepairInputError,
    RepoSource,
    clone_repository,
    git_run,
    parse_repo_source,
)


@pytest.mark.parametrize(
    "value, location",
    [
        ("owner/project", "https://github.com/owner/project.git"),
        ("https://github.com/owner/project/", "https://github.com/owner/project.git"),
        ("https://github.com/owner/project.git", "https://github.com/owner/project.git"),
        ("git@github.com:owner/project.git", "git@github.com:owner/project.git"),
    ],
)
def test_github_sources(value, location):
    assert parse_repo_source(value) == RepoSource(location, True)


@pytest.mark.parametrize(
    "value",
    [
        "https://token@github.com/owner/project",
        "http://github.com/owner/project",
        "https://example.com/owner/project",
        "https://github.com/owner/project/issues/1",
        "https://github.com/owner/project?token=secret",
        "git@evil:owner/project",
        "https://github.com/owner/..",
        "--upload-pack=bad",
        "../missing",
        "missing",
        "",
    ],
)
def test_invalid_sources(value):
    with pytest.raises(RepairInputError):
        parse_repo_source(value)


def test_local_directory_has_priority(tmp_path, monkeypatch):
    local = tmp_path / "owner" / "project"
    local.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    assert parse_repo_source("owner/project") == RepoSource(str(local), False)
    file = tmp_path / "file"
    file.touch()
    with pytest.raises(RepairInputError):
        parse_repo_source(str(file))


def test_clone_default_branch_and_pin_sha(temp_workspace, tmp_path):
    expected = git_run(temp_workspace, "rev-parse", "HEAD").stdout.decode().strip()
    source = RepoSource(str(temp_workspace), True)
    destination = tmp_path / "checkout"
    sha = clone_repository(source, destination, ref=None, token=CancellationToken())
    assert sha == expected
    assert (destination / "README.md").exists()
    assert git_run(destination, "symbolic-ref", "HEAD", check=False).returncode != 0
    assert git_run(destination, "status", "--porcelain").stdout == b""
    with pytest.raises(RepairInputError, match="已存在"):
        clone_repository(source, destination, ref=None, token=CancellationToken())


@pytest.mark.parametrize("ref_kind", ["branch", "tag", "sha"])
def test_clone_requested_ref(temp_workspace, tmp_path, ref_kind):
    base = git_run(temp_workspace, "rev-parse", "HEAD").stdout.decode().strip()
    git_run(temp_workspace, "branch", "feature/fix", base)
    git_run(temp_workspace, "tag", "v1", base)
    (temp_workspace / "later.py").write_text("later = True\n")
    git_run(temp_workspace, "add", "later.py")
    git_run(temp_workspace, "commit", "-m", "later")
    ref = {"branch": "feature/fix", "tag": "v1", "sha": base}[ref_kind]
    dest = tmp_path / "checkout"
    assert (
        clone_repository(
            RepoSource(str(temp_workspace), True),
            dest,
            ref=ref,
            token=CancellationToken(),
        )
        == base
    )
    assert not (dest / "later.py").exists()


def test_missing_ref_and_invalid_ref(temp_workspace, tmp_path):
    with pytest.raises(RepairInputError) as error:
        clone_repository(
            RepoSource(str(temp_workspace), True),
            tmp_path / "bad-ref",
            ref="does-not-exist",
            token=CancellationToken(),
        )
    assert error.value.category == "reference_not_found"
    with pytest.raises(RepairInputError):
        clone_repository(
            RepoSource(str(temp_workspace), True),
            tmp_path / "injection",
            ref="--upload-pack=evil",
            token=CancellationToken(),
        )
    assert not (tmp_path / "injection").exists()


def test_cancelled_clone_never_launches(temp_workspace, tmp_path):
    token = CancellationToken()
    token.cancel("user")
    with pytest.raises(CancelledError):
        clone_repository(
            RepoSource(str(temp_workspace), True),
            tmp_path / "cancelled",
            ref=None,
            token=token,
        )
    assert not (tmp_path / "cancelled").exists()


def test_export_preserves_real_index_and_applies_to_baseline(temp_workspace, tmp_path):
    repo = temp_workspace
    (repo / "delete.py").write_text("delete = True\n")
    (repo / "binary.bin").write_bytes(b"\0old")
    git_run(repo, "add", "delete.py", "binary.bin")
    git_run(repo, "commit", "-m", "files")
    baseline = git_run(repo, "rev-parse", "HEAD").stdout.decode().strip()
    index_file = Path(git_run(repo, "rev-parse", "--git-path", "index").stdout.decode().strip())
    if not index_file.is_absolute():
        index_file = repo / index_file
    original_index = index_file.read_bytes()
    output = tmp_path / "result"
    output.mkdir()
    snapshot = GitSnapshot(repo, output)
    (repo / "README.md").write_text("# changed\n")
    (repo / "delete.py").unlink()
    (repo / "new test.py").write_text("def test_new():\n    assert True\n")
    (repo / "binary.bin").write_bytes(b"\0new")
    (repo / ".agent").mkdir()
    (repo / ".agent" / "trace.jsonl").write_text("log")
    (repo / ".env").write_text("KEY=secret")
    patch, changed = snapshot.export()
    snapshot.close()
    assert set(changed) == {"README.md", "delete.py", "new test.py", "binary.bin"}
    assert index_file.read_bytes() == original_index
    assert not snapshot.index.exists()
    clean = tmp_path / "clean"
    clone_repository(RepoSource(str(repo), True), clean, ref=baseline, token=CancellationToken())
    git_run(clean, "apply", "--check", "-", stdin=patch)
    git_run(clean, "apply", "-", stdin=patch)
    assert (clean / "README.md").read_text() == (repo / "README.md").read_text()
    assert (clean / "binary.bin").read_bytes() == b"\0new"
    assert (clean / "new test.py").exists()
    assert not (clean / "delete.py").exists()


def test_dirty_local_baseline_only_exports_new_changes(temp_workspace, tmp_path):
    repo = temp_workspace
    (repo / "README.md").write_text("user change\n")
    git_run(repo, "add", "README.md")
    (repo / "notes.txt").write_text("user untracked\n")
    output = tmp_path / "result"
    output.mkdir()
    snapshot = GitSnapshot(repo, output)
    (repo / "fix.py").write_text("fixed = True\n")
    patch, changed = snapshot.export()
    snapshot.close()
    assert changed == ["fix.py"]
    assert b"user change" not in patch and b"notes.txt" not in patch
    assert git_run(repo, "diff", "--cached", "--name-only").stdout.strip() == b"README.md"


def test_staged_deletion_recreated_by_repair_is_an_addition(temp_workspace, tmp_path):
    repo = temp_workspace
    git_run(repo, "rm", "README.md")
    output = tmp_path / "result"
    output.mkdir()
    snapshot = GitSnapshot(repo, output)
    (repo / "README.md").write_text("recreated\n")
    patch, changed = snapshot.export()
    snapshot.close()
    assert changed == ["README.md"]
    assert b"new file mode" in patch
    assert b"/dev/null" in patch


def test_preexisting_untracked_file_staged_during_repair_is_excluded(temp_workspace, tmp_path):
    repo = temp_workspace
    (repo / "notes.txt").write_text("user notes\n")
    output = tmp_path / "result"
    output.mkdir()
    snapshot = GitSnapshot(repo, output)
    git_run(repo, "add", "notes.txt")
    patch, changed = snapshot.export()
    snapshot.close()
    assert patch == b"" and changed == []


def test_snapshot_respects_existing_crlf_configuration(temp_workspace, tmp_path):
    repo = temp_workspace
    git_run(repo, "config", "core.autocrlf", "true")
    (repo / "README.md").write_bytes(b"# Test Project\r\n\r\nThis is a test repo.\r\n")
    output = tmp_path / "result"
    output.mkdir()
    snapshot = GitSnapshot(repo, output)
    patch, changed = snapshot.export()
    snapshot.close()
    assert patch == b"" and changed == []


def test_inherited_git_index_cannot_redirect_snapshot(temp_workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "wrong-index"))
    assert git_run(temp_workspace, "ls-files").stdout.strip()
    assert not (tmp_path / "wrong-index").exists()


def test_snapshot_cancel_keeps_real_index_untouched(temp_workspace, tmp_path, monkeypatch):
    repo = temp_workspace
    output = tmp_path / "result"
    output.mkdir()
    token = CancellationToken()
    original = git_run

    def cancel_before_staging(repo, *args, **kwargs):
        if args[0] == "add":
            token.cancel("user")
        return original(repo, *args, **kwargs)

    monkeypatch.setattr("src.repair_workspace.git_run", cancel_before_staging)
    with pytest.raises(CancelledError):
        GitSnapshot(repo, output, token=token)
    assert not (output / "snapshot.index").exists()
    assert original(repo, "diff", "--cached").stdout == b""


def test_clone_failure_is_categorized(tmp_path):
    with pytest.raises(RepairInputError) as error:
        clone_repository(
            RepoSource(str(tmp_path / "missing-source"), True),
            tmp_path / "dest",
            ref=None,
            token=CancellationToken(),
        )
    assert error.value.category == "clone_failed"


def test_git_timeout_kills_and_drains_process(monkeypatch):
    class HungProcess:
        returncode = None
        killed = False

        def communicate(self, **kwargs):
            if self.killed:
                return b"", b""
            raise subprocess.TimeoutExpired("git", 0.1)

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -1

    process = HungProcess()
    monkeypatch.setattr("src.repair_workspace.subprocess.Popen", lambda *a, **kw: process)
    monkeypatch.setattr("src.repair_workspace._kill_git_process", lambda proc: proc.kill())
    clock = iter([0, 1])
    monkeypatch.setattr("src.repair_workspace.time.monotonic", lambda: next(clock))
    with pytest.raises(RepairInputError) as error:
        git_run(None, "clone", "unused", timeout=0.5)
    assert error.value.category == "timeout"
    assert process.killed
