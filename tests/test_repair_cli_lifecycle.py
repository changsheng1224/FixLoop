"""CLI lifecycle with real local clones and an injected repair runtime."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_runtime.cancellation import CancellationToken
from src.repair_cli import TextSnapshot, run_repair_cli
from src.repair_report import RepairReport
from src.repair_workspace import RepoSource, clone_repository, git_run
from src.state import CandidatePatch, RepairState, VerificationResult


def arguments(repo, output, **kwargs):
    values = dict(
        repo=str(repo),
        output=str(output),
        issue="结果不正确，请修复",
        issue_file=None,
        ref=None,
        resume_repair=None,
        skip_verify=False,
        execution_tier="auto",
        require_sandbox=False,
    )
    return SimpleNamespace(**(values | kwargs))


def result(output):
    return json.loads((output / "result.json").read_text(encoding="utf-8"))


def patch_state(issue, **kwargs):
    return RepairState(
        issue_input=issue,
        status="fixed",
        candidate_patches=[CandidatePatch(file_path="README.md")],
        **kwargs,
    )


def redirect_clone(monkeypatch, source_repo):
    (source_repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    git_run(source_repo, "add", "app.py")
    git_run(source_repo, "commit", "-m", "Python fixture")

    def local_clone(source, destination, **kwargs):
        return clone_repository(RepoSource(str(source_repo), True), destination, **kwargs)

    monkeypatch.setattr("src.repair_cli.clone_repository", local_clone)


def test_github_default_container_end_to_end(temp_workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    redirect_clone(monkeypatch, temp_workspace)
    output = tmp_path / "result"
    args = arguments("owner/project", output)

    def execute(repo, token):
        assert args.execution_tier == "container" and args.require_sandbox
        assert not token.is_cancelled
        (Path(repo) / "README.md").write_text("# repaired\n")
        (Path(repo) / "new.py").write_text("value = 2\n")
        return patch_state(
            args.issue,
            verification_result=VerificationResult(all_passed=True, total_tests=2, passed=2),
        )

    assert run_repair_cli(args, execute) == 0
    data = result(output)
    assert data["status"] == "fixed"
    assert data["verification_tier"] == "container"
    assert set(data["changed_files"]) == {"README.md", "new.py"}
    assert len(data["base_commit"]) == 40
    assert [event["phase"] for event in data["events"]] == [
        "preflight",
        "clone",
        "snapshot",
        "repair",
        "delivery",
    ]
    assert (output / "report.md").exists()
    clean = tmp_path / "clean"
    clone_repository(
        RepoSource(str(temp_workspace), True),
        clean,
        ref=None,
        token=CancellationToken(),
    )
    git_run(clean, "apply", "--check", str(output / "patch.diff"))


def test_issue_file_and_existing_local_contract(temp_workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    issue_file = tmp_path / "issue.md"
    issue_file.write_text("\ufeff复现步骤：调用 add()\n预期：返回 2", encoding="utf-8")
    output = tmp_path / "result"
    args = arguments(
        temp_workspace, output, issue=None, issue_file=str(issue_file), skip_verify=True
    )

    def execute(repo, token):
        assert Path(repo) == temp_workspace
        assert args.issue.startswith("复现步骤")
        (Path(repo) / "README.md").write_text("fixed\n")
        return patch_state(args.issue)

    assert run_repair_cli(args, execute) == 0
    assert result(output)["status"] == "pending_verify"
    assert result(output)["verification_scope"] == "unverified"


def test_initialization_environment_failure_preserves_report(temp_workspace, tmp_path, monkeypatch):
    from src.repair_factory import RequiredVerifierError

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    redirect_clone(monkeypatch, temp_workspace)
    output = tmp_path / "result"

    def execute(repo, token):
        raise RequiredVerifierError("Docker 镜像缺失")

    assert run_repair_cli(arguments("owner/project", output), execute) == 2
    data = result(output)
    assert data["category"] == "verification_environment_failed"
    assert (output / "repo" / "README.md").exists()
    assert (output / "patch.diff").read_bytes() == b""


def test_missing_api_key_does_not_clone(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    output = tmp_path / "result"
    assert (
        run_repair_cli(arguments("owner/project", output), lambda *a: pytest.fail("executed")) == 2
    )
    assert result(output)["category"] == "configuration_failed"
    assert not (output / "repo").exists()


def test_cancel_after_edit_exports_partial_patch(temp_workspace, tmp_path, monkeypatch):
    from agent_runtime.cancellation import CancelledError

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    output = tmp_path / "result"

    def execute(repo, token):
        (Path(repo) / "README.md").write_text("partial\n")
        token.cancel("user")
        raise CancelledError("user")

    assert run_repair_cli(arguments(temp_workspace, output), execute) == 130
    assert result(output)["status"] == "user_cancel"
    assert b"partial" in (output / "patch.diff").read_bytes()


def test_runtime_failure_keeps_patch(temp_workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    output = tmp_path / "result"

    def execute(repo, token):
        (Path(repo) / "README.md").write_text("partial\n")
        raise RuntimeError("provider failed")

    assert run_repair_cli(arguments(temp_workspace, output), execute) == 1
    assert result(output)["category"] == "runtime_failed"
    assert result(output)["patch_available"]


@pytest.mark.parametrize(
    "verification",
    [
        None,
        VerificationResult(all_passed=True, total_tests=0),
    ],
)
def test_unverified_never_reported_fixed(tmp_path, verification):
    output = tmp_path / "report"
    output.mkdir()
    report = RepairReport(output, "test")
    report.record_state(patch_state("fix", verification_result=verification), "static")
    assert result(output)["status"] == "pending_verify"


def test_dependency_failure_report(tmp_path):
    output = tmp_path / "report"
    output.mkdir()
    report = RepairReport(output, "test")
    state = RepairState(
        issue_input="fix",
        status="failed",
        verification_result=VerificationResult(
            all_passed=False,
            failure_logs=["ModuleNotFoundError: dependency"],
        ),
    )
    report.record_state(state, "container")
    assert result(output)["category"] == "verification_environment_failed"
    assert "ModuleNotFoundError" in (output / "report.md").read_text(encoding="utf-8")


def test_actual_verification_backend_is_reported(tmp_path):
    output = tmp_path / "report"
    output.mkdir()
    report = RepairReport(output, "test")
    state = patch_state(
        "fix",
        verification_result=VerificationResult(all_passed=True, total_tests=2, passed=2),
    )
    state.node_timings["phases_internal"] = {"verify": {"actual_tier": "host"}}
    report.record_state(state, "auto")
    assert result(output)["verification_tier"] == "host"
    assert result(output)["verification_requested_tier"] == "auto"


def test_runtime_claim_without_disk_change_is_not_success(temp_workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    output = tmp_path / "result"
    assert run_repair_cli(arguments(temp_workspace, output), lambda *a: patch_state("fix")) == 1
    assert result(output)["category"] == "no_changes"


def test_dry_run_does_not_claim_verified_repair(temp_workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    output = tmp_path / "result"
    args = arguments(temp_workspace, output, dry_run=True)
    state = patch_state(
        "fix",
        verification_result=VerificationResult(all_passed=True, total_tests=2, passed=2),
    )
    assert run_repair_cli(args, lambda *a: state) == 0
    assert result(output)["status"] == "pending_verify"
    assert not result(output)["patch_available"]


def test_unsupported_remote_project_stops_before_runtime(temp_workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    redirect_clone(monkeypatch, temp_workspace)
    output = tmp_path / "result"
    args = arguments("owner/project", output, issue="[lang:javascript] fix app.js")
    assert run_repair_cli(args, lambda *a: pytest.fail("executed")) == 2
    assert result(output)["category"] == "unsupported_project"


def test_empty_issue_and_missing_issue_file_are_rejected(temp_workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    for issue, issue_file in [("   ", None), (None, str(tmp_path / "missing.md"))]:
        output = tmp_path / "result"
        assert (
            run_repair_cli(
                arguments(temp_workspace, output, issue=issue, issue_file=issue_file),
                lambda *a: pytest.fail("executed"),
            )
            == 2
        )
        assert not output.exists()


def test_gated_backend_does_not_clone_or_load_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr("src.repair_cli.load_dotenv", lambda: pytest.fail("dotenv reached"))
    output = tmp_path / "result"
    args = arguments(
        "owner/project",
        output,
        execution_backend="wsl_bwrap",
        code_exploration_mode="text",
        pylsp_path=None,
    )
    assert run_repair_cli(args, lambda *a: pytest.fail("executed")) == 2
    assert not output.exists()


def test_existing_output_is_not_overwritten(temp_workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    output = tmp_path / "report"
    output.mkdir()
    (output / "result.json").write_text("keep")
    assert (
        run_repair_cli(arguments(temp_workspace, output), lambda *a: pytest.fail("executed")) == 2
    )
    assert (output / "result.json").read_text() == "keep"


def test_text_directory_export_applies(tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1", encoding="utf-8")
    snapshot = TextSnapshot(repo)
    (repo / "a.py").write_text("x = 2", encoding="utf-8")
    (repo / "new.py").write_text("new = True\n", encoding="utf-8")
    patch, changed = snapshot.export()
    assert changed == ["a.py", "new.py"]
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "a.py").write_text("x = 1", encoding="utf-8")
    git_run(clean, "init", token=CancellationToken())
    git_run(clean, "apply", "--check", "-", stdin=patch)
    git_run(clean, "apply", "-", stdin=patch)
    assert (clean / "a.py").read_text() == "x = 2"


def test_cli_rejects_conflicting_issue_inputs(monkeypatch):
    from src.cli import main

    monkeypatch.setattr(
        sys, "argv", ["fixloop", "repair", "--issue", "fix", "--issue-file", "x.md"]
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_real_cli_dispatch_github(temp_workspace, tmp_path, monkeypatch, capsys):
    from src.cli import main

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    redirect_clone(monkeypatch, temp_workspace)
    captured = {}
    output = tmp_path / "result"

    class FakeOrchestrator:
        verifier = object()

        def repair(self, issue, **kwargs):
            (captured["repo"] / "README.md").write_text("fixed\n")
            return patch_state(issue)

    def factory(**kwargs):
        captured.update(kwargs)

        def create(repo):
            captured["repo"] = Path(repo)
            return FakeOrchestrator()

        return create

    monkeypatch.setattr("src.cli.make_orchestrator_factory", factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fixloop",
            "repair",
            "--repo",
            "owner/project",
            "--issue",
            "fix README",
            "--output",
            str(output),
        ],
    )
    assert main() == 0
    assert captured["execution_tier"] == "container"
    assert captured["require_sandbox"] is True
    out = capsys.readouterr().out
    assert "补丁已生成，等待验证" in out
    assert "修复完成" not in out
    assert result(output)["patch_available"]
