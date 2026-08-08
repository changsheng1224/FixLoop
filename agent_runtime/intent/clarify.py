"""Clarify / fallback policies for low-confidence and ambiguous intents.

Policy (product): on low confidence → clarify only, do **not** auto-execute.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from agent_runtime.intent.graph import clarify_graph, validate_graph
from agent_runtime.intent.models import IntentNode, IntentResult, RouteContext
from agent_runtime.intent.policy import required_confidence
from agent_runtime.intent.rules import RuleHit

# Vague / underspecified repair or deixis without an object.
_DEIXIS = re.compile(
    r"(?i)^("
    r"这个|那个|它|这个呢|那个呢|修一下|fix\s*it|fix\s*this|帮我看看|"
    r"怎么办|怎么弄|啥意思"
    r")[\s?？!！.。]*$"
)
_AMBIGUOUS_MARKERS = re.compile(r"(?i)(这个|那个|它|somehow|随便|不知道|可能是|好像|咋办|又报错了)")
_VAGUE_BROKEN = re.compile(
    r"(?i)^[\w\u4e00-\u9fff]{1,12}(坏了|挂了|不行了|broken|is down)[\s!！.。]*$"
)
_VAGUE_ERROR = re.compile(r"(?i)(又报错了|咋办|又挂了|error\s+again)")

# Prometheus / observability reason labels (stable set).
CLARIFY_REASON_LABELS = frozenset(
    {
        "low_conf",
        "no_hit",
        "ambiguous",
        "conflict",
        "empty",
        "below_tau_exec",
        "unresolved_anaphora",
    }
)

_REASON_NORMALIZE = {
    "low_confidence": "low_conf",
    "below_tau_clarify": "low_conf",
    "weak signal": "low_conf",
    "no_intent_hit": "no_hit",
    "ambiguous": "ambiguous",
    "conflict": "conflict",
    "empty": "empty",
    "empty input": "empty",
    "below_tau_exec": "below_tau_exec",
    "unresolved_anaphora": "unresolved_anaphora",
}


@dataclass
class ClarifyCandidate:
    primary: str
    confidence: float
    reason: str = ""


@dataclass
class ClarifyPayload:
    reason: str  # low_conf | no_hit | ambiguous | conflict | empty | below_tau_exec
    question: str
    candidates: list[ClarifyCandidate] = field(default_factory=list)
    original_text: str = ""
    allow_execute: bool = False  # always False under current product policy

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_QUESTIONS = {
    "low_conf": (
        "我还不太确定您的意图（置信度偏低）。"
        "请补充：是要修复报错、解释代码、重构、写测试，还是其他？"
    ),
    "no_hit": (
        "没有识别到明确的编码意图。"
        "可以试试：「帮我修这个错误」「解释这段代码」「重构 xxx」「补单测」。"
    ),
    "ambiguous": (
        "您的问题比较模糊（缺少对象或指代不清）。请指明文件/函数/报错栈，或具体想做的动作。"
    ),
    "conflict": ("同一段话里检测到互相冲突的意图。请拆成两句，或明确优先做哪一件。"),
    "empty": "请输入具体问题或粘贴报错堆栈。",
    "below_tau_exec": (
        "意图置信度未达到自动执行门槛，我先跟您确认："
        "请补充细节，或明确说出想要的动作（修 bug / 解释 / 重构 / 测试…）。"
    ),
    "unresolved_anaphora": (
        "我不确定「这个/刚才那个」指的是什么。"
        "请再说一次完整对象（文件、函数或报错），或先描述具体问题。"
    ),
}


def normalize_clarify_reason(reason: str) -> str:
    key = (reason or "").strip()
    if key in CLARIFY_REASON_LABELS:
        return key
    return _REASON_NORMALIZE.get(key, _REASON_NORMALIZE.get(key.lower(), "ambiguous"))


def is_ambiguous_utterance(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return True
    if _DEIXIS.match(raw):
        return True
    if _VAGUE_BROKEN.match(raw):
        return True
    if len(raw) < 8 and _AMBIGUOUS_MARKERS.search(raw):
        return True
    if (
        len(raw) < 24
        and _VAGUE_ERROR.search(raw)
        and not re.search(r"(?i)(帮我修|fix|文件|函数|\.py|\.js|\.ts)", raw)
    ):
        return True
    # "修一下" style without file/stack/error token
    if re.search(r"(?i)^(修一下|fix\s+it|帮我看看)\b", raw) and not re.search(
        r"(?i)(\.py|traceback|error|失败|bug|函数|文件)", raw
    ):
        return True
    return False


def is_no_intent_hit(hit: RuleHit | None, *, fused_conf: float, tau_node: float) -> bool:
    if hit is None:
        return True
    if hit.reason in ("rule:unclear", "rule:too_short", "empty"):
        return True
    # Soft default_ask only counts as no-hit when confidence is below node threshold
    # (not tau_node+ε — float(0.7) < 0.55+0.15 is True due to IEEE noise).
    if hit.reason == "rule:default_ask" and fused_conf < tau_node:
        return True
    return False


def build_clarify_payload(
    reason: str,
    *,
    text: str,
    candidates: list[ClarifyCandidate] | None = None,
) -> ClarifyPayload:
    label = normalize_clarify_reason(reason)
    q = _QUESTIONS.get(label, _QUESTIONS["ambiguous"])
    return ClarifyPayload(
        reason=label,
        question=q,
        candidates=list(candidates or []),
        original_text=text,
        allow_execute=False,
    )


def candidates_from_hits(
    hits: list[tuple[Any, RuleHit]],
    *,
    limit: int = 3,
) -> list[ClarifyCandidate]:
    ranked = sorted(hits, key=lambda x: x[1].confidence, reverse=True)
    out: list[ClarifyCandidate] = []
    seen: set[str] = set()
    for _, hit in ranked:
        if hit.primary in seen or hit.primary == "clarify":
            continue
        seen.add(hit.primary)
        out.append(
            ClarifyCandidate(
                primary=hit.primary,
                confidence=round(hit.confidence, 4),
                reason=hit.reason,
            )
        )
        if len(out) >= limit:
            break
    return out


def should_clarify(
    result: IntentResult,
    ctx: RouteContext,
    *,
    text: str,
    hits: list[tuple[Any, RuleHit]] | None = None,
    segment_breakdowns: list[dict[str, float]] | None = None,
    force_conflict: bool = False,
) -> ClarifyPayload | None:
    """Decide clarify-only (never auto-execute on low confidence)."""
    if ctx.channel == "repair":
        if not (text or "").strip():
            return build_clarify_payload("empty", text=text or "")
        return None

    if result.primary in ("help", "cancel") or result.action in ("help", "noop_cancel"):
        return None

    hits = hits or []
    cands = candidates_from_hits(hits)
    conf = float(result.confidence)
    min_node = float(result.confidence_breakdown.get("min_node_conf", conf))
    breakdown_conflict = bool(
        force_conflict
        or result.confidence_breakdown.get("conflict")
        or any(b.get("conflict") for b in (segment_breakdowns or []))
    )

    if not (text or "").strip():
        return build_clarify_payload("empty", text=text or "", candidates=cands)

    if force_conflict or (breakdown_conflict and result.graph.mode == "single" and len(cands) >= 2):
        return build_clarify_payload("conflict", text=text, candidates=cands)

    if result.primary == "clarify" or result.action == "clarify":
        reason = str(result.raw_signals.get("clarify_reason") or result.reason or "ambiguous")
        return build_clarify_payload(reason, text=text, candidates=cands)

    if is_ambiguous_utterance(text):
        return build_clarify_payload("ambiguous", text=text, candidates=cands)

    if hits:
        best = max(hits, key=lambda x: x[1].confidence)[1]
        if is_no_intent_hit(best, fused_conf=conf, tau_node=ctx.tau_node):
            return build_clarify_payload("no_hit", text=text, candidates=cands)
        # Soft default_ask on vague/ultra-short text → clarify rather than auto-ask
        if best.reason == "rule:default_ask" and (
            is_ambiguous_utterance(text) or len(text.strip()) < 4
        ):
            return build_clarify_payload("no_hit", text=text, candidates=cands)

    # Low confidence: clarify only (do not execute)
    if conf < ctx.tau_clarify or min_node < ctx.tau_clarify:
        return build_clarify_payload("low_conf", text=text, candidates=cands)

    exec_threshold = required_confidence(result, ctx)
    if conf < exec_threshold or min_node < exec_threshold:
        return build_clarify_payload("below_tau_exec", text=text, candidates=cands)

    return None


def apply_clarify(
    result: IntentResult,
    payload: ClarifyPayload,
) -> IntentResult:
    """Replace result with clarify graph; attach payload; block execution."""
    g = validate_graph(clarify_graph(payload.reason, confidence=min(result.confidence, 0.4)))
    node = (
        g.nodes[0]
        if g.nodes
        else IntentNode(id="n0", primary="clarify", action="clarify", role="clarify")
    )
    signals = dict(result.raw_signals or {})
    signals["clarify_reason"] = payload.reason
    signals["clarify"] = payload.to_dict()
    signals["allow_execute"] = False
    signals["fallback"] = {"policy": "clarify_only", "executed": False}
    if "intents" in (result.raw_signals or {}):
        signals["prior_intents"] = result.raw_signals["intents"]
    signals.setdefault("mode", "single")
    signals.setdefault("split_strategy", result.raw_signals.get("split_strategy", "clarify"))

    out = IntentResult(
        primary="clarify",
        action="clarify",
        confidence=node.confidence,
        parser=node.parser,
        graph=g,
        slots={"clarify_question": payload.question, "note": payload.reason},
        reason=payload.reason,
        confidence_breakdown=dict(result.confidence_breakdown),
        raw_signals=signals,
    )
    out.confidence_breakdown["clarify_forced"] = 1.0
    return out
