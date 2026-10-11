"""S0 diagnostics must fail closed and clean only their own Windows fixtures."""

import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import wsl_sandbox_s0_probe as probe


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (r"C:\Users\name\repo 空格", "/mnt/c/Users/name/repo 空格"),
        (r"D:\repo's $literal\file", "/mnt/d/repo's $literal/file"),
    ],
)
def test_drive_mapping_keeps_paths_as_data(source, expected):
    assert probe.wsl_path(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "relative",
        "C:relative",
        r"\\server\repo",
        r"\\?\C:\repo",
        r"C:\repo\..\outside",
        "C:\\repo\x00",
        "/mnt/c/repo",
    ],
)
def test_mapping_rejects_unsupported_or_ambiguous_roots(source):
    with pytest.raises(ValueError):
        probe.wsl_path(source)


@pytest.mark.parametrize(
    "checks", [{}, {"mount": {"passed": False}}, {"mount": {"passed": None}}, {"mount": {}}]
)
def test_missing_or_failed_evidence_is_no_go(checks):
    result = probe.outcome(checks)
    assert result["decision"] == "no_go"
    assert result["probe_passed"] is False


def test_alias_failure_cannot_be_hidden_by_successful_mount():
    report = probe.outcome({"mount": {"passed": True}, "junction": {"passed": False}})
    assert report["blockers"] == ["junction"]
    assert report["probe_passed"] is False


@pytest.mark.parametrize("kind", ["missing", "extra", "not_bool", "not_dict"])
def test_target_protocol_requires_every_check_and_typed_results(kind):
    checks = {name: {"passed": True} for name in probe.TARGET_CHECKS}
    if kind == "missing":
        checks.pop("junction_escape")
    elif kind == "extra":
        checks["unknown"] = {"passed": True}
    elif kind == "not_bool":
        checks["junction_escape"] = {"passed": "true"}
    else:
        checks["junction_escape"] = True
    with pytest.raises(ValueError, match="evidence"):
        probe.target_evidence(json.dumps(checks))


def test_target_security_failure_is_preserved_in_report():
    checks = {name: {"passed": True} for name in probe.TARGET_CHECKS}
    checks["secret_hardlink"] = {"passed": False, "readable": True}
    evidence = probe.target_evidence(json.dumps(checks))
    assert probe.outcome(evidence)["blockers"] == ["secret_hardlink"]


def test_masks_handle_git_directory_and_worktree_git_file(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    (workspace / ".agent").mkdir()
    (workspace / ".env").write_text("fake")
    directory_args = probe.mask_arguments(workspace)
    assert ["--tmpfs", "/workspace/.git", "--remount-ro", "/workspace/.git"] == directory_args[:4]
    (workspace / ".git").rmdir()
    (workspace / ".git").write_text("gitdir: ../shared")
    assert probe.mask_arguments(workspace)[:3] == ["--ro-bind", "/dev/null", "/workspace/.git"]


def test_profile_never_mounts_whole_windows_drive_or_host_usr(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in (".git", ".agent", ".env"):
        (workspace / name).write_text("fake")
    argv = probe.sandbox_arguments(workspace, Path("/trusted/venv"), Path("/usr/bin/python3.14"))
    sources = [argv[i + 1] for i, arg in enumerate(argv) if arg in ("--bind", "--ro-bind")]
    assert "/usr" not in sources
    assert "/mnt/c" not in sources
    assert "/" not in sources
    assert str(workspace) in sources
    assert "--unshare-net" in argv and "--clearenv" in argv


def test_cleanup_rejects_arbitrary_directory(tmp_path):
    user_repo = tmp_path / "user-repo"
    user_repo.mkdir()
    sentinel = user_repo / "user.txt"
    sentinel.write_text("preserve")
    with pytest.raises(RuntimeError, match="unsafe"):
        probe.cleanup_fixture(user_repo, tmp_path)
    assert sentinel.read_text() == "preserve"


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only Git objects")
def test_cleanup_handles_readonly_objects_and_preserves_junction_target(tmp_path):
    root = tmp_path / ".fixloop-s0-cleanup-test"
    root.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    sentinel = target / "user.txt"
    sentinel.write_text("preserve")
    readonly = root / "git-object"
    readonly.write_text("object")
    readonly.chmod(stat.S_IREAD)
    link = root / "junction"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "-"],
        input=f"New-Item -ItemType Junction -Path '{str(link).replace(chr(39), chr(39) * 2)}' "
        f"-Target '{str(target).replace(chr(39), chr(39) * 2)}' | Out-Null\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    probe.cleanup_fixture(root, tmp_path)
    assert not root.exists()
    assert sentinel.read_text() == "preserve"


def test_wsl_inventory_decoding():
    assert probe.decode_wsl("Ubuntu 2\n".encode("utf-16-le")) == "Ubuntu 2\n"
    assert probe.decode_wsl(b"plain error") == "plain error"


@pytest.mark.parametrize(
    ("inventory", "selected", "expected"),
    [
        ("* Ubuntu Running 2\n docker-desktop Stopped 2", "Ubuntu", True),
        ("Ubuntu Running 1", "Ubuntu", False),
        ("Ubuntu Running 2", "Other", False),
        ("FixLoop Sandbox Stopped 2", "FixLoop Sandbox", True),
    ],
)
def test_selected_distribution_must_be_wsl2(inventory, selected, expected):
    assert probe.selected_wsl2(inventory, selected) is expected


def test_transport_failure_preserves_checks_and_cleans_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.platform, "version", lambda: "test-windows")
    monkeypatch.setattr(probe, "wsl_path", lambda path: "/mapped")
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            [], 0, stdout="Ubuntu Running 2".encode("utf-16-le"), stderr=b""
        ),
    )
    monkeypatch.setattr(probe, "create_fixtures", lambda root: {"fixture": {"passed": True}})
    monkeypatch.setattr(
        probe,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 3, stdout="", stderr="controller failed"),
    )
    report = probe.windows_probe(
        SimpleNamespace(workspace_parent=str(tmp_path), distribution="Ubuntu", toolchain="/trusted")
    )
    assert report["decision"] == "no_go"
    assert report["checks"]["fixture"]["passed"] is True
    assert report["checks"]["fixture_cleanup"]["passed"] is True
    assert report["checks"]["linux_probe"]["stderr"] == "controller failed"
    assert not list(tmp_path.iterdir())


