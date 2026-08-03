"""Multi-intent planner: single / multi / hybrid + edge construction."""

from __future__ import annotations

import re
from typing import Any

from agent_runtime.intent.graph import (
    clarify_graph,
    merge_constraints,
    recompute_root_ids,
    validate_graph,
)
from agent_runtime.intent.models import (
    PRIMARY_ACTIONS,
    IntentEdge,
    IntentGraph,
    IntentNode,
    Segment,
)
from agent_runtime.intent.rules import RuleHit, is_constraint_text

_MUTEX = frozenset(PRIMARY_ACTIONS.keys()) - frozenset({"clarify"})
# ask/explain are mergeable peers (not multi-exec by default)
_ASK_FAMILY = frozenset({"ask", "explain"})
_DEPENDS_CUE = re.compile(r"(先|必须|必須|must\s+first|before\s+you)", re.I)


def _node_from_hit(
    nid: str,
    hit: RuleHit,
    seg: Segment,
    *,
    role: str = "executable",
    span_start: int = 0,
) -> IntentNode:
    primary = hit.primary
    if role == "constraint":
        # keep slots; primary unused for exec
        pass
    return IntentNode(
        id=nid,
        primary=primary if role != "constraint" else hit.primary,
        action=PRIMARY_ACTIONS.get(primary, hit.action),
        role=role,  # type: ignore[arg-type]
        span={"start": span_start, "end": span_start + max(len(seg.text), 1)},
        text=seg.text,
        slots=dict(hit.slots),
        confidence=hit.confidence,
        parser=hit.parser,
        segment_index=seg.index,
    )


def _is_strong_executable(hit: RuleHit, *, tau_node: float) -> bool:
    if hit.reason == "rule:constraint":
        return False
    if hit.primary in ("clarify",):
        return False
    if hit.primary not in _MUTEX:
        return False
    return hit.confidence >= tau_node


def plan(
    segment_hits: list[tuple[Segment, RuleHit]],
    *,
    channel: str = "repl",
    max_executable_nodes: int = 4,
    tau_node: float = 0.55,
) -> IntentGraph:
    """Build IntentGraph from per-segment rule hits (embed fusion applied upstream)."""
    if not segment_hits:
        return clarify_graph("empty input")

    # Full-text slash short-circuit already handled in router; still guard first seg
    first_seg, first_hit = segment_hits[0]
    if first_hit.primary in ("help", "cancel") and first_hit.confidence >= 0.99:
        node = _node_from_hit("n0", first_hit, first_seg)
        return IntentGraph(nodes=[node], edges=[], mode="single", root_ids=["n0"])

    # Repair channel: fold to single repair_issue + constraints
    if channel == "repair":
        return _plan_repair_channel(segment_hits, max_executable_nodes=max_executable_nodes)

    # Classify each segment
    classified: list[tuple[Segment, RuleHit, str]] = []  # role tag: exec|constraint|weak
    for seg, hit in segment_hits:
        if hit.reason == "rule:constraint" or is_constraint_text(seg.text):
            classified.append((seg, hit, "constraint"))
        elif _is_strong_executable(hit, tau_node=tau_node):
            classified.append((seg, hit, "exec"))
        else:
            classified.append((seg, hit, "weak"))

    exec_items = [(s, h) for s, h, r in classified if r == "exec"]
    constraint_items = [(s, h) for s, h, r in classified if r == "constraint"]

    # Ask/explain merge: peers in ask-family → single node (prefer explain)
    if (
        exec_items
        and all(h.primary in _ASK_FAMILY for _, h in exec_items)
        and len(exec_items) >= 2
    ):
        joined = " ".join(s.text for s, _ in segment_hits)
        primary = (
            "explain"
            if any(h.primary == "explain" for _, h in exec_items)
            else "ask"
        )
        hit = RuleHit(
            primary,
            PRIMARY_ACTIONS[primary],
            max(h.confidence for _, h in exec_items),
            reason="planner:ask_family_merge",
        )
        seg = Segment(index=0, text=joined)
        node = _node_from_hit("n0", hit, seg)
        g = IntentGraph(nodes=[node], edges=[], mode="single", root_ids=["n0"])
        return validate_graph(g, max_executable_nodes=max_executable_nodes)

    # help/cancel exclusivity when mixed
    for seg, hit in exec_items:
        if hit.primary == "cancel":
            node = _node_from_hit("n0", hit, seg)
            return IntentGraph(nodes=[node], edges=[], mode="single", root_ids=["n0"])
    help_items = [(s, h) for s, h in exec_items if h.primary == "help"]
    if help_items and len(exec_items) > 1:
        seg, hit = help_items[0]
        node = _node_from_hit("n0", hit, seg)
        return IntentGraph(nodes=[node], edges=[], mode="single", root_ids=["n0"])

    if len(exec_items) == 0:
        # weak-only → ask or clarify
        best = max(segment_hits, key=lambda x: x[1].confidence)
        seg, hit = best
        if hit.confidence < tau_node:
            return clarify_graph("weak signal", confidence=hit.confidence)
        node = _node_from_hit("n0", hit, seg)
        if node.primary == "clarify":
            node.role = "clarify"
        return IntentGraph(nodes=[node], edges=[], mode="single", root_ids=["n0"])

    if len(exec_items) == 1:
        seg, hit = exec_items[0]
        nodes = [_node_from_hit("n0", hit, seg, span_start=0)]
        edges: list[IntentEdge] = []
        mode = "single"
        if constraint_items:
            mode = "hybrid"
            for i, (cseg, chit) in enumerate(constraint_items, start=1):
                cid = f"n{i}"
                nodes.append(
                    _node_from_hit(cid, chit, cseg, role="constraint", span_start=i * 10)
                )
                edges.append(
                    IntentEdge(src=cid, dst="n0", kind="constrains", reason="constraint attach")
                )
        # also attach weak segments as note constraints if hybrid-ish
        g = IntentGraph(nodes=nodes, edges=edges, mode=mode, root_ids=["n0"])
        g = merge_constraints(g) if mode == "hybrid" else g
        return validate_graph(g, max_executable_nodes=max_executable_nodes)

    # multi: ≥2 mutex executables
    if len(exec_items) > max_executable_nodes:
        return clarify_graph(f"too many executables: {len(exec_items)}")

    nodes = []
    for i, (seg, hit) in enumerate(exec_items):
        nodes.append(_node_from_hit(f"n{i}", hit, seg, span_start=i * 10))
    edges = []
    full_text = " ".join(s.text for s, _ in segment_hits)
    edge_kind = "depends_on" if _DEPENDS_CUE.search(full_text) else "sequence"
    for i in range(len(exec_items) - 1):
        edges.append(
            IntentEdge(
                src=f"n{i}",
                dst=f"n{i + 1}",
                kind=edge_kind,  # type: ignore[arg-type]
                reason="narrative order" if edge_kind == "sequence" else "explicit depends",
            )
        )
    # attach leftover constraints to nearest / last exec
    base = len(nodes)
    for j, (cseg, chit) in enumerate(constraint_items):
        cid = f"n{base + j}"
        nodes.append(
            _node_from_hit(cid, chit, cseg, role="constraint", span_start=(base + j) * 10)
        )
        edges.append(
            IntentEdge(
                src=cid,
                dst=f"n{len(exec_items) - 1}",
                kind="constrains",
                reason="constraint attach",
            )
        )

    g = IntentGraph(nodes=nodes, edges=edges, mode="multi", root_ids=[])
    g.root_ids = recompute_root_ids(g)
    g = merge_constraints(g)
    return validate_graph(g, max_executable_nodes=max_executable_nodes)


