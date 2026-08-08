"""Graph-level LLM refine for ambiguous multi-intent plans.

When invoked, the light client is asked for a structured graph **and**
closed-set Top-k candidates (discovery side-channel). Candidates never
auto-register into taxonomy.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from agent_runtime.intent.graph import clarify_graph, recompute_root_ids, validate_graph
from agent_runtime.intent.llm_runtime import IntentLlmRuntime
from agent_runtime.intent.models import (
    PRIMARY_ACTIONS,
    IntentEdge,
    IntentGraph,
    IntentNode,
)

_ALLOWED_KIND = frozenset({"sequence", "depends_on", "constrains"})
_ALLOWED_ROLE = frozenset({"executable", "constraint", "clarify"})

INTENT_LLM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["mode", "nodes", "edges", "need_clarify"],
    "properties": {
        "mode": {"type": "string", "enum": ["single", "multi", "hybrid"]},
        "nodes": {"type": "array", "maxItems": 4},
        "edges": {"type": "array", "maxItems": 12},
        "candidates": {"type": "array", "maxItems": 5},
        "need_clarify": {"type": "boolean"},
    },
}


@dataclass
class LlmCandidate:
    """One LLM-nominated intent (prefer closed-set ``label``)."""

    label: str
    confidence: float = 0.0
    merge_into: str | None = None  # existing primary if label is novel
    is_new: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "confidence": round(float(self.confidence), 4),
            "merge_into": self.merge_into,
            "is_new": self.is_new,
        }


@dataclass
class LlmRefineResult:
    graph: IntentGraph
    candidates: list[LlmCandidate] = field(default_factory=list)
    need_clarify: bool | None = None
    applied: bool = False  # True when LLM JSON replaced the graph
    raw_parsed: dict[str, Any] | None = None
    fallback_reason: str = ""
    attempts: int = 0
    latency_ms: float = 0.0
    schema_errors: list[str] = field(default_factory=list)


def _extract_json(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


def _graph_confidence(graph: IntentGraph) -> float:
    execs = graph.executable_nodes()
    if not execs:
        return 0.0
    return sum(n.confidence for n in execs) / len(execs)


def _parse_candidates(data: dict[str, Any]) -> list[LlmCandidate]:
    raw = data.get("candidates")
    if not isinstance(raw, list):
        return []
    out: list[LlmCandidate] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("primary") or "").strip()
        if not label or label in seen:
            continue
        seen.add(label)
        merge = item.get("merge_into")
        if merge in ("", "null", None):
            merge = None
        else:
            merge = str(merge)
            if merge not in PRIMARY_ACTIONS:
                merge = None
        is_new = label not in PRIMARY_ACTIONS
        # Closed-set preferred; novel labels kept only as nomination with optional merge
        if is_new and merge is None and not item.get("allow_new"):
            # still keep as discovery nomination
            pass
        try:
            conf = float(item.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        out.append(
            LlmCandidate(
                label=label,
                confidence=max(0.0, min(1.0, conf)),
                merge_into=merge,
                is_new=is_new,
            )
        )
        if len(out) >= 5:
            break
    return out


def maybe_refine_graph(
    graph: IntentGraph,
    text: str,
    client: Any | None,
    *,
    tau_llm: float = 0.55,
    segments: list[str] | None = None,
    force: bool = False,
) -> IntentGraph:
    """Backward-compatible wrapper — returns only the (possibly refined) graph."""
    return maybe_refine(graph, text, client, tau_llm=tau_llm, segments=segments, force=force).graph


def maybe_refine(
    graph: IntentGraph,
    text: str,
    client: Any | None,
    *,
    tau_llm: float = 0.55,
    segments: list[str] | None = None,
    force: bool = False,
    runtime: IntentLlmRuntime | None = None,
    cancel_token=None,
    deadline=None,
    budget=None,
) -> LlmRefineResult:
    """Ask light_client for graph correction **and** Top-k candidates.

    Skips when no client, confidence already high (unless force), or parse fails.
    Candidates are a side-channel for discovery / clarify UI — not taxonomy writes.
    """
    if client is None:
        return LlmRefineResult(graph=graph, applied=False)

    conf = _graph_confidence(graph)
    ambiguous = graph.mode not in ("single", "multi", "hybrid") or conf < tau_llm
    if not force and not ambiguous and conf >= tau_llm:
        return LlmRefineResult(graph=graph, applied=False)

    closed = ", ".join(sorted(PRIMARY_ACTIONS.keys()))
    seg_blob = "\n".join(f"[{i}] {s}" for i, s in enumerate(segments or [text]))
    prompt = (
        "Classify the user input into an intent graph JSON with keys:\n"
        "  mode (single|multi|hybrid),\n"
        "  nodes[{id,primary,role,segment_index}],\n"
        "  edges[{src,dst,kind}],\n"
        "  candidates[{label,confidence,merge_into}] — top 3 alternatives,\n"
        "  need_clarify (bool).\n"
        f"primary and candidates[].label MUST prefer this closed set: {closed}.\n"
        "If nothing fits, set candidates[].label to a short snake_case proposal, "
        "merge_into to the closest closed label (or null), and allow_new=true.\n"
        "role: executable|constraint|clarify. kind: sequence|depends_on|constrains.\n"
        f"Segments:\n{seg_blob}\n"
        "Reply with ONLY JSON."
    )
    runtime = runtime or IntentLlmRuntime()
    call = runtime.complete(
        client,
        prompt,
        cancel_token=cancel_token,
        deadline=deadline,
        budget=budget,
        max_new_tokens=384,
    )
    if call.status != "success":
        return LlmRefineResult(
            graph=graph,
            applied=False,
            fallback_reason=call.reason or call.status,
            attempts=call.attempts,
            latency_ms=call.latency_ms,
        )
    raw = call.content

    data = _extract_json(raw if isinstance(raw, str) else str(raw))
    if not data:
        return LlmRefineResult(
            graph=graph,
            applied=False,
            fallback_reason="invalid_json",
            attempts=call.attempts,
            latency_ms=call.latency_ms,
        )

    schema_errors = validate_llm_payload(data)
    if schema_errors:
        return LlmRefineResult(
            graph=clarify_graph("invalid llm schema"),
            applied=True,
            need_clarify=True,
            raw_parsed=data,
            fallback_reason="schema_rejected",
            attempts=call.attempts,
            latency_ms=call.latency_ms,
            schema_errors=schema_errors,
        )

    candidates = _parse_candidates(data)
    need_clarify = data.get("need_clarify")
    if not isinstance(need_clarify, bool):
        need_clarify = None

    try:
        refined = _build_from_llm(data, text, segments or [text], fallback=graph)
    except ValueError:
        return LlmRefineResult(
            graph=clarify_graph("invalid llm graph"),
            candidates=candidates,
            need_clarify=True if need_clarify is None else need_clarify,
            applied=True,
            raw_parsed=data,
            fallback_reason="invalid_graph",
            attempts=call.attempts,
            latency_ms=call.latency_ms,
        )

    return LlmRefineResult(
        graph=validate_graph(refined),
        candidates=candidates,
        need_clarify=need_clarify,
        applied=True,
        raw_parsed=data,
        attempts=call.attempts,
        latency_ms=call.latency_ms,
    )


def validate_llm_payload(data: dict[str, Any]) -> list[str]:
    """Strict, dependency-free validation for the LLM graph envelope."""
    if not isinstance(data, dict):
        return ["payload_not_object"]
    errors: list[str] = []
    allowed_top = set(INTENT_LLM_SCHEMA["properties"])
    unknown = sorted(set(data) - allowed_top)
    if unknown:
        errors.append("unknown_fields:" + ",".join(unknown))
    for key in INTENT_LLM_SCHEMA["required"]:
        if key not in data:
            errors.append(f"missing:{key}")
    if data.get("mode") not in {"single", "multi", "hybrid"}:
        errors.append("invalid_mode")
    if not isinstance(data.get("need_clarify"), bool):
        errors.append("invalid_need_clarify")
    nodes = data.get("nodes")
    edges = data.get("edges")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 4:
        errors.append("invalid_nodes")
        nodes = []
    if not isinstance(edges, list) or len(edges) > 12:
        errors.append("invalid_edges")
        edges = []
    ids: set[str] = set()
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            errors.append(f"node_{index}:not_object")
            continue
        extra = set(node) - {"id", "primary", "role", "segment_index"}
        if extra:
            errors.append(f"node_{index}:unknown_fields")
        node_id = str(node.get("id") or "")
        if not node_id or len(node_id) > 64 or node_id in ids:
            errors.append(f"node_{index}:invalid_id")
        ids.add(node_id)
        if node.get("primary") not in PRIMARY_ACTIONS:
            errors.append(f"node_{index}:invalid_primary")
        if node.get("role", "executable") not in _ALLOWED_ROLE:
            errors.append(f"node_{index}:invalid_role")
        if not isinstance(node.get("segment_index", index), int):
            errors.append(f"node_{index}:invalid_segment_index")
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            errors.append(f"edge_{index}:not_object")
            continue
        if set(edge) - {"src", "dst", "kind"}:
            errors.append(f"edge_{index}:unknown_fields")
        src, dst = str(edge.get("src") or ""), str(edge.get("dst") or "")
        if src not in ids or dst not in ids or src == dst:
            errors.append(f"edge_{index}:invalid_endpoint")
        if edge.get("kind") not in _ALLOWED_KIND:
            errors.append(f"edge_{index}:invalid_kind")
    candidates = data.get("candidates", [])
    if not isinstance(candidates, list) or len(candidates) > 5:
        errors.append("invalid_candidates")
        candidates = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            errors.append(f"candidate_{index}:not_object")
            continue
        if set(candidate) - {"label", "confidence", "merge_into", "allow_new"}:
            errors.append(f"candidate_{index}:unknown_fields")
        label = str(candidate.get("label") or candidate.get("primary") or "")
        if not label or len(label) > 64:
            errors.append(f"candidate_{index}:invalid_label")
        try:
            confidence = float(candidate.get("confidence", 0.0))
        except (TypeError, ValueError):
            errors.append(f"candidate_{index}:invalid_confidence")
        else:
            if not 0.0 <= confidence <= 1.0:
                errors.append(f"candidate_{index}:invalid_confidence")
        merge_into = candidate.get("merge_into")
        if merge_into not in (None, "", "null") and merge_into not in PRIMARY_ACTIONS:
            errors.append(f"candidate_{index}:invalid_merge_target")
    return errors


def _build_from_llm(
    data: dict[str, Any],
    text: str,
    segments: list[str],
    *,
    fallback: IntentGraph,
) -> IntentGraph:
    mode = data.get("mode", "single")
    if mode not in ("single", "multi", "hybrid"):
        raise ValueError("bad mode")
    raw_nodes = data.get("nodes") or []
    raw_edges = data.get("edges") or []
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError("no nodes")

    nodes: list[IntentNode] = []
    for i, nd in enumerate(raw_nodes):
        if not isinstance(nd, dict):
            raise ValueError("bad node")
        primary = str(nd.get("primary", ""))
        if primary not in PRIMARY_ACTIONS:
            raise ValueError("bad primary")
        role = str(nd.get("role", "executable"))
        if role not in _ALLOWED_ROLE:
            raise ValueError("bad role")
        nid = str(nd.get("id") or f"n{i}")
        si = nd.get("segment_index", i)
        try:
            si_i = int(si)
        except (TypeError, ValueError):
            si_i = i
        seg_text = segments[si_i] if 0 <= si_i < len(segments) else text
        conf = 0.7
        for old in fallback.nodes:
            if old.id == nid:
                conf = old.confidence
                break
        nodes.append(
            IntentNode(
                id=nid,
                primary=primary,
                action=PRIMARY_ACTIONS[primary],
                role=role,  # type: ignore[arg-type]
                text=seg_text,
                confidence=conf,
                parser="llm",
                segment_index=si_i,
                span={"start": si_i * 10, "end": si_i * 10 + 1},
            )
        )

    edges: list[IntentEdge] = []
    for ed in raw_edges:
        if not isinstance(ed, dict):
            raise ValueError("bad edge")
        kind = str(ed.get("kind", "sequence"))
        if kind not in _ALLOWED_KIND:
            raise ValueError("bad kind")
        edges.append(
            IntentEdge(
                src=str(ed["src"]),
                dst=str(ed["dst"]),
                kind=kind,  # type: ignore[arg-type]
                reason="llm",
            )
        )

    g = IntentGraph(nodes=nodes, edges=edges, mode=mode, root_ids=[])  # type: ignore[arg-type]
    g.root_ids = recompute_root_ids(g)
    return g
