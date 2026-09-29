"""Task-local relation view and conservative invalidation."""

from __future__ import annotations

import shutil
from pathlib import Path

from agent_runtime.code_exploration.io import grep_result, read_file_result
from agent_runtime.code_exploration.service import CodeExplorationService
from agent_runtime.context_runtime import ObservationStore
from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import tool_write_file

FIXTURES = Path(__file__).parent / "fixtures/code_exploration/repos"


def _setup(tmp_path, name: str):
    root = tmp_path / "repo"
    shutil.copytree(FIXTURES / name, root)
    state = {
        "id": "session-1",
        "session_scope": {"session_id": "session-1", "workspace_id": "workspace-1"},
        "session_identity": {
            "session_id": "session-1",
            "workspace_id": "workspace-1",
            "task_id": "task-1",
            "run_id": "run-1",
        },
    }
    context = ToolContext(root=str(root), exploration_mode="relations", observation_state=state)
    service = CodeExplorationService(context, mode="relations", server_argv=())
    return root, state, context, service


def _observe(state, context, service, path: str) -> str:
    args = {"path": path}
    tool_result = read_file_result(context, args)
    facts = tool_result.metadata["retrieval_result"]
    store = ObservationStore(state, context.root)
    observation = store.put(
        "read_file",
        args,
        tool_result.content,
        source_dependencies=tool_result.metadata["source_dependencies"],
        retrieval_query_id=facts["query_id"],
    )
    store.close()
    service.observe("read_file", args, facts, observation.observation_id)
    return observation.observation_id


def test_contains_imports_and_test_imports_from_observed_files(tmp_path):
    root, state, context, service = _setup(tmp_path, "test_relation")
    first = _observe(state, context, service, "service.py")
    second = _observe(state, context, service, "test_service.py")
    response = service.relations({})
    view = response.metadata["relation_view"]
    assert set(view["observation_refs"]) == {first, second}
    assert set(view["covered_files"]) == {"service.py", "test_service.py"}
    assert len(view["inclusion_paths"]) == 2
    assert any(
        path["relation"] == "test_imports" and path["target"] == "service.py"
        for path in view["inclusion_paths"]
    )
    assert any(node.get("qualified_name") == "render" for node in view["nodes"])
    assert any(
        edge["kind"] == "test_imports" and edge["to"] == "file:service.py" for edge in view["edges"]
    )
    assert all(edge["observation_refs"] for edge in view["edges"])
    assert all(edge["dependency_versions"] for edge in view["edges"])
    assert not any("assert render" in str(node) for node in view["nodes"])
    cached = service.parsed_cache["service.py"][1]
    service.relations({})
    assert service.parsed_cache["service.py"][1] is cached
    service.close()


def test_unobserved_file_is_not_added_by_import_resolution(tmp_path):
    _, state, context, service = _setup(tmp_path, "cross_file_import")
    _observe(state, context, service, "service.py")
    view = service.relations({}).metadata["relation_view"]
    assert view["covered_files"] == ["service.py"]
    assert any(
        edge["kind"] == "imports" and edge["resolution"] == "unresolved" for edge in view["edges"]
    )
    assert not any(edge["to"] == "file:pkg/calc.py" for edge in view["edges"])
    _observe(state, context, service, "pkg/calc.py")
    view = service.relations({}).metadata["relation_view"]
    assert any(
        edge["kind"] == "imports" and edge["to"] == "file:pkg/calc.py" for edge in view["edges"]
    )
    service.close()