def _plan_repair_channel(
    segment_hits: list[tuple[Segment, RuleHit]],
    *,
    max_executable_nodes: int,
) -> IntentGraph:
    texts = [s.text for s, _ in segment_hits]
    joined = "\n".join(texts)
    # Prefer hit with repair primary / highest conf stack
    repair_hit = None
    for seg, hit in segment_hits:
        if hit.primary in ("repair_issue", "repair_request"):
            repair_hit = (seg, hit)
            break
    if repair_hit is None:
        seg, hit = segment_hits[0]
        hit = RuleHit(
            "repair_issue",
            "run_repair",
            max(0.7, hit.confidence),
            slots=dict(hit.slots),
            reason="planner:repair_fold",
        )
        repair_hit = (seg, hit)

    seg0, hit0 = repair_hit
    # merge slots from all segments
    slots: dict[str, Any] = dict(hit0.slots)
    for seg, hit in segment_hits:
        for k, v in hit.slots.items():
            if k not in slots or not slots[k]:
                slots[k] = v
            elif isinstance(slots[k], list) and isinstance(v, list):
                for item in v:
                    if item not in slots[k]:
                        slots[k].append(item)
    hit0 = RuleHit(
        "repair_issue",
        "run_repair",
        hit0.confidence,
        slots=slots,
        reason=hit0.reason or "planner:repair_fold",
    )
    main = _node_from_hit("n0", hit0, Segment(index=0, text=joined), span_start=0)
    nodes = [main]
    edges: list[IntentEdge] = []
    mode = "single"
    idx = 1
    for seg, hit in segment_hits:
        if is_constraint_text(seg.text) or hit.reason == "rule:constraint":
            mode = "hybrid"
            cid = f"n{idx}"
            idx += 1
            nodes.append(
                _node_from_hit(cid, hit, seg, role="constraint", span_start=idx * 10)
            )
            edges.append(IntentEdge(src=cid, dst="n0", kind="constrains", reason="repair constraint"))
    g = IntentGraph(nodes=nodes, edges=edges, mode=mode, root_ids=["n0"])
    if mode == "hybrid":
        g = merge_constraints(g)
    return validate_graph(g, max_executable_nodes=max_executable_nodes)
