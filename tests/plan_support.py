"""Deterministic real-filesystem Plan fixtures, without model/network calls."""

from pathlib import Path
from types import SimpleNamespace

from agent_runtime.plan_runtime import Completion, Plan, PlanNode
from agent_runtime.plan_runtime.session import PlanSession
from agent_runtime.tool_result import ToolResult

REGISTRY = {
    "read_file": {"side_effect": "read"},
    "write_file": {"side_effect": "write"},
    "run_shell": {"side_effect": "verify"},
    "quick_test": {"side_effect": "verify"},
    "grep": {"side_effect": "read"},
}


def session_for(root, **kwargs):
    return PlanSession(str(root), "task", "run", REGISTRY, **kwargs)


def simple_plan(session, reads=1):
    nodes = tuple(
        PlanNode(
            f"read-{i}",
            "explore",
            "inspect file",
            tool_allowlist=("read_file",),
            tool_name="read_file",
            arguments_json='{"path":"value.py"}',
            completion=(Completion("observation_present"),),
        )
        for i in range(reads)
    )
    nodes += (
        PlanNode(
            "analysis",
            "analyze",
            "record reasoning",
            depends_on=tuple(n.node_id for n in nodes),
            completion=(Completion("analysis_recorded"),),
        ),
        PlanNode(
            "edit",
            "edit",
            "apply patch",
            depends_on=("analysis",),
            tool_allowlist=("write_file", "read_file"),
            side_effect="write",
            completion=(Completion("patch_applied"),),
        ),
        PlanNode(
            "verify",
            "verify",
            "test actual patch",
            depends_on=("edit",),
            side_effect="verify",
            completion=(Completion("tests_passed"),),
        ),
    )
    return Plan("plan", **session.identity, nodes=nodes).seal()


def tool(session, name, args, raw):
    agent = SimpleNamespace(session={})
    return session.execute_tool(agent, name, args, raw)


def read(session, attempt):
    tool(
        session,
        "read_file",
        {"path": "value.py"},
        lambda: ToolResult(content=(Path(session.workspace) / "value.py").read_text()),
    )
    return session.tool_result(attempt)


def write(session, attempt):
    def raw():
        (Path(session.workspace) / "value.py").write_text("value = 2\n")
        return ToolResult(content="applied")

    tool(session, "write_file", {"path": "value.py"}, raw)
    return session.tool_result(attempt)


def through_analysis(session):
    for node in session.plan.nodes:
        if node.kind == "explore":
            session.run_node(node.node_id, lambda a: read(session, a))
    refs = [r for n in session.plan.nodes if n.kind == "explore" for r in n.output_evidence_refs]
    session.run_node("analysis", lambda a: session.analysis(a, "value is incorrect", refs))
