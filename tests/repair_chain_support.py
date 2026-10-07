"""Assertions for the public CLI, real repair tools, verifier and delivered patch."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from agent_runtime.cancellation import CancellationToken
from agent_runtime.providers.clients import FakeModelClient, FakeNativeToolClient
from src.repair.verification.verify import DockerVerifyStrategy, PytestVerifyStrategy
from src.repair_workspace import RepoSource, clone_repository, git_run
from tests.repair_support import build_repository

ISSUE = "Fix value.py: answer() returns 1 but should return 2. Keep the existing tests unchanged."
NODEID = "test_value.py::test_answer"
REMOTE = "https://github.com/fixloop-fixture/repair-chain.git"


def source_repository(root: Path) -> Path:
    return build_repository(
        root,
        {
            "value.py": "def answer():\n    return 1\n",
            "test_value.py": (
                "from value import answer\n\n"
                "def test_answer():\n    assert answer() == 2\n\n"
                "def test_answer_is_integer():\n    assert isinstance(answer(), int)\n"
            ),
        },
        git=True,
    )


def scripted_client(*, native: bool = False, new_value: int = 2, attempts: int = 1):
    """Only model responses are scripted; file IO and verification stay real."""
    client_type = FakeNativeToolClient if native else FakeModelClient
    return client_type(
        [
            '{"conclusion":"Read value.py, fix answer(), then verify the existing test."}',
            '<tool>{"name":"read_file","args":{"path":"value.py"}}</tool>',
            "<tool>"
            + json.dumps(
                {
                    "name": "patch_file",
                    "args": {
                        "path": "value.py",
                        "old_text": "return 1",
                        "new_text": f"return {new_value}",
                    },
                }
            )
            + "</tool>",
            "<final>Applied the requested fix.</final>",
        ]
        * attempts
    )


def invoke_cli(monkeypatch, repo: str, output: Path, *, tier: str | None = "host", ref=None):
    from src.cli import main

    argv = ["fixloop", "repair", "--repo", repo, "--issue", ISSUE, "--output", str(output)]
    if tier:
        argv += ["--execution-tier", tier]
    if ref:
        argv += ["--ref", ref]
    monkeypatch.setattr(sys, "argv", argv)
    return main()


def verify(repo: Path, tier: str):
    strategy = DockerVerifyStrategy() if tier == "container" else PytestVerifyStrategy()
    return strategy.run(str(repo), test_path=NODEID)


def assert_delivery(output: Path, *, source: str, base: str, tier: str):
    report = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert report["source"] == source
    assert report["base_commit"] == base
    assert report["status"] == report["runtime_status"] == "fixed", report
    assert report["runtime_run_id"]
    assert report["verification_tier"] == tier
    assert report["verification"]["all_passed"]
    assert report["verification"]["total_tests"] > 0
    receipt = report["verification_receipt"]
    assert receipt["completed"] is True
    assert receipt["exit_code"] == 0
    assert receipt["command"]
    if tier == "container":
        assert receipt["receipt_id"].startswith("docker-")
        assert report["verification_details"]["cleanup"] == "confirmed"
    assert report["verification_scope"] == "runtime_selected_tests"
    assert report["patch_available"]
    assert report["changed_files"] == ["value.py"]
    assert [event["phase"] for event in report["events"]] == [
        "preflight",
        "clone",
        "snapshot",
        "repair",
        "delivery",
    ]
    assert (output / "report.md").is_file()
    repo = Path(report["repo_path"])
    assert repo == output / "repo"
    trace = repo / ".agent" / "runs" / report["runtime_run_id"] / "trace.jsonl"
    assert trace.is_file()
    assert "patch_file" in trace.read_text(encoding="utf-8")
    patch = (output / "patch.diff").read_bytes()
    assert patch and b"test_value.py" not in patch
    clean = output.parent / (output.name + "-reapply")
    clone_repository(RepoSource(str(repo), True), clean, ref=base, token=CancellationToken())
    git_run(clean, "apply", "--check", "-", stdin=patch)
    git_run(clean, "apply", "-", stdin=patch)
    assert verify(clean, tier).result.all_passed
    return report


def pytest_process(repo: Path):
    return subprocess.run(
        [sys.executable, "-m", "pytest", NODEID, "-q", "-p", "no:cacheprovider"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
    )
