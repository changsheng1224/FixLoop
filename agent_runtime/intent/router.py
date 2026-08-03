"""IntentRouter: segment → rule+embed fuse → planner → optional LLM → IntentResult."""

from __future__ import annotations

import time
from typing import Any

from agent_runtime.intent.clarify import apply_clarify, should_clarify
from agent_runtime.intent.confidence import apply_breakdown_to_result, fuse_confidence
from agent_runtime.intent.embed_index import EmbedIndex, EmbedMatch
from agent_runtime.intent.graph import merge_constraints, recompute_root_ids, validate_graph
from agent_runtime.intent.llm_fallback import maybe_refine, maybe_refine_graph
from agent_runtime.intent.models import (
    PRIMARY_ACTIONS,
    IntentGraph,
    IntentNode,
    IntentResult,
    RouteContext,
    Segment,
)
from agent_runtime.intent.observability import record_intent_route
from agent_runtime.intent.planner import plan
from agent_runtime.intent.rules import RuleHit, classify_rules, has_conflicting_leads
from agent_runtime.intent.segmenter import segment


def _fuse(hit: RuleHit, emb: EmbedMatch | None) -> tuple[RuleHit, dict[str, float]]:
    """Strong rule wins; agreement boosts; conflict recorded in slots._conflict."""
    emb_primary = emb.primary if emb else None
    emb_score = emb.score if emb else None
    emb_margin = emb.margin if emb else None
    fused_conf, breakdown = fuse_confidence(
        hit,
        embed_primary=emb_primary,
        embed_score=emb_score,
        embed_margin=emb_margin,
    )

    if emb is None:
        return hit, breakdown

    if hit.confidence >= 0.9:
        if emb.primary != hit.primary:
            slots = dict(hit.slots)
            slots["_embed_conflict"] = {"embed": emb.primary, "score": emb.score}
            return (
                RuleHit(
                    hit.primary,
                    hit.action,
                    fused_conf,
                    slots=slots,
                    reason=hit.reason,
                    parser=hit.parser,
                ),
                breakdown,
            )
        return (
            RuleHit(
                hit.primary,
                hit.action,
                fused_conf,
                slots=dict(hit.slots),
                reason=hit.reason,
                parser="rule+embed",
            ),
            breakdown,
        )

    if emb.score >= 0.55 and emb.margin >= 0.08 and emb.primary != hit.primary:
        return (
            RuleHit(
                emb.primary,
                PRIMARY_ACTIONS.get(emb.primary, emb.primary),
                fused_conf,
                slots=dict(hit.slots),
                reason="embed",
                parser="embed",
            ),
            breakdown,
        )
    if emb.primary == hit.primary:
        return (
            RuleHit(
                hit.primary,
                hit.action,
                fused_conf,
                slots=dict(hit.slots),
                reason=hit.reason,
                parser="rule+embed",
            ),
            breakdown,
        )
    slots = dict(hit.slots)
    slots["_embed_conflict"] = {"embed": emb.primary, "score": emb.score}
    return (
        RuleHit(
            hit.primary,
            hit.action,
            fused_conf,
            slots=slots,
            reason=hit.reason + "+conflict",
            parser=hit.parser,
        ),
        breakdown,
    )


def _result_from_graph(graph: IntentGraph, *, raw_signals: dict[str, Any] | None = None) -> IntentResult:
    graph = validate_graph(graph)
    execs = [n for n in graph.nodes if n.role == "executable"]
    # clarify-only
    if not execs:
        node = graph.nodes[0] if graph.nodes else IntentNode(
            id="n0", primary="clarify", action="clarify", role="clarify"
        )
        return IntentResult(
            primary=node.primary,
            action=node.action,
            confidence=node.confidence,
            parser=node.parser,
            graph=graph,
            slots=dict(node.slots),
            reason=node.text or "clarify",
            raw_signals=raw_signals or {},
        )

    roots = graph.root_ids or recompute_root_ids(graph)
    by_id = graph.node_map()
    primary_node = by_id.get(roots[0], execs[0]) if roots else execs[0]
    # highest priority among roots if multiple
    if len(roots) > 1:
        primary_node = max(
            (by_id[r] for r in roots if r in by_id),
            key=lambda n: (n.priority, -n.span.get("start", 0), n.confidence),
        )

    secondary = [n.primary for n in execs if n.id != primary_node.id]
    conf = sum(n.confidence for n in execs) / len(execs)
    parsers = sorted({n.parser for n in graph.nodes if n.parser})
    action = primary_node.action
    if graph.mode == "multi" and len(execs) >= 2:
        action = "run_graph"

    signals = dict(raw_signals or {})
    signals.setdefault("mode", graph.mode)
    signals.setdefault("segment_count", len(graph.nodes))

    return IntentResult(
        primary=primary_node.primary,
        action=action,
        confidence=conf,
        parser="+".join(parsers) if parsers else "rule",
        graph=graph,
        secondary=secondary,
        slots=dict(primary_node.slots),
        reason=primary_node.parser,
        raw_signals=signals,
    )