def test_timeout_retains_existing_evidence_and_cleans_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.platform, "version", lambda: "test-windows")
    monkeypatch.setattr(probe, "wsl_path", lambda path: "/mapped")
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            [], 0, stdout="Ubuntu Running 2".encode("utf-16-le"), stderr=b""
        ),
    )
    monkeypatch.setattr(probe, "create_fixtures", lambda root: {"fixture": {"passed": True}})

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("wsl", 120)

    monkeypatch.setattr(probe, "run", fail)
    report = probe.windows_probe(
        SimpleNamespace(workspace_parent=str(tmp_path), distribution="Ubuntu", toolchain="/trusted")
    )
    assert report["checks"]["fixture"]["passed"] is True
    assert report["checks"]["probe_completed"]["passed"] is False
    assert report["checks"]["fixture_cleanup"]["passed"] is True
    assert not list(tmp_path.iterdir())


def test_fixture_git_cannot_be_redirected_into_user_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "user/.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "user"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "user-index"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(tmp_path / "user-hooks"))
    captured = {}

    def capture(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(probe, "run", capture)
    probe.checked_git(tmp_path / "fixture", "init", "--template=")
    assert "GIT_DIR" not in captured["env"]
    assert "GIT_WORK_TREE" not in captured["env"]
    assert "GIT_INDEX_FILE" not in captured["env"]
    assert "GIT_CONFIG_COUNT" not in captured["env"]
    assert "GIT_CONFIG_VALUE_0" not in captured["env"]
    assert captured["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
    assert captured["argv"][captured["argv"].index("-C") + 1] == str(tmp_path / "fixture")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction cleanup safety")
def test_cleanup_rejects_junction_as_fixture_root(tmp_path):
    target = tmp_path / ".fixloop-s0-preserved"
    target.mkdir()
    sentinel = target / "user.txt"
    sentinel.write_text("preserve")
    alias = tmp_path / ".fixloop-s0-alias"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "-"],
        input=f"New-Item -ItemType Junction -Path '{str(alias).replace(chr(39), chr(39) * 2)}' "
        f"-Target '{str(target).replace(chr(39), chr(39) * 2)}' | Out-Null\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    try:
        with pytest.raises(RuntimeError, match="unsafe"):
            probe.cleanup_fixture(alias, tmp_path)
        assert sentinel.read_text() == "preserve"
    finally:
        alias.rmdir()  # Remove the junction itself, never its target.
