"""Retrieval contracts visible through the existing tool registry."""

from agent_runtime.code_exploration.io import grep_result, read_file_result
from agent_runtime.context_runtime import ObservationStore
from agent_runtime.tool_context import ToolContext
from agent_runtime.tool_result import ToolResult, require_tool_result
from agent_runtime.tools import build_tool_registry, tool_grep, tool_read_file


def test_registry_returns_structured_metadata_and_legacy_text(tmp_path):
    (tmp_path / "mod.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    context = ToolContext(root=str(tmp_path))
    registry = build_tool_registry(context)
    for tool, args in (
        ("read_file", {"path": "mod.py"}),
        ("grep", {"pattern": "answer", "path": "."}),
        ("list_files", {"path": "."}),
    ):
        result = require_tool_result(registry[tool]["run"](args), tool_name=tool)
        assert isinstance(result, ToolResult)
        assert result.metadata["retrieval_result"]["schema_version"] == "1"
        assert result.metadata["retrieval_result"]["execution"] == "ok"
    assert "def answer" in tool_read_file(context, {"path": "mod.py"}).content
    assert "mod.py:1" in tool_grep(context, {"pattern": "answer"}).content
    listing = registry["list_files"]["run"]({"path": "."})
    assert listing.metadata["retrieval_result"]["hits"][0]["path"] == "mod.py"
    from src.tools.registry import build_repair_tools

    repair_grep = build_repair_tools(context)["grep"]["run"]({"pattern": "answer"})
    assert repair_grep.metadata["retrieval_result"]["hits"][0]["path"] == "mod.py"


def test_single_file_search_and_full_hash_are_distinct_from_excerpt(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("def answer():\n    return 42\n", encoding="utf-8")
    result = grep_result(ToolContext(root=str(tmp_path)), {"pattern": "answer", "path": "a.py"})
    retrieval = result.metadata["retrieval_result"]
    assert retrieval["hits"][0]["path"] == "a.py"
    assert retrieval["hits"][0]["content_hash"] == retrieval["dependency_versions"]["a.py"]
    assert retrieval["hits"][0]["excerpt_hash"] != retrieval["hits"][0]["content_hash"]


def test_no_match_is_complete_only_after_file_scan(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    result = grep_result(ToolContext(root=str(tmp_path)), {"pattern": "missing"})
    retrieval = result.metadata["retrieval_result"]
    assert retrieval["hits"] == []
    assert retrieval["completeness"] == "complete_in_scope"
    assert "0 matches" in result.content


def test_read_file_does_not_claim_complete_file_hash_for_partial_range(tmp_path):
    (tmp_path / "a.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    result = read_file_result(ToolContext(root=str(tmp_path)), {"path": "a.py", "end": 1})
    retrieval = result.metadata["retrieval_result"]
    assert retrieval["hits"][0]["excerpt_hash"]
    assert retrieval["hits"][0]["content_hash"] is None
    assert retrieval["dependency_versions"] == {}


def test_observation_uses_retrieval_versions_without_reopening_source(tmp_path, monkeypatch):
    source = tmp_path / "source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    original = source.open

    def forbidden_open(*args, **kwargs):
        raise AssertionError("Observation insert reread retrieval source")

    monkeypatch.setattr(
        source.__class__,
        "open",
        lambda self, *a, **k: (
            forbidden_open(*a, **k) if self == source else original.__func__(self, *a, **k)
        ),
    )
    store = ObservationStore({}, root=str(tmp_path))
    try:
        observation = store.put(
            "grep",
            {"pattern": "value", "path": "."},
            "source.py:1: value = 1",
            source_dependencies={"source.py": "abc123"},
            retrieval_query_id="q1",
        )
        assert "source.py" in observation.dependencies
    finally:
        store.close()