def test_relative_import_links_only_to_observed_target(tmp_path):
    root = tmp_path / "repo"
    package = root / "pkg"
    package.mkdir(parents=True)
    (package / "worker.py").write_text("from .helper import run\n", encoding="utf-8")
    (package / "helper.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    state = {"id": "session-1", "session_identity": {"task_id": "task-1"}}
    context = ToolContext(root=str(root), exploration_mode="relations", observation_state=state)
    service = CodeExplorationService(context, mode="relations", server_argv=())
    _observe(state, context, service, "pkg/worker.py")
    first = service.relations({}).metadata["relation_view"]
    assert any(edge["resolution"] == "unresolved" for edge in first["edges"])

    _observe(state, context, service, "pkg/helper.py")
    second = service.relations({}).metadata["relation_view"]
    assert any(
        edge["to"] == "file:pkg/helper.py" and edge["resolution"] == "candidate"
        for edge in second["edges"]
    )
    service.close()


def test_new_referrer_requires_and_appears_in_fresh_query(tmp_path):
    root, state, context, service = _setup(tmp_path, "test_relation")
    _observe(state, context, service, "service.py")
    first = service.relations({}).metadata["relation_view"]
    assert "new_caller.py" not in first["covered_files"]

    (root / "new_caller.py").write_text(
        "from service import render\nresult = render('x')\n", encoding="utf-8"
    )
    fresh = grep_result(context, {"pattern": "render", "path": ".", "glob": "*.py"})
    assert any(hit["path"] == "new_caller.py" for hit in fresh.metadata["retrieval_result"]["hits"])
    _observe(state, context, service, "new_caller.py")
    second = service.relations({}).metadata["relation_view"]
    assert "new_caller.py" in second["covered_files"]
    assert any(
        edge["from"] == "file:new_caller.py" and edge["to"] == "file:service.py"
        for edge in second["edges"]
    )
    service.close()


def test_source_change_and_observation_invalidation_clear_whole_view(tmp_path):
    root, state, context, service = _setup(tmp_path, "test_relation")
    oid = _observe(state, context, service, "service.py")
    service.relations({})
    old_epoch = service.epoch
    (root / "service.py").write_text("def render(value):\n    return value\n", encoding="utf-8")
    invalidated = service.relations({})
    assert invalidated.metadata["relation_view"]["status"] == "invalidated"
    assert service.epoch != old_epoch
    assert not service.evidence and not service.pending_candidates
    _observe(state, context, service, "service.py")
    store = ObservationStore(state, context.root)
    store.invalidate_paths(["service.py"])
    store.close()
    assert not service.validate_view()
    assert not service.evidence
    assert oid not in service.evidence
    service.close()


def test_successful_unrelated_write_clears_view_and_deleted_source_is_rejected(tmp_path):
    root, state, context, service = _setup(tmp_path, "test_relation")
    context.exploration_service = service
    _observe(state, context, service, "service.py")
    service.relations({})
    old_epoch = service.epoch
    result = tool_write_file(context, {"path": "new.py", "content": "value = 1\n"})
    assert not result.startswith("Error:")
    assert service.epoch != old_epoch
    assert not service.evidence and not service.pending_candidates

    _observe(state, context, service, "service.py")
    (root / "service.py").unlink()
    assert not service.validate_view()
    assert not service.evidence
    service.close()


def test_no_evidence_is_empty_and_disabled_modes_do_not_scan(tmp_path):
    _, _, context, service = _setup(tmp_path, "test_relation")
    empty = service.relations({}).metadata["relation_view"]
    assert empty["covered_files"] == []
    assert empty["candidate_snippets"] == []
    service.close()
    disabled = CodeExplorationService(context, mode="lsp", server_argv=())
    assert disabled.relations({}).metadata["relation_view"]["status"] == "disabled"


def test_lsp_reference_edge_keeps_resolution_and_observation(tmp_path):
    _, state, context, service = _setup(tmp_path, "alias_reference")
    _observe(state, context, service, "operations.py")
    _observe(state, context, service, "runner.py")
    versions = {
        path: digest for item in service.evidence.values() for path, digest in item.versions.items()
    }
    args = {"path": "operations.py", "line": 1, "column": 5, "operation": "references"}
    retrieval = {
        "execution": "ok",
        "observed_at": "2026-09-30T00:00:00Z",
        "dependency_versions": versions,
        "hits": [
            {
                "path": "runner.py",
                "range": {"start_line": 1, "end_line": 2},
                "source": "lsp",
                "resolution": "resolved_by_lsp",
            }
        ],
    }
    store = ObservationStore(state, context.root)
    stored = store.put("code_lookup", args, "reference result", source_dependencies=versions)
    store.close()
    service.observe("code_lookup", args, retrieval, stored.observation_id)
    view = service.relations({}).metadata["relation_view"]
    references = [edge for edge in view["edges"] if edge["kind"] == "references"]
    assert len(references) == 1
    assert references[0]["source"] == "lsp"
    assert references[0]["resolution"] == "resolved_by_lsp"
    assert references[0]["from"] == "file:runner.py"
    assert references[0]["to"] == "file:operations.py"
    assert references[0]["observation_refs"] == [stored.observation_id]
    service.close()


def test_relation_window_is_bounded_to_eight_observed_files(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    state = {"id": "session-1", "session_identity": {"task_id": "task-1"}}
    context = ToolContext(root=str(root), exploration_mode="relations", observation_state=state)
    service = CodeExplorationService(context, mode="relations", server_argv=())
    for index in range(10):
        path = f"module_{index}.py"
        (root / path).write_text(f"def function_{index}():\n    pass\n", encoding="utf-8")
        _observe(state, context, service, path)
    view = service.relations({}).metadata["relation_view"]
    assert len(view["covered_files"]) == 8
    assert len(view["nodes"]) <= 64
    assert len(view["edges"]) <= 128
    assert "evidence_window" in view["truncation_reasons"]
    service.close()


def test_corrupt_observation_blob_invalidates_view(tmp_path):
    _, state, context, service = _setup(tmp_path, "test_relation")
    oid = _observe(state, context, service, "service.py")
    if state["observations"][oid]["raw_ref"].startswith("memory:"):
        state["observation_blobs"][oid] = "tampered"
    else:
        blob = Path(state["observations"][oid]["raw_ref"])
        blob.write_text("tampered", encoding="utf-8")
    assert not service.validate_view()
    assert not service.evidence
    service.close()
