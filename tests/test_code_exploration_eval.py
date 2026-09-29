import json
from pathlib import Path

import pytest

from agent_runtime.providers.clients import FakeModelClient
from src.eval.code_exploration import baseline_manifest, load_oracles, load_suite, main
from src.eval.code_exploration_p4 import agent_run

SUITE = Path(__file__).parent / "fixtures/code_exploration/tasks.json"


def test_suite_and_oracles_are_separate_and_complete():
    tasks = load_suite(SUITE)
    assert len(tasks) == 8
    oracles = load_oracles(SUITE.with_name("oracles.json"), {task["id"] for task in tasks})
    assert all(oracle["accepted_definitions"] for oracle in oracles.values())
    assert "accepted_definitions" not in tasks[0]
    for task in tasks:
        fixture = SUITE.parent / task["fixture_dir"]
        for definition in oracles[task["id"]]["accepted_definitions"]:
            lines = (fixture / definition["path"]).read_text(encoding="utf-8").splitlines()
            line = lines[definition["range"]["start_line"] - 1]
            assert line.lstrip().startswith(f"def {definition['qualified_name']}(")


def test_deterministic_smoke_uses_fresh_repo_and_records_calls(tmp_path):
    output = tmp_path / "results"
    assert main(["--suite", str(SUITE), "--deterministic", "--output", str(output)]) == 0
    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    result = json.loads((output / "per_task_results.jsonl").read_text(encoding="utf-8"))
    calls = json.loads((output / result["trace_ref"]).read_text(encoding="utf-8"))
    assert manifest["kind"] == "current_text_snapshot"
    assert manifest["source_hashes"]["agent_runtime/tools.py"]
    assert result["predicted_locations"] == [{"path": "normalize.py", "line": 1}]
    assert [call["tool"] for call in calls] == ["list_files", "grep", "read_file"]
    assert result["correctness"] is None


def test_suite_rejects_missing_fixture(tmp_path):
    bad = tmp_path / "tasks.json"
    bad.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "tasks": [
                    {
                        "id": "bad",
                        "category": "x",
                        "fixture_dir": "../outside",
                        "prompt": "x",
                        "entry_paths": ["x.py"],
                        "budget_profile": "default",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Invalid fixture"):
        load_suite(bad)


def test_p4_text_contract_records_evidence_without_agent_claim(tmp_path):
    output = tmp_path / "comparison"
    assert (
        main(
            [
                "--suite",
                str(SUITE),
                "--task",
                "same_name",
                "--mode",
                "text",
                "--deterministic",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    row = json.loads((output / "per_task_results.jsonl").read_text(encoding="utf-8"))
    assert manifest["kind"] == "p4_deterministic_contract"
    assert manifest["tool_schema_hash"]
    assert row["contract_checks"]["definition_location"]
    assert row["correctness"] is None
    assert row["evaluation_kind"] == "deterministic_contract_not_agent_effect"
    assert (output / row["trace_ref"]).is_file()
    assert "pending explicit authorization" in (output / "report.md").read_text(encoding="utf-8")


def test_agent_entry_requires_explicit_provider(tmp_path):
    with pytest.raises(SystemExit, match="2"):
        main(["--mode", "all", "--agent", "--output", str(tmp_path / "out")])


def test_agent_runner_isolates_session_and_records_schema(tmp_path, monkeypatch):
    tasks = load_suite(SUITE)
    task = next(item for item in tasks if item["id"] == "same_name")
    oracle = load_oracles(SUITE.with_name("oracles.json"), {item["id"] for item in tasks})
    project_root = Path(__file__).resolve().parents[1]
    manifest = baseline_manifest(project_root, SUITE, tasks)
    monkeypatch.setattr(
        "agent_runtime.bootstrap.create_model_client",
        lambda **_kwargs: FakeModelClient(["<final>beta.py:1</final>"]),
    )
    row = agent_run(
        task,
        oracle[task["id"]],
        SUITE,
        tmp_path,
        manifest,
        "text",
        1,
        provider="fake",
        model="fake",
    )
    assert row["evaluation_kind"] == "real_agent"
    assert row["tool_schema_hash"]
    assert row["session_id"]
    assert row["correctness"] is None
    assert row["location_mention_proxy"]
    assert (tmp_path / row["trace_ref"]).is_file()
    assert (tmp_path / row["diff_ref"]).read_text(encoding="utf-8") == ""
