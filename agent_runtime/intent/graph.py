"""Intent graph validation, constraint merge, topological order."""

from __future__ import annotations

from collections import defaultdict, deque

from agent_runtime.intent.models import PRIMARY_ACTIONS, IntentEdge, IntentGraph, IntentNode


class GraphValidationError(ValueError):
    """Raised when graph invariants fail under strict validation."""


def clarify_graph(reason: str, *, confidence: float = 0.3) -> IntentGraph:
    node = IntentNode(
        id="n0",
        primary="clarify",
        action="clarify",
        role="clarify",
        text=reason,
        confidence=confidence,
        parser="planner",
        slots={"note": reason},
    )
    return IntentGraph(nodes=[node], edges=[], mode="single", root_ids=["n0"])


def _has_cycle(nodes: list[IntentNode], edges: list[IntentEdge]) -> bool:
    """Cycle detection over sequence/depends_on only (constrains ignored)."""
    ids = {n.id for n in nodes}
    adj: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        if e.kind == "constrains":
            continue
        if e.src in ids and e.dst in ids:
            adj[e.src].append(e.dst)

    white, gray, black = 0, 1, 2
    color = {nid: white for nid in ids}

    def dfs(u: str) -> bool:
        color[u] = gray
        for v in adj[u]:
            if color[v] == gray:
                return True
            if color[v] == white and dfs(v):
                return True
        color[u] = black
        return False

    return any(color[nid] == white and dfs(nid) for nid in ids)


def _exec_count(graph: IntentGraph) -> int:
    return sum(1 for n in graph.nodes if n.role == "executable")


def _check_constrains_roles(graph: IntentGraph) -> str | None:
    by_id = graph.node_map()
    for e in graph.edges:
        if e.kind != "constrains":
            continue
        src = by_id.get(e.src)
        if src is None:
            return f"constrains src missing: {e.src}"
        if src.role != "constraint":
            return f"constrains src {e.src} must have role=constraint"
    return None


def recompute_root_ids(graph: IntentGraph) -> list[str]:
    """Executable nodes with no incoming sequence/depends_on edge."""
    incoming: set[str] = set()
    for e in graph.edges:
        if e.kind == "constrains":
            continue
        incoming.add(e.dst)
    return [n.id for n in graph.nodes if n.role == "executable" and n.id not in incoming]


def validate_graph(
    graph: IntentGraph,
    *,
    max_executable_nodes: int = 4,
    strict: bool = False,
) -> IntentGraph:
    """Validate invariants; return clarify graph on soft failure (or raise if strict)."""
    err = _check_constrains_roles(graph)
    if err:
        if strict:
            raise GraphValidationError(err)
        return clarify_graph(err)

    if _has_cycle(graph.nodes, graph.edges):
        msg = "cycle detected in intent graph"
        if strict:
            raise GraphValidationError(msg)
        return clarify_graph(msg)

    if _exec_count(graph) > max_executable_nodes:
        msg = f"executable nodes exceed max={max_executable_nodes}"
        if strict:
            raise GraphValidationError(msg)
        return clarify_graph(msg)

    roots = recompute_root_ids(graph)
    return IntentGraph(
        nodes=list(graph.nodes),
        edges=list(graph.edges),
        mode=graph.mode,
        root_ids=roots,
    )


def merge_constraints(graph: IntentGraph) -> IntentGraph:
    """Merge constrains edge src slots into dst; keep constraint nodes in graph."""
    by_id = {n.id: n for n in graph.nodes}
    # copy slots so we don't mutate caller's nodes unexpectedly beyond merge
    nodes = [
        IntentNode(
            id=n.id,
            primary=n.primary,
            action=n.action,
            role=n.role,
            span=dict(n.span),
            text=n.text,
            slots=dict(n.slots),
            confidence=n.confidence,
            parser=n.parser,
            priority=n.priority,
            segment_index=n.segment_index,
        )
        for n in graph.nodes
    ]
    by_id = {n.id: n for n in nodes}

    for e in graph.edges:
        if e.kind != "constrains":
            continue
        src = by_id.get(e.src)
        dst = by_id.get(e.dst)
        if src is None or dst is None:
            continue
        for k, v in src.slots.items():
            if k not in dst.slots or dst.slots[k] in (None, "", [], {}):
                dst.slots[k] = v
            elif isinstance(dst.slots[k], list) and isinstance(v, list):
                for item in v:
                    if item not in dst.slots[k]:
                        dst.slots[k].append(item)
            # else keep dst value

    roots = recompute_root_ids(
        IntentGraph(nodes=nodes, edges=list(graph.edges), mode=graph.mode, root_ids=[])
    )
    return IntentGraph(nodes=nodes, edges=list(graph.edges), mode=graph.mode, root_ids=roots)


def topological_executable_nodes(graph: IntentGraph) -> list[IntentNode]:
    """Stable Kahn topo over executables; same-layer: -priority, then span.start, then id."""
    exec_nodes = [n for n in graph.nodes if n.role == "executable"]
    ids = {n.id for n in exec_nodes}
    by_id = {n.id: n for n in exec_nodes}

    indeg: dict[str, int] = {nid: 0 for nid in ids}
    adj: dict[str, list[str]] = defaultdict(list)
    for e in graph.edges:
        if e.kind == "constrains":
            continue
        if e.src in ids and e.dst in ids:
            adj[e.src].append(e.dst)
            indeg[e.dst] += 1

    def sort_key(nid: str) -> tuple:
        n = by_id[nid]
        span_start = int(n.span.get("start", 0) if n.span else 0)
        return (-n.priority, span_start, nid)

    ready = sorted([nid for nid, d in indeg.items() if d == 0], key=sort_key)
    queue: deque[str] = deque(ready)
    ordered: list[IntentNode] = []
    seen: set[str] = set()

    while queue:
        # re-sort ready set each pop for stable same-layer ordering when new zeros appear
        batch = sorted(queue, key=sort_key)
        queue.clear()
        for u in batch:
            if u in seen:
                continue
            seen.add(u)
            ordered.append(by_id[u])
            for v in adj[u]:
                indeg[v] -= 1
                if indeg[v] == 0 and v not in seen:
                    queue.append(v)

    if len(ordered) != len(exec_nodes):
        # residual cycle among executables — return clarify singleton callers should validate first
        return list(clarify_graph("cycle among executable nodes").executable_nodes())

    return ordered


def action_for_primary(primary: str) -> str:
    return PRIMARY_ACTIONS.get(primary, primary)
