"""Intent graph validation, constraint merge, and topological order."""

from __future__ import annotations

import pytest

from agent_runtime.intent.graph import (
    GraphValidationError,
    clarify_graph,
    merge_constraints,
    topological_executable_nodes,
    validate_graph,
)
from agent_runtime.intent.models import IntentEdge, IntentGraph, IntentNode


def _node(
    nid: str,
    primary: str = "ask",
    *,
    role: str = "executable",
    confidence: float = 0.9,
    priority: int = 0,
    span_start: int = 0,
    slots: dict | None = None,
    text: str = "",
) -> IntentNode:
    return IntentNode(
        id=nid,
        primary=primary,
        action={
            "ask": "ask",
            "remember": "promote_memory",
            "repair_request": "run_repair",
            "repair_issue": "run_repair",
            "help": "help",
            "clarify": "clarify",
            "cancel": "noop_cancel",
            "out_of_scope": "reject",
            "explain": "explain_code",
            "review": "review_code",
            "refactor": "run_refactor",
            "implement": "run_implement",
            "test": "run_tests",
            "debug": "run_debug",
            "search": "search_codebase",
            "plan": "make_plan",
        }.get(primary, primary),
        role=role,  # type: ignore[arg-type]
        span={"start": span_start, "end": span_start + 1},
        text=text or nid,
        slots=slots or {},
        confidence=confidence,
        parser="rule",
        priority=priority,
    )


class TestValidateGraph:
    def test_valid_single_node(self):
        g = IntentGraph(nodes=[_node("n0")], edges=[], mode="single", root_ids=["n0"])
        out = validate_graph(g, max_executable_nodes=4)
        assert out.mode == "single"
        assert len(out.nodes) == 1

    def test_cycle_returns_clarify(self):
        g = IntentGraph(
            nodes=[_node("n0", "remember"), _node("n1", "repair_request")],
            edges=[
                IntentEdge(src="n0", dst="n1", kind="sequence"),
                IntentEdge(src="n1", dst="n0", kind="depends_on"),
            ],
            mode="multi",
            root_ids=[],
        )
        out = validate_graph(g, max_executable_nodes=4)
        assert out.mode == "single"
        assert len(out.nodes) == 1
        assert out.nodes[0].primary == "clarify"
        assert out.nodes[0].action == "clarify"

    def test_too_many_executable_clarify(self):
        nodes = [_node(f"n{i}", "ask", span_start=i) for i in range(5)]
        edges = [
            IntentEdge(src=f"n{i}", dst=f"n{i + 1}", kind="sequence") for i in range(4)
        ]
        g = IntentGraph(nodes=nodes, edges=edges, mode="multi", root_ids=["n0"])
        out = validate_graph(g, max_executable_nodes=4)
        assert out.nodes[0].primary == "clarify"

    def test_constrains_src_must_be_constraint_role(self):
        g = IntentGraph(
            nodes=[_node("n0", "repair_request"), _node("n1", "ask", role="executable")],
            edges=[IntentEdge(src="n1", dst="n0", kind="constrains")],
            mode="hybrid",
            root_ids=["n0"],
        )
        with pytest.raises(GraphValidationError):
            validate_graph(g, max_executable_nodes=4, strict=True)
        soft = validate_graph(g, max_executable_nodes=4, strict=False)
        assert soft.nodes[0].primary == "clarify"


class TestMergeConstraints:
    def test_merges_slots_onto_executable(self):
        repair = _node("n0", "repair_request", slots={"issue_type": "type_error"})
        constraint = _node(
            "n1",
            "ask",
            role="constraint",
            slots={"suspect_files": ["foo.py"], "note": "only foo"},
        )
        g = IntentGraph(
            nodes=[repair, constraint],
            edges=[IntentEdge(src="n1", dst="n0", kind="constrains", reason="scope")],
            mode="hybrid",
            root_ids=["n0"],
        )
        merged = merge_constraints(g)
        exec_nodes = [n for n in merged.nodes if n.role == "executable"]
        assert len(exec_nodes) == 1
        assert exec_nodes[0].slots["suspect_files"] == ["foo.py"]
        assert exec_nodes[0].slots["issue_type"] == "type_error"
        assert exec_nodes[0].slots["note"] == "only foo"
        # constraint node retained in graph but not executable for topo
        assert any(n.role == "constraint" for n in merged.nodes)


class TestTopologicalOrder:
    def test_sequence_order(self):
        g = IntentGraph(
            nodes=[
                _node("n0", "remember", span_start=0),
                _node("n1", "repair_request", span_start=10),
            ],
            edges=[IntentEdge(src="n0", dst="n1", kind="sequence")],
            mode="multi",
            root_ids=["n0"],
        )
        ordered = topological_executable_nodes(merge_constraints(g))
        assert [n.id for n in ordered] == ["n0", "n1"]

    def test_same_layer_priority_then_span(self):
        # two roots: higher priority first; tie-break by span.start
        g = IntentGraph(
            nodes=[
                _node("n_low", "ask", priority=1, span_start=5),
                _node("n_hi", "help", priority=5, span_start=20),
                _node("n_early", "remember", priority=1, span_start=1),
            ],
            edges=[],
            mode="multi",
            root_ids=["n_low", "n_hi", "n_early"],
        )
        ordered = topological_executable_nodes(g)
        assert [n.id for n in ordered] == ["n_hi", "n_early", "n_low"]

    def test_constrains_nodes_excluded_from_exec_order(self):
        g = IntentGraph(
            nodes=[
                _node("n0", "repair_request"),
                _node("n1", "ask", role="constraint", slots={"language": "python"}),
            ],
            edges=[IntentEdge(src="n1", dst="n0", kind="constrains")],
            mode="hybrid",
            root_ids=["n0"],
        )
        ordered = topological_executable_nodes(merge_constraints(g))
        assert [n.id for n in ordered] == ["n0"]


class TestClarifyHelper:
    def test_clarify_graph_shape(self):
        g = clarify_graph("cycle detected", confidence=0.3)
        assert g.mode == "single"
        assert g.nodes[0].primary == "clarify"
        assert g.root_ids == ["n0"]
