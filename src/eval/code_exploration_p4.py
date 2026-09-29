"""Isolated T/L/R code exploration runs and evidence-only reporting."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from agent_runtime.code_exploration.io import grep_result, read_file_result
from agent_runtime.code_exploration.service import CodeExplorationService
from agent_runtime.context_runtime import ObservationStore
from agent_runtime.tool_context import ToolContext


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _workspace(task: dict, suite: Path, parent: Path) -> Path:
    root = parent / "repo"
    shutil.copytree(suite.parent / task["fixture_dir"], root)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
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
        cwd=root,
        check=True,
    )
    return root


def _pylsp(manifest: dict) -> tuple[str, ...]:
    path = manifest["runtime_versions"]["pylsp"].get("path")
    if not path or not Path(path).is_file():
        raise RuntimeError("Real pylsp executable is required for lsp/relations evaluation")
    return (str(Path(path).resolve()),)


def _observe(
    context: ToolContext, service: CodeExplorationService, path: str, calls: list[dict]
) -> None:
    args = {"path": path}
    result = read_file_result(context, args)
    calls.append(
        {
            "tool": "read_file",
            "args": args,
            "status": result.status,
            "retrieval_result": result.metadata.get("retrieval_result", {}),
        }
    )
    facts = result.metadata.get("retrieval_result", {})
    if facts.get("execution") != "ok":
        return
    store = ObservationStore(context.observation_state, context.root)
    try:
        record = store.put(
            "read_file",
            args,
            result.content,
            source_dependencies=result.metadata.get("source_dependencies"),
            retrieval_query_id=facts.get("query_id", ""),
        )
    finally:
        store.close()
    service.observe("read_file", args, facts, record.observation_id)


def _observe_lookup(
    context: ToolContext,
    service: CodeExplorationService,
    args: dict,
    result,
    calls: list[dict],
) -> None:
    facts = result.metadata.get("retrieval_result", {})
    calls.append(
        {"tool": "code_lookup", "args": args, "status": result.status, "retrieval_result": facts}
    )
    if facts.get("execution") != "ok" or not facts.get("hits"):
        return
    store = ObservationStore(context.observation_state, context.root)
    try:
        record = store.put(
            "code_lookup",
            args,
            result.content,
            source_dependencies=facts.get("dependency_versions"),
            retrieval_query_id=facts.get("query_id", ""),
        )
    finally:
        store.close()
    service.observe("code_lookup", args, facts, record.observation_id)


def _location(hit: dict) -> dict:
    span = hit.get("range") or {}
    start = span.get("start") or {}
    return {"path": hit.get("path", ""), "line": start.get("line") or span.get("start_line")}


def _contract_checks(task: dict, oracle: dict, locations: list[dict], edges: list[dict]) -> dict:
    accepted = oracle["accepted_definitions"]
    definition = any(
        location.get("path") == item["path"] and location.get("line") == item["range"]["start_line"]
        for location in locations
        for item in accepted
    )
    relation_checks = {}
    for relation in oracle["required_relations"]:
        key = f"{relation['type']}:{relation['source']}:{relation['target']}"
        relation_checks[key] = any(
            edge.get("kind") == relation["type"]
            and edge.get("from") == f"file:{relation['source']}"
            and edge.get("to") == f"file:{relation['target']}"
            for edge in edges
        )
    return {
        "definition_location": definition,
        "required_relations": relation_checks,
        "oracle_used_only_for_scoring": True,
        "task_id": task["id"],
    }


def deterministic_run(
    task: dict, oracle: dict, suite: Path, output: Path, manifest: dict, mode: str
) -> dict:
    """Run a fixed public probe; these rows are never reported as Agent outcomes."""
    started = time.monotonic()
    calls: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="fixloop-code-eval-") as temporary:
        root = _workspace(task, suite, Path(temporary))
        state = {
            "id": uuid.uuid4().hex,
            "session_identity": {
                "workspace_id": hashlib.sha256(str(root).encode()).hexdigest(),
                "session_id": uuid.uuid4().hex,
                "task_id": uuid.uuid4().hex,
                "run_id": uuid.uuid4().hex,
            },
        }
        context = ToolContext(root=str(root), exploration_mode=mode, observation_state=state)
        service = CodeExplorationService(
            context, mode=mode, server_argv=_pylsp(manifest) if mode != "text" else ()
        )
        try:
            probe = task["deterministic_probe"]
            first = time.monotonic()
            grep = grep_result(context, {"pattern": probe["pattern"], "path": probe["path"]})
            cold_ms = round((time.monotonic() - first) * 1000)
            calls.append(
                {
                    "tool": "grep",
                    "args": {
                        "path": probe["path"],
                        "pattern_hash": hashlib.sha256(probe["pattern"].encode()).hexdigest(),
                    },
                    "status": grep.status,
                    "retrieval_result": grep.metadata.get("retrieval_result", {}),
                }
            )
            text_locations = [
                _location(hit) for hit in grep.metadata.get("retrieval_result", {}).get("hits", [])
            ]
            lsp_locations: list[dict] = []
            lsp_status = "not_requested"
            cold_lookup_ms = None
            warm_lookup_ms = None
            if mode != "text" and task.get("lookup_anchor"):
                lookup_start = time.monotonic()
                lookup = service.lookup(task["lookup_anchor"])
                cold_lookup_ms = round((time.monotonic() - lookup_start) * 1000)
                facts = lookup.metadata.get("retrieval_result", {})
                lsp_locations = [_location(hit) for hit in facts.get("hits", [])]
                lsp_status = facts.get("execution", "unknown")
                calls.append(
                    {
                        "tool": "code_lookup",
                        "args": task["lookup_anchor"],
                        "status": lookup.status,
                        "retrieval_result": facts,
                    }
                )
                lookup_start = time.monotonic()
                repeated_lookup = service.lookup(task["lookup_anchor"])
                warm_lookup_ms = round((time.monotonic() - lookup_start) * 1000)
                calls.append(
                    {
                        "tool": "code_lookup",
                        "repeat": True,
                        "status": repeated_lookup.status,
                        "retrieval_result": repeated_lookup.metadata.get("retrieval_result", {}),
                    }
                )
            view = {}
            if mode == "relations":
                for path in dict.fromkeys([*task["entry_paths"], probe["path"]]):
                    _observe(context, service, path, calls)
                reference_anchor = task.get("reference_anchor")
                if reference_anchor:
                    reference = service.lookup(reference_anchor)
                    _observe_lookup(context, service, reference_anchor, reference, calls)
                relation = service.relations({})
                view = relation.metadata.get("relation_view", {})
                calls.append(
                    {"tool": "code_relations", "args": {}, "status": relation.status, "view": view}
                )
            warm_start = time.monotonic()
            repeat = grep_result(context, {"pattern": probe["pattern"], "path": probe["path"]})
            warm_ms = round((time.monotonic() - warm_start) * 1000)
            calls.append(
                {
                    "tool": "grep",
                    "repeat": True,
                    "status": repeat.status,
                    "retrieval_result": repeat.metadata.get("retrieval_result", {}),
                }
            )
            checks = _contract_checks(
                task, oracle, lsp_locations or text_locations, view.get("edges", [])
            )
            trace = output / "traces" / f"{mode}-{task['id']}.json"
            _write_json(trace, calls)
            return {
                "task_id": task["id"],
                "mode": mode,
                "repetition": 1,
                "evaluation_kind": "deterministic_contract_not_agent_effect",
                "execution_status": "ok" if grep.ok and repeat.ok else "error",
                "predicted_locations": lsp_locations or text_locations,
                "text_locations": text_locations,
                "lsp_locations": lsp_locations,
                "lsp_status": lsp_status,
                "relation_claims": view.get("edges", []),
                "coverage": view.get("coverage", {}),
                "truncation_reasons": view.get("truncation_reasons", []),
                "contract_checks": checks,
                "correctness": None,
                "metrics": {
                    "tool_calls": len(calls),
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "cold_probe_ms": cold_ms,
                    "warm_probe_ms": warm_ms,
                    "cold_lookup_ms": cold_lookup_ms,
                    "warm_lookup_ms": warm_lookup_ms,
                },
                "fixture_hash": manifest["fixture_hashes"][task["id"]],
                "config_hash": hashlib.sha256(
                    _json({"mode": mode, "task": task}).encode()
                ).hexdigest(),
                "trace_ref": trace.relative_to(output).as_posix(),
                "evidence_refs": view.get("observation_refs", []),
                "verification_result": None,
            }
        finally:
            service.close()
            # Windows may release pylsp's working-directory handle just after exit.
            time.sleep(0.5)


def agent_run(
    task: dict,
    oracle: dict,
    suite: Path,
    output: Path,
    manifest: dict,
    mode: str,
    repetition: int,
    *,
    provider: str,
    model: str | None,
) -> dict:
    """One real-model attempt in a fresh fixture repo and Agent session."""
    from agent_runtime.bootstrap import create_model_client
    from agent_runtime.config import AgentConfig
    from agent_runtime.runtime import Agent
    from agent_runtime.workspace import WorkspaceContext

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="fixloop-code-agent-") as temporary:
        root = _workspace(task, suite, Path(temporary))
        client = create_model_client(provider=provider, model=model, temperature=0)
        config = AgentConfig(
            provider=provider,
            model=model or getattr(client, "model", ""),
            approval="auto",
            temperature=0,
            max_steps=10,
            max_new_tokens=2048,
            code_exploration={
                "mode": mode,
                "server_argv": _pylsp(manifest) if mode != "text" else None,
            },
        )
        agent = Agent(config, client, WorkspaceContext.build(str(root)), cwd=str(root))
        schemas = {
            name: {key: value for key, value in spec.items() if not callable(value)}
            for name, spec in agent.tools.items()
        }
        tool_schema_hash = hashlib.sha256(_json(schemas).encode()).hexdigest()
        error = ""
        try:
            answer = agent.ask(task["prompt"], skip_plan=True)
        except Exception as exc:
            answer = ""
            error = f"{type(exc).__name__}: {exc}"
        trace = output / "traces" / f"{mode}-{task['id']}-{repetition}.json"
        observations = agent.session.get("tool_observations", [])
        _write_json(
            trace,
            {
                "answer": answer,
                "error": error,
                "session_id": agent.session.get("id"),
                "observations": observations,
            },
        )
        diff = subprocess.run(
            ["git", "diff", "--binary"], cwd=root, capture_output=True, text=True, check=True
        ).stdout
        diff_ref = output / "evidence" / f"{mode}-{task['id']}-{repetition}.patch"
        diff_ref.parent.mkdir(parents=True, exist_ok=True)
        diff_ref.write_text(diff, encoding="utf-8")
        verification = None
        if task.get("verification_argv"):
            completed = subprocess.run(
                task["verification_argv"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            verification = {
                "exit_code": completed.returncode,
                "output_tail": (completed.stdout + completed.stderr)[-4000:],
            }
        found = re.findall(r"([\w./\\-]+\.py):?(\d+)?", answer)
        locations = [
            {"path": path.replace("\\", "/"), "line": int(line) if line else None}
            for path, line in found
        ]
        location_or_test = (
            bool(verification and verification["exit_code"] == oracle.get("expected_test_exit"))
            if task.get("verification_argv")
            else any(
                item["path"] == accepted["path"]
                for item in locations
                for accepted in oracle["accepted_definitions"]
            )
        )
        return {
            "task_id": task["id"],
            "mode": mode,
            "repetition": repetition,
            "evaluation_kind": "real_agent",
            "execution_status": "error" if error else "ok",
            "model": config.model,
            "provider": provider,
            "tool_schema_hash": tool_schema_hash,
            "session_id": agent.session.get("id"),
            "predicted_locations": locations,
            "correctness": location_or_test if verification is not None else None,
            "location_mention_proxy": location_or_test if verification is None else None,
            "scoring_method": "targeted_test"
            if verification is not None
            else "location_mention_requires_manual_review",
            "verification_result": verification,
            "metrics": {
                "duration_ms": round((time.monotonic() - started) * 1000),
                "tool_calls": len(observations),
                "usage": getattr(client, "session_usage", {}),
            },
            "fixture_hash": manifest["fixture_hashes"][task["id"]],
            "config_hash": hashlib.sha256(_json(config.snapshot()).encode()).hexdigest(),
            "trace_ref": trace.relative_to(output).as_posix(),
            "diff_ref": diff_ref.relative_to(output).as_posix(),
            "evidence_refs": [
                item.get("observation_id") for item in observations if item.get("observation_id")
            ],
            "error": error,
        }


def write_report(output: Path, rows: list[dict], *, agent_requested: bool) -> None:
    if agent_requested:
        lines = [
            "# Code exploration MVP: real Agent runs",
            "",
            f"Runs: {len(rows)}. Each run used a fresh fixture repository and Agent session.",
            "Location mention is a coarse answer screen, not verified correctness.",
            "Repair success requires the fixture's targeted test to exit with code 0.",
            "",
            "| Mode | Task | Runs | Path mention | Verified repairs | Mean elapsed ms |",
            "|---|---|---:|---:|---:|---:|",
        ]
        for mode in ("text", "lsp", "relations"):
            for task_id in dict.fromkeys(row["task_id"] for row in rows):
                group = [row for row in rows if row["mode"] == mode and row["task_id"] == task_id]
                if not group:
                    continue
                has_verification = any(row.get("verification_result") is not None for row in group)
                mentions = sum(
                    bool(row.get("location_mention_proxy", row.get("correctness"))) for row in group
                )
                repairs = (
                    sum(bool(row.get("correctness")) for row in group) if has_verification else 0
                )
                durations = [row.get("metrics", {}).get("duration_ms") for row in group]
                measured = [duration for duration in durations if isinstance(duration, int)]
                mean_ms = round(sum(measured) / len(measured)) if measured else "n/a"
                lines.append(
                    f"| {mode} | {task_id} | {len(group)} | "
                    f"{mentions if not has_verification else 'n/a'} | "
                    f"{repairs if has_verification else 'n/a'} | {mean_ms} |"
                )
        lines += [
            "",
            "## Model and tool use",
            "",
            "Read `per_task_results.jsonl` for per-run token usage, tool calls, "
            "session ID, source hashes and trace references.",
            "Review traces before attributing an answer to LSP or the relation view.",
            "The deterministic contract report is in the separate offline run.",
        ]
        errors = [row for row in rows if row["execution_status"] != "ok"]
        lines += ["", "## Execution errors", ""]
        lines += [
            f"- {row['mode']}/{row['task_id']}/#{row['repetition']}: "
            f"{row.get('error', row['execution_status'])}"
            for row in errors
        ] or ["- None recorded."]
        (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return
    lines = [
        "# Code exploration MVP evaluation",
        "",
        f"Runs: {len(rows)}. Evaluation: "
        f"{'real Agent' if agent_requested else 'deterministic contract only'}.",
        "",
        "| Mode | Task | Repetitions | Successful runs | Definition checks | Relation checks |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for mode in ("text", "lsp", "relations"):
        for task_id in dict.fromkeys(row["task_id"] for row in rows):
            group = [row for row in rows if row["mode"] == mode and row["task_id"] == task_id]
            if not group:
                continue
            definitions = sum(
                bool(row.get("contract_checks", {}).get("definition_location")) for row in group
            )
            relations = sum(
                sum(
                    bool(ok)
                    for ok in row.get("contract_checks", {}).get("required_relations", {}).values()
                )
                for row in group
            )
            successes = sum(bool(row.get("correctness")) for row in group) if agent_requested else 0
            lines.append(
                f"| {mode} | {task_id} | {len(group)} | "
                f"{successes if agent_requested else 'n/a'} | {definitions} | {relations} |"
            )
    lines += [
        "",
        "Deterministic probes are scripted checks, not Agent effectiveness evidence.",
        "Cold/warm timings are measured within each isolated run; they include tool work only.",
        "",
        "| Mode | Mean cold text ms | Mean warm text ms | Mean cold LSP ms | Mean warm LSP ms |",
        "|---|---:|---:|---:|---:|",
    ]
    for mode in ("text", "lsp", "relations"):
        group = [row for row in rows if row["mode"] == mode]
        if not group:
            continue

        def mean(key: str) -> str:
            values = [row.get("metrics", {}).get(key) for row in group]
            numbers = [value for value in values if isinstance(value, int)]
            return str(round(sum(numbers) / len(numbers))) if numbers else "n/a"

        lines.append(
            f"| {mode} | {mean('cold_probe_ms')} | {mean('warm_probe_ms')} | "
            f"{mean('cold_lookup_ms')} | {mean('warm_lookup_ms')} |"
        )
    lines += ["", "## Failed or incomplete checks", ""]
    failures = []
    for row in rows:
        checks = row.get("contract_checks", {})
        missing = [
            name for name, passed in checks.get("required_relations", {}).items() if not passed
        ]
        if checks and not checks.get("definition_location"):
            missing.insert(0, "definition_location")
        if row["execution_status"] != "ok":
            missing.insert(0, f"execution:{row['execution_status']}")
        if missing:
            failures.append(
                f"- {row['mode']}/{row['task_id']}/#{row['repetition']}: " + ", ".join(missing)
            )
    lines += failures or ["- None in this run."]
    lines += [
        "",
        "Text and LSP modes do not produce a relation view. The `repair_import` fixture "
        "starts with a broken import, so its target relation should remain unresolved "
        "until a verified repair is made.",
        "Per-run traces retain coverage, truncation, provenance and degraded LSP results.",
    ]
    if not agent_requested:
        lines += ["", "Real model comparison and verified repair: pending explicit authorization."]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