def _split_strategy(segs: list[Segment], graph: IntentGraph) -> str:
    if graph.mode == "multi":
        if any(s.cue == "sequential" for s in segs) and len(segs) >= 2:
            # same-sentence expand sets sequential cue on trailing parts
            if len(segs) >= 2 and all(
                not (s.text.count("。") or s.text.count(".")) for s in segs[:2]
            ):
                return "same_sentence_multi"
            return "narrative_multi"
        return "multi"
    if graph.mode == "hybrid":
        return "hybrid_constraint"
    return "single"


class IntentRouter:
    def __init__(self, *, embed_index: EmbedIndex | None = None) -> None:
        self.embed_index = embed_index or EmbedIndex(embed_fn=None)

    def route(self, text: str, context: RouteContext | None = None) -> IntentResult:
        ctx = context or RouteContext()
        original = (text or "").strip()
        signals: dict[str, Any] = {}
        t0 = time.perf_counter()
        embed_skipped = self.embed_index.embed_fn is None and ctx.embed_fn is None
        llm_outcome: str | None = None
        segment_breakdowns: list[dict[str, float]] = []
        hits: list[tuple[Segment, RuleHit]] = []
        anaphora_outcome: str | None = None

        # History-first anaphora / clarify-resume rewrite
        from agent_runtime.intent.dialogue import resolve_utterance

        resolved = resolve_utterance(
            original,
            history=ctx.history,
            projection=ctx.dialogue,
        )
        raw = resolved.text.strip()
        anaphora_outcome = resolved.outcome
        if resolved.outcome != "passthrough":
            signals["anaphora"] = {
                "outcome": resolved.outcome,
                "reason": resolved.reason,
                "original": original,
                "resolved_text": raw[:500],
                "used_history": resolved.used_history,
                "used_projection": resolved.used_projection,
            }
            signals["resolved_from"] = resolved.reason

        def _finish(result: IntentResult) -> IntentResult:
            if anaphora_outcome and anaphora_outcome != "passthrough":
                sig = dict(result.raw_signals or {})
                sig.setdefault("anaphora", signals.get("anaphora"))
                sig.setdefault("resolved_from", signals.get("resolved_from"))
                result.raw_signals = sig
            latency_ms = (time.perf_counter() - t0) * 1000.0
            self._emit(ctx, result)
            try:
                record_intent_route(
                    result,
                    ctx,
                    latency_ms=latency_ms,
                    embed_skipped=embed_skipped,
                    llm_outcome=llm_outcome,
                )
            except Exception:
                pass
            return result

        if resolved.outcome == "unresolved":
            from agent_runtime.intent.clarify import build_clarify_payload
            from agent_runtime.intent.candidates import record_route_candidates

            empty = _result_from_graph(
                plan([], channel=ctx.channel, max_executable_nodes=ctx.max_executable_nodes),
                raw_signals=dict(signals),
            )
            payload = build_clarify_payload(
                "unresolved_anaphora",
                text=original,
            )
            result = apply_clarify(empty, payload)
            try:
                record_route_candidates(
                    result,
                    text=original,
                    root=ctx.candidate_root,
                    hits=[],
                    channel=ctx.channel,
                )
            except Exception:
                pass
            return _finish(result)

        if not raw:
            g = plan([], channel=ctx.channel, max_executable_nodes=ctx.max_executable_nodes)
            result = _result_from_graph(g, raw_signals={"mode": "single", "segment_count": 0})
            result = apply_breakdown_to_result(result, split_strategy="empty")
            payload = should_clarify(result, ctx, text=raw, hits=[])
            if payload:
                result = apply_clarify(result, payload)
            try:
                from agent_runtime.intent.candidates import record_route_candidates

                record_route_candidates(
                    result,
                    text=original,
                    root=ctx.candidate_root,
                    hits=[],
                    channel=ctx.channel,
                )
            except Exception:
                pass
            return _finish(result)

        # slash short-circuit on full text
        slash_hit = classify_rules(raw, channel=ctx.channel)
        if slash_hit and slash_hit.reason.startswith("slash:"):
            seg = Segment(index=0, text=raw)
            g = plan(
                [(seg, slash_hit)],
                channel=ctx.channel,
                max_executable_nodes=ctx.max_executable_nodes,
                tau_node=ctx.tau_node,
            )
            result = _result_from_graph(g, raw_signals={"mode": "single", "segment_count": 1})
            result = apply_breakdown_to_result(result, split_strategy="slash")
            return _finish(result)

        segs = segment(raw)
        if not segs:
            segs = [Segment(index=0, text=raw)]
        signals["segment_count"] = len(segs)

        force_conflict = False
        if len(segs) == 1 and has_conflicting_leads(segs[0].text):
            force_conflict = True
            signals["conflict"] = True

        # optional full-text embed signal
        embed_idx = self.embed_index
        if ctx.embed_fn is not None:
            embed_idx = EmbedIndex(embed_idx.prototypes, embed_fn=ctx.embed_fn)
            embed_skipped = False

        for seg in segs:
            rule = classify_rules(seg.text, channel=ctx.channel)
            assert rule is not None
            emb = embed_idx.match(seg.text)
            fused, breakdown = _fuse(rule, emb)
            segment_breakdowns.append(breakdown)
            hits.append((seg, fused))

        graph_before = plan(
            hits,
            channel=ctx.channel,
            max_executable_nodes=ctx.max_executable_nodes,
            tau_node=ctx.tau_node,
        )

        # weak graph → LLM once (graph + Top-k candidates side-channel)
        llm_candidates_payload: list[dict] = []
        llm_need_clarify: bool | None = None
        if ctx.light_client is not None:
            refine = maybe_refine(
                graph_before,
                raw,
                ctx.light_client,
                tau_llm=ctx.tau_llm,
                segments=[s.text for s in segs],
            )
            graph = refine.graph
            llm_candidates_payload = [c.to_dict() for c in refine.candidates]
            llm_need_clarify = refine.need_clarify
            if refine.applied and any(n.parser == "llm" for n in graph.nodes):
                llm_outcome = "applied"
            else:
                llm_outcome = "skipped"
        else:
            graph = maybe_refine_graph(
                graph_before,
                raw,
                None,
                tau_llm=ctx.tau_llm,
                segments=[s.text for s in segs],
            )
            llm_outcome = None

        if llm_candidates_payload:
            signals["llm_candidates"] = llm_candidates_payload
        if llm_need_clarify is not None:
            signals["llm_need_clarify"] = llm_need_clarify

        if graph.mode == "hybrid":
            graph = merge_constraints(graph)

        signals["mode"] = graph.mode
        if graph.mode == "hybrid":
            signals["disambiguation"] = "constraint_attach"
        elif graph.mode == "multi":
            signals["disambiguation"] = "multi_executable"
        else:
            signals["disambiguation"] = "single"

        strategy = _split_strategy(segs, graph)
        signals["split_strategy"] = strategy

        result = _result_from_graph(graph, raw_signals=signals)
        result = apply_breakdown_to_result(
            result,
            segment_breakdowns=segment_breakdowns,
            split_strategy=strategy,
        )

        # Overlay stack-first slots from the *full* user text so fenced paste
        # noise in partial segments cannot pollute suspect_files.
        from agent_runtime.intent.stack_parse import extract_issue_slots, has_stack_signal

        if has_stack_signal(raw):
            full_slots = extract_issue_slots(raw)
            if full_slots:
                merged_slots = dict(result.slots)
                for key in (
                    "suspect_files",
                    "issue_type",
                    "frames",
                    "top_frame",
                    "exception_type",
                    "exception_msg",
                    "stack_line",
                    "stack_span",
                ):
                    if key in full_slots:
                        merged_slots[key] = full_slots[key]
                result.slots = merged_slots
                for n in result.graph.nodes:
                    if n.role == "executable" and n.primary.startswith("repair"):
                        n.slots.update(
                            {
                                k: full_slots[k]
                                for k in (
                                    "suspect_files",
                                    "issue_type",
                                    "frames",
                                    "top_frame",
                                )
                                if k in full_slots
                            }
                        )

        # Clarify-only policy (low conf / ambiguous / conflict / no hit)
        payload = should_clarify(
            result,
            ctx,
            text=raw,
            hits=hits,
            segment_breakdowns=segment_breakdowns,
            force_conflict=force_conflict,
        )
        if payload:
            result = apply_clarify(result, payload)

        # Candidate intent discovery (topk / clarify residual / conflict / llm)
        try:
            from agent_runtime.intent.candidates import (
                events_from_llm_candidates,
                record_route_candidates,
            )

            record_route_candidates(
                result,
                text=original or raw,
                root=ctx.candidate_root,
                hits=hits,
                channel=ctx.channel,
            )
            llm_cands = (result.raw_signals or {}).get("llm_candidates") or signals.get(
                "llm_candidates"
            )
            if llm_cands:
                # Ensure signals survived onto result
                sig = dict(result.raw_signals or {})
                sig["llm_candidates"] = llm_cands
                if "llm_need_clarify" in signals:
                    sig["llm_need_clarify"] = signals["llm_need_clarify"]
                result.raw_signals = sig
                llm_events = events_from_llm_candidates(
                    text=original or raw,
                    predicted=result.primary,
                    llm_candidates=llm_cands,
                    channel=ctx.channel,
                    need_clarify=signals.get("llm_need_clarify"),
                )
                if llm_events:
                    from agent_runtime.intent.candidates import CandidateStore

                    if ctx.candidate_root:
                        CandidateStore(ctx.candidate_root).append(llm_events)
                    sig = dict(result.raw_signals or {})
                    prev = list(sig.get("candidate_events") or [])
                    prev.extend(e.to_dict() for e in llm_events)
                    sig["candidate_events"] = prev
                    keys = set(sig.get("candidate_keys") or [])
                    from agent_runtime.intent.candidates import _card_key

                    keys.update(_card_key(e) for e in llm_events)
                    sig["candidate_keys"] = sorted(keys)
                    result.raw_signals = sig
        except Exception:
            pass

        return _finish(result)

    @staticmethod
    def _emit(ctx: RouteContext, result: IntentResult) -> None:
        if ctx.emit is None:
            return
        g = result.graph
        clarify = (result.raw_signals or {}).get("clarify") or {}
        payload = {
            "mode": g.mode,
            "primary": result.primary,
            "action": result.action,
            "confidence": result.confidence,
            "parser": result.parser,
            "confidence_breakdown": dict(result.confidence_breakdown or {}),
            "intents": (result.raw_signals or {}).get("intents", []),
            "split_strategy": (result.raw_signals or {}).get("split_strategy"),
            "clarify_reason": (result.raw_signals or {}).get("clarify_reason"),
            "allow_execute": (result.raw_signals or {}).get("allow_execute", True),
            "clarify_question": clarify.get("question")
            or (result.slots or {}).get("clarify_question"),
            "anaphora": (result.raw_signals or {}).get("anaphora"),
            "resolved_from": (result.raw_signals or {}).get("resolved_from"),
            "llm_candidates": (result.raw_signals or {}).get("llm_candidates"),
            "nodes": [
                {
                    "id": n.id,
                    "primary": n.primary,
                    "role": n.role,
                    "confidence": n.confidence,
                }
                for n in g.nodes
            ],
            "edges": [
                {"src": e.src, "dst": e.dst, "kind": e.kind} for e in g.edges
            ],
        }
        try:
            ctx.emit("intent_routed", payload)
        except TypeError:
            ctx.emit("intent", "intent_routed", payload)
