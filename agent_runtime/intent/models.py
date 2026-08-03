"""Intent Router schema: nodes, edges, graph, result, context."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

PRIMARY_ACTIONS: dict[str, str] = {
    # Core session / repair
    "ask": "ask",
    "remember": "promote_memory",
    "repair_request": "run_repair",
    "repair_issue": "run_repair",
    "help": "help",
    "clarify": "clarify",
    "cancel": "noop_cancel",
    "out_of_scope": "reject",
    # Enterprise coding-agent actions
    "explain": "explain_code",
    "review": "review_code",
    "refactor": "run_refactor",
    "implement": "run_implement",
    "test": "run_tests",
    "debug": "run_debug",
    "search": "search_codebase",
    "plan": "make_plan",
}

EdgeKind = Literal["sequence", "depends_on", "constrains"]
NodeRole = Literal["executable", "constraint", "clarify"]
GraphMode = Literal["single", "multi", "hybrid"]
Channel = Literal["repair", "repl"]


@dataclass
class Segment:
    index: int
    text: str
    cue: str | None = None  # None | "sequential" | "additive"


@dataclass
class IntentNode:
    id: str
    primary: str
    action: str
    role: NodeRole = "executable"
    span: dict[str, int] = field(default_factory=lambda: {"start": 0, "end": 0})
    text: str = ""
    slots: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    parser: str = "rule"
    priority: int = 0
    segment_index: int | None = None

    def __post_init__(self) -> None:
        if self.primary in PRIMARY_ACTIONS and not self.action:
            self.action = PRIMARY_ACTIONS[self.primary]


@dataclass
class IntentEdge:
    src: str
    dst: str
    kind: EdgeKind
    reason: str = ""


@dataclass
class IntentGraph:
    nodes: list[IntentNode] = field(default_factory=list)
    edges: list[IntentEdge] = field(default_factory=list)
    mode: GraphMode = "single"
    root_ids: list[str] = field(default_factory=list)

    def node_map(self) -> dict[str, IntentNode]:
        return {n.id: n for n in self.nodes}

    def executable_nodes(self) -> list[IntentNode]:
        return [n for n in self.nodes if n.role == "executable"]


@dataclass
class IntentResult:
    """Compatibility wrapper; ``graph`` is authoritative."""

    primary: str
    action: str
    confidence: float
    parser: str
    graph: IntentGraph
    secondary: list[str] = field(default_factory=list)
    slots: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    embed_top1: str | None = None
    embed_score: float | None = None
    confidence_breakdown: dict[str, float] = field(default_factory=dict)
    raw_signals: dict[str, Any] = field(default_factory=dict)


@dataclass
class RouteContext:
    channel: Channel = "repl"
    light_client: Any | None = None
    emit: Callable[..., None] | None = None
    max_executable_nodes: int = 4
    tau_node: float = 0.55
    tau_llm: float = 0.55
    tau_clarify: float = 0.45
    tau_exec: float = 0.60
    embed_fn: Callable[[str], Any] | None = None
    # Multi-turn: agent history (source of truth) + thin intent projection
    history: list[dict[str, Any]] | None = None
    dialogue: Any | None = None  # DialogueProjection | None
    # Candidate discovery: if set, append events under {candidate_root}/.agent/
    candidate_root: str | None = None
