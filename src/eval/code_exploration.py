"""P0 code exploration fixtures and deterministic baseline recorder.

The deterministic probe is an infrastructure smoke test, not an Agent result.
Oracles are deliberately loaded only by scoring/validation callers.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import tool_grep, tool_list_files, tool_read_file

DEFAULT_SUITE = Path("tests/fixtures/code_exploration/tasks.json")
BASELINE_SOURCES = (
    "agent_runtime/tools.py",
    "agent_runtime/file_listing.py",
    "agent_runtime/io_limits.py",
    "agent_runtime/tool_context.py",
    "agent_runtime/tool_result.py",
    "src/eval/runner.py",
)
P4_SOURCES = (
    *BASELINE_SOURCES,
    "agent_runtime/code_exploration/io.py",
    "agent_runtime/code_exploration/lsp.py",
    "agent_runtime/code_exploration/service.py",
    "agent_runtime/code_exploration/relations.py",
    "agent_runtime/code_exploration/context.py",
    "agent_runtime/context_manager.py",
    "agent_runtime/agent_loop.py",
    "src/eval/code_exploration.py",
    "src/eval/code_exploration_p4.py",
)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def _command_version(argv: list[str]) -> dict:
    executable = shutil.which(argv[0])
    if executable is None:
        return {"path": None, "version": None}
    try:
        result = subprocess.run(
            [executable, *argv[1:]], capture_output=True, text=True, timeout=5, check=False
        )
        version = (result.stdout or result.stderr).strip().splitlines()[:1]
    except (OSError, subprocess.TimeoutExpired):
        version = []
    return {"path": executable, "version": version[0] if version else None}


def _pylsp_environment() -> dict:
    server = _command_version(["pylsp", "--version"])
    if not server["path"]:
        return {**server, "python": None, "python_lsp_server": None, "jedi": None}
    interpreter = Path(server["path"]).parent.parent / "python.exe"
    if not interpreter.is_file():
        return {**server, "python": None, "python_lsp_server": None, "jedi": None}
    probe = (
        "import json,sys,importlib.metadata as m; "
        "print(json.dumps({'python':sys.version.split()[0],"
        "'python_lsp_server':m.version('python-lsp-server'),'jedi':m.version('jedi')}))"
    )
    try:
        result = subprocess.run(
            [str(interpreter), "-c", probe],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        details = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        details = {"python": None, "python_lsp_server": None, "jedi": None}
    return {**server, "interpreter": str(interpreter), **details}


def load_suite(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != "1" or not isinstance(data.get("tasks"), list):
        raise ValueError("Unsupported task suite schema")
    tasks = data["tasks"]
    required = {"id", "category", "fixture_dir", "prompt", "entry_paths", "budget_profile"}
    ids = [task.get("id") for task in tasks]
    if len(ids) != len(set(ids)) or not all(isinstance(item, str) for item in ids):
        raise ValueError("Task IDs must be unique strings")
    base = path.parent.resolve()
    for task in tasks:
        if not required <= task.keys():
            raise ValueError(f"Incomplete task: {task.get('id')}")
        fixture = (base / task["fixture_dir"]).resolve()
        if not fixture.is_relative_to(base) or not fixture.is_dir():
            raise ValueError(f"Invalid fixture directory: {task['fixture_dir']}")
        if any(item.is_symlink() for item in fixture.rglob("*")):
            raise ValueError(f"Symlink in fixture: {task['id']}")
        for entry in task["entry_paths"]:
            entry_path = (fixture / entry).resolve()
            if not entry_path.is_relative_to(fixture) or not entry_path.is_file():
                raise ValueError(f"Invalid entry path: {entry}")
    return tasks


def load_oracles(path: Path, task_ids: set[str]) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != "1" or set(data.get("tasks", {})) != task_ids:
        raise ValueError("Oracle IDs do not match suite")
    required = {
        "accepted_definitions",
        "relevant_files",
        "required_relations",
        "forbidden_claims",
        "success_predicate",
    }
    for task_id, oracle in data["tasks"].items():
        if not required <= oracle.keys():
            raise ValueError(f"Incomplete oracle: {task_id}")
    return data["tasks"]


def baseline_manifest(project_root: Path, suite: Path, tasks: list[dict]) -> dict:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project_root, capture_output=True, text=True, check=True
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return {
        "schema_version": "1",
        "kind": "current_text_snapshot",
        "head": head,
        "dirty": bool(status),
        "tracked_dirty_count": sum(not line.startswith("??") for line in status),
        "untracked_entry_count": sum(line.startswith("??") for line in status),
        "source_hashes": {name: _hash_file(project_root / name) for name in BASELINE_SOURCES},
        "suite_hash": _hash_file(suite),
        "fixture_hashes": {
            task["id"]: _hash_fixture(suite.parent / task["fixture_dir"]) for task in tasks
        },
        "runtime_versions": {
            "python": platform.python_version(),
            "pytest": _version("pytest"),
            "ruff": _command_version(["ruff", "--version"]),
            "pylsp": _pylsp_environment(),
        },
        "budget_defaults": {
            "range_scan_bytes": 262144,
            "range_return_lines": 200,
            "range_return_bytes": 32768,
            "search_files": 300,
            "search_read_bytes": 2097152,
            "file_ast_lsp_bytes": 262144,
            "retrieval_hits": 50,
            "tool_visible_bytes": 65536,
            "lsp_initialize_seconds": 5,
            "lsp_request_seconds": 3,
            "lsp_message_bytes": 1048576,
            "relation_files": 8,
            "relation_nodes": 64,
            "relation_edges": 128,
            "context_top_k": 6,
            "context_tokens": 1500,
        },
        "comparison_design": {
            "text": "bounded_text_after_P1",
            "lsp": "bounded_text_plus_python_lsp",
            "relations": "bounded_text_plus_lsp_plus_task_view",
            "agent_repetitions_per_task": 3,
            "model": "pending_authorization",
            "sampling": "pending_authorization",
        },
        "agent_experiment": "pending_authorization",
    }


def _hash_fixture(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(_hash_file(path).encode("ascii"))
    return digest.hexdigest()


def run_deterministic(task: dict, suite: Path, output: Path, manifest: dict) -> dict:
    fixture = suite.parent / task["fixture_dir"]
    probe = task.get("deterministic_probe")
    if not isinstance(probe, dict):
        raise ValueError(f"No deterministic probe for {task['id']}")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="fixloop-exploration-") as directory:
        workspace = Path(directory) / "repo"
        shutil.copytree(fixture, workspace)
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
        subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=FixLoop Eval",
                "-c",
                "user.email=eval@localhost",
                "commit",
                "-q",
                "-m",
                "fixture baseline",
            ],
            cwd=workspace,
            check=True,
        )
        context = ToolContext(root=str(workspace))
        calls = []
        for name, args, function in (
            ("list_files", {"path": ".", "depth": 0, "glob": "*.py"}, tool_list_files),
            ("grep", {"pattern": probe["pattern"], "path": probe["path"]}, tool_grep),
        ):
            result = function(context, args)
            calls.append({"tool": name, "args": args, "result": result})
        # rg omits the filename when its target is a single file.
        match = re.search(r"(?:^|:)(\d+):", calls[-1]["result"], re.MULTILINE)
        predicted = []
        if match:
            line = int(match.group(1))
            args = {"path": probe["path"], "start": line, "end": line + 3}
            result = tool_read_file(context, args)
            calls.append({"tool": "read_file", "args": args, "result": result})
            predicted = [{"path": probe["path"], "line": line}]
        trace_path = output / "traces" / f"{task['id']}.json"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text(json.dumps(calls, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "task_id": task["id"],
        "mode": "text_deterministic",
        "repetition": 1,
        "runtime_versions": manifest["runtime_versions"],
        "fixture_hash": manifest["fixture_hashes"][task["id"]],
        "config_hash": hashlib.sha256(
            json.dumps({"mode": "text_deterministic", "probe": probe}, sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest(),
        "execution_status": "ok" if predicted else "unscorable",
        "predicted_locations": predicted,
        "selected_files": [probe["path"]] if predicted else [],
        "relation_claims": [],
        "correctness": None,
        "metrics": {
            "tool_calls": len(calls),
            "duration_ms": round((time.monotonic() - started) * 1000),
        },
        "trace_ref": trace_path.relative_to(output).as_posix(),
        "evidence_refs": [],
        "verification_result": None,
        "evaluation_kind": "deterministic_smoke_not_agent_effect",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--task", default=None)
    parser.add_argument("--mode", choices=["text", "lsp", "relations", "all"])
    execution = parser.add_mutually_exclusive_group(required=True)
    execution.add_argument("--deterministic", action="store_true")
    execution.add_argument("--agent", action="store_true")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--provider", choices=["anthropic_compat", "openai", "ollama"])
    parser.add_argument("--model")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    suite = args.suite.resolve()
    tasks = load_suite(suite)
    load_oracles(suite.with_name("oracles.json"), {task["id"] for task in tasks})
    selected = [task for task in tasks if args.task is None or task["id"] == args.task]
    if not selected:
        parser.error(f"Unknown task: {args.task}")
    if args.agent and not args.mode:
        parser.error("--agent requires --mode")
    if args.agent and not args.provider:
        parser.error("--agent requires --provider")
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    project_root = Path(__file__).resolve().parents[2]
    manifest = baseline_manifest(project_root, suite, tasks)
    if args.mode:
        from agent_runtime.tools import build_tool_registry

        modes = ["text", "lsp", "relations"] if args.mode == "all" else [args.mode]
        schema = build_tool_registry(ToolContext(root=str(project_root)))
        schema_data = {
            name: {key: value for key, value in spec.items() if not callable(value)}
            for name, spec in schema.items()
        }
        manifest.update(
            {
                "kind": "p4_agent_comparison" if args.agent else "p4_deterministic_contract",
                "source_hashes": {name: _hash_file(project_root / name) for name in P4_SOURCES},
                "modes": modes,
                "tool_schema_hash": hashlib.sha256(
                    json.dumps(schema_data, sort_keys=True, default=str).encode()
                ).hexdigest(),
                "tool_schema": schema_data,
                "model": args.model if args.agent else None,
                "provider": args.provider if args.agent else None,
                "repetitions_per_task": args.repetitions if args.agent else 1,
            }
        )
        manifest["comparison_design"]["model"] = args.model if args.agent else None
        manifest["comparison_design"]["sampling"] = (
            {"temperature": 0, "repetitions": args.repetitions} if args.agent else None
        )
        manifest["agent_experiment"] = "executed" if args.agent else "not_run"
    (output / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if args.mode:
        from src.eval.code_exploration_p4 import agent_run, deterministic_run, write_report

        oracles = load_oracles(suite.with_name("oracles.json"), {task["id"] for task in tasks})
        rows = []
        result_path = output / "per_task_results.jsonl"
        with result_path.open("w", encoding="utf-8") as stream:
            for mode in modes:
                for task in selected:
                    repetitions = args.repetitions if args.agent else 1
                    for repetition in range(1, repetitions + 1):
                        try:
                            if args.agent:
                                row = agent_run(
                                    task,
                                    oracles[task["id"]],
                                    suite,
                                    output,
                                    manifest,
                                    mode,
                                    repetition,
                                    provider=args.provider,
                                    model=args.model,
                                )
                            else:
                                row = deterministic_run(
                                    task, oracles[task["id"]], suite, output, manifest, mode
                                )
                        except Exception as exc:
                            row = {
                                "task_id": task["id"],
                                "mode": mode,
                                "repetition": repetition,
                                "evaluation_kind": "real_agent"
                                if args.agent
                                else "deterministic_contract_not_agent_effect",
                                "execution_status": "error",
                                "correctness": None,
                                "error": f"{type(exc).__name__}: {exc}",
                                "metrics": {},
                            }
                        rows.append(row)
                        stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                        stream.flush()
                        print(
                            json.dumps(
                                {
                                    "mode": mode,
                                    "task_id": task["id"],
                                    "repetition": repetition,
                                    "execution_status": row["execution_status"],
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
        write_report(output, rows, agent_requested=args.agent)
        return 0 if all(row["execution_status"] == "ok" for row in rows) else 1
    task = (
        selected[0]
        if args.task
        else next(item for item in tasks if item["id"] == "error_definition")
    )
    result = run_deterministic(task, suite, output, manifest)
    (output / "per_task_results.jsonl").write_text(
        json.dumps(result, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["execution_status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
