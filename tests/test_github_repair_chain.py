"""Public repair CLI integration; live dependencies require explicit opt-in."""

from __future__ import annotations

import json
import os

import pytest

from agent_runtime.bootstrap import load_dotenv
from agent_runtime.cancellation import CancellationToken
from src.harness.sandbox_health import probe_sandbox_health
from src.repair_workspace import clone_repository, git_run, parse_repo_source
from tests.repair_chain_support import (
    REMOTE,
    assert_delivery,
    invoke_cli,
    pytest_process,
    scripted_client,
    source_repository,
    verify,
)


def prepare_environment(tmp_path, monkeypatch, *, offline=True):
    # Runtime policy is explicit; model keys are never inherited by offline tests.
    for name, value in {
        "FIXLOOP_PROFILE": "dev",
        "FIXLOOP_MAX_STEPS": "8",
        "FIXLOOP_BUDGET_MAX_LLM_CALLS": "8",
        "FIXLOOP_DEADLINE_REPAIR_S": "90",
        "FIXLOOP_DEADLINE_TOOL_S": "30",
        "FIXLOOP_PROGRESS_HEARTBEAT": "0",
    }.items():
        monkeypatch.setenv(name, value)
    if offline:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-fixture")
    monkeypatch.chdir(tmp_path)
    source = source_repository(tmp_path / "source")
    base = git_run(source, "rev-parse", "HEAD").stdout.decode().strip()
    assert pytest_process(source).returncode == 1
    # Exercise the real Git process and clone function, with a per-process URL rewrite.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{source.as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", REMOTE)
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "always")
    return source, base


@pytest.fixture
def chain_environment(tmp_path, monkeypatch):
    return prepare_environment(tmp_path, monkeypatch)


def inject_model(monkeypatch, client):
    def create(model_client=None, **kwargs):
        return model_client if model_client is not None else client

    monkeypatch.setattr("src.repair_factory.create_model_client", create)


@pytest.mark.parametrize("native", [False, True], ids=["xml", "native"])
def test_public_cli_repairs_and_delivers_reapplicable_patch(
    chain_environment, tmp_path, monkeypatch, native
):
    source, base = chain_environment
    original_test = git_run(source, "show", f"{base}:test_value.py").stdout
    client = scripted_client(native=native)
    inject_model(monkeypatch, client)
    output = tmp_path / "result"
    assert invoke_cli(monkeypatch, REMOTE, output, ref=base) == 0
    assert_delivery(output, source=REMOTE, base=base, tier="host")
    assert (output / "repo/test_value.py").read_bytes() == original_test
    assert (source / "value.py").read_text() == "def answer():\n    return 1\n"
    assert client.session_usage["calls"] > 0


@pytest.fixture
def live_docker():
    if os.environ.get("FIXLOOP_CHAIN_DOCKER") != "1":
        pytest.skip("set FIXLOOP_CHAIN_DOCKER=1 to require the real Docker verifier")
    report = probe_sandbox_health()
    assert report.ready, report.to_dict()


def test_public_cli_default_docker_verifies_real_patch(
    chain_environment, live_docker, tmp_path, monkeypatch
):
    source, base = chain_environment
    before = verify(source, "container")
    assert before.internal["actual_tier"] == "container"
    assert not before.result.all_passed and before.result.failed > 0
    inject_model(monkeypatch, scripted_client())
    output = tmp_path / "docker-result"
    assert invoke_cli(monkeypatch, REMOTE, output, tier=None, ref=base) == 0
    report = assert_delivery(output, source=REMOTE, base=base, tier="container")
    assert report["verification_details"]["actual_tier"] == "container"


def test_public_cli_bad_patch_is_not_reported_fixed(chain_environment, tmp_path, monkeypatch):
    _, base = chain_environment
    inject_model(monkeypatch, scripted_client(new_value=3, attempts=4))
    output = tmp_path / "failed-result"
    assert invoke_cli(monkeypatch, REMOTE, output, ref=base) != 0
    report = json.loads((output / "result.json").read_text())
    assert report["status"] not in {"fixed", "pending_verify"}
    assert report["verification"] and not report["verification"]["all_passed"]
    assert report["verification"]["failed"] > 0


def test_live_github_clone_pins_requested_commit(tmp_path):
    if os.environ.get("FIXLOOP_CHAIN_GITHUB") != "1":
        pytest.skip("set FIXLOOP_CHAIN_GITHUB=1 for actual GitHub clone")
    repo = os.environ.get("FIXLOOP_CHAIN_GITHUB_REPO", "https://github.com/pypa/sampleproject")
    ref = os.environ.get("FIXLOOP_CHAIN_GITHUB_SHA", "")
    assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), (
        "set a pinned FIXLOOP_CHAIN_GITHUB_SHA"
    )
    destination = tmp_path / "github-checkout"
    source = parse_repo_source(repo)
    assert source.remote
    assert clone_repository(source, destination, ref=ref, token=CancellationToken()) == ref
    assert (
        git_run(destination, "remote", "get-url", "origin").stdout.decode().strip()
        == source.location
    )
    assert git_run(destination, "status", "--porcelain").stdout == b""
    assert git_run(destination, "symbolic-ref", "HEAD", check=False).returncode != 0


def test_live_model_cli(tmp_path, monkeypatch, live_docker):
    if os.environ.get("FIXLOOP_CHAIN_MODEL") != "1":
        pytest.skip("set FIXLOOP_CHAIN_MODEL=1 for paid model acceptance")
    load_dotenv()
    assert os.environ.get("DEEPSEEK_API_KEY"), "configure the model key before opting in"
    # Preserve the real provider, factory and runtime; only use a controlled Git source.
    source, base = prepare_environment(tmp_path, monkeypatch, offline=False)
    original_test = git_run(source, "show", f"{base}:test_value.py").stdout
    assert not verify(source, "container").result.all_passed
    output = tmp_path / "result"
    assert invoke_cli(monkeypatch, REMOTE, output, tier=None, ref=base) == 0
    assert_delivery(output, source=REMOTE, base=base, tier="container")
    assert (output / "repo/test_value.py").read_bytes() == original_test
    assert (source / "value.py").read_text() == "def answer():\n    return 1\n"
