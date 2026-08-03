"""Rule-layer intent classification (slash / remember / repair / clarify / ask + enterprise)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from agent_runtime.features.memory.durable import _has_save_intent
from agent_runtime.intent.models import PRIMARY_ACTIONS, Channel
from agent_runtime.intent.stack_parse import extract_issue_slots, has_stack_signal

ChannelName = Channel


@dataclass
class RuleHit:
    primary: str
    action: str
    confidence: float
    slots: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    parser: str = "rule"


_SLASH = re.compile(r"^\s*/(help|cancel|quit|exit)\b", re.I)
_HELP_WORDS = re.compile(r"(?i)\b(help|帮助|怎么用|有哪些命令)\b")
_CANCEL_WORDS = re.compile(r"(?i)\b(cancel|取消|停下|停止)\b")
_REPAIR_WORDS = re.compile(
    r"(?i)(帮我修|帮忙修|修一下|修这个|修好|请修复|帮忙修复|"
    r"修复(?!计划)|fix\s*(this|the|it|bug|error|crash)|please\s+fix)"
)

# Enterprise action patterns (checked after repair/stack; more specific first).
_TEST_WORDS = re.compile(
    r"(?i)(补单测|补.{0,8}(单元)?测试|写(个|一下)?(单元)?测试|"
    r"跑\s*(一下)?\s*(相关)?\s*(pytest|测试)|"
    r"add\s+(unit\s+)?tests?|run\s+(the\s+)?tests?|coverage|提高.*覆盖率)"
)
_REFACTOR_WORDS = re.compile(
    r"(?i)(重构|抽成|抽取(成|为)?|rename\b|refactor\b|整理一下.*代码|"
    r"不改变行为)"
)
_IMPLEMENT_WORDS = re.compile(
    r"(?i)(实现一个|实现一下|加个功能|新增|添加一个|implement\b|add\s+(a\s+)?feature|"
    r"写一个.*(功能|模块|接口)|支持.*(功能|能力))"
)
_REVIEW_WORDS = re.compile(
    r"(?i)(代码审查|review\b|帮我看看有没有(问题|bug)|有没有明显|"
    r"please\s+review|查一下隐患)"
)
_DEBUG_WORDS = re.compile(
    r"(?i)(排查|定位根因|根因|先别改|investigate\b|debug\s+why|"
    r"为什么.*(变成|变成了|会是)|hangs?\b)"
)
_SEARCH_WORDS = re.compile(
    r"(?i)(搜索|搜一下|哪里(调用|用了|写了)|find\s+usages?|locate\b|"
    r"代码里哪里)"
)
_PLAN_WORDS = re.compile(
    r"(?i)(修复计划|实现方案|先.*计划|出个方案|make\s+a\s+plan|"
    r"怎么拆分|再动手)"
)
_EXPLAIN_WORDS = re.compile(
    r"(?i)(解释|说明一下|是干什么的|是做什么的|什么意思|walk\s+me\s+through|"
    r"what\s+does\b|explain\b|这段.*(意思|逻辑))"
)

_CONSTRAINT_HINT = re.compile(
    r"(?i)(只[用改动]|仅[用改]|不要|先别|只用|only\s+(use|change|edit)|"
    r"don'?t|please\s+use|scope:)"
)

# Same-sentence multi-intent / conflict detection.
_REMEMBER_LEAD = re.compile(
    r"(?i)(请记住|记住|remember(?:\s+to)?|don't\s+forget|永记|备忘)"
)
_SPLIT_JOIN = re.compile(
    r"(?:,|，|；|;|\s*然后\s*|\s*接着\s*|\s*之后\s*|"
    r"\s+and\s+then\s+|\s+then\s+|\s+also\s+|\s*并(?:且)?\s*|\s*同时\s*)"
)
_CONFLICT_FAMILIES: frozenset[frozenset[str]] = frozenset(
    {
        frozenset({"repair", "refactor"}),
        frozenset({"repair", "implement"}),
        frozenset({"refactor", "implement"}),
    }
)
_LEAD_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_REMEMBER_LEAD, "remember"),
    (_REPAIR_WORDS, "repair"),
    (_REFACTOR_WORDS, "refactor"),
    (_IMPLEMENT_WORDS, "implement"),
    (_TEST_WORDS, "test"),
    (_EXPLAIN_WORDS, "explain"),
    (_REVIEW_WORDS, "review"),
    (_DEBUG_WORDS, "debug"),
    (_SEARCH_WORDS, "search"),
    (_PLAN_WORDS, "plan"),
]


def find_intent_leads(text: str) -> list[tuple[int, str]]:
    """Return (start, family) leads sorted by position (dedupe overlapping)."""
    raw = text or ""
    found: list[tuple[int, str]] = []
    for pat, family in _LEAD_PATTERNS:
        for m in pat.finditer(raw):
            found.append((m.start(), family))
    found.sort(key=lambda x: x[0])
    # Deduplicate: keep first lead per family; skip leads within 2 chars of previous.
    out: list[tuple[int, str]] = []
    seen_fam: set[str] = set()
    last_pos = -99
    for pos, fam in found:
        if fam in seen_fam:
            continue
        if pos - last_pos < 2 and out:
            continue
        out.append((pos, fam))
        seen_fam.add(fam)
        last_pos = pos
    return out


def has_conflicting_leads(text: str) -> bool:
    """True when one utterance mixes mutually exclusive coding intents."""
    fams = {f for _, f in find_intent_leads(text)}
    for pair in _CONFLICT_FAMILIES:
        if pair <= fams:
            return True
    return False


def split_same_sentence_multi(text: str) -> list[str] | None:
    """Split one sentence with ≥2 compatible intent leads into parts.

    Returns None when no split is appropriate (single lead, conflict, or no join).
    Compatible example: remember + repair separated by 「然后」/comma.
    Conflict (repair+refactor) returns None — caller should clarify.
    """
    raw = (text or "").strip()
    if not raw or has_stack_signal(raw) or raw.startswith("```"):
        return None
    leads = find_intent_leads(raw)
    if len(leads) < 2:
        return None
    fams = {f for _, f in leads}
    for pair in _CONFLICT_FAMILIES:
        if pair <= fams:
            return None  # conflict — do not invent a false multi-split

    # Prefer split at conjunction between first two distinct leads.
    p0, _ = leads[0]
    p1, _ = leads[1]
    if p1 <= p0:
        return None
    mid = raw[p0:p1]
    m = None
    for m in _SPLIT_JOIN.finditer(mid):
        pass
    if m is None:
        # Soft split: cut at second lead start if there is punctuation-ish gap
        gap = raw[p0:p1]
        if not re.search(r"[,，;；\s]", gap):
            return None
        cut = p1
    else:
        cut = p0 + m.end()

    left = raw[:cut].strip(" ，,;；")
    right = raw[cut:].strip(" ，,;；")
    if not left or not right or len(left) < 2 or len(right) < 2:
        return None
    # Each side should retain its lead
    if len(find_intent_leads(left)) < 1 or len(find_intent_leads(right)) < 1:
        return None
    return [left, right]


def _hit(primary: str, confidence: float, reason: str, slots: dict | None = None) -> RuleHit:
    return RuleHit(
        primary,
        PRIMARY_ACTIONS[primary],
        confidence,
        slots=slots or {},
        reason=reason,
    )


def _slots_from_issue_text(text: str) -> dict[str, Any]:
    """Delegate to stack-first extractor (traceback region preferred)."""
    return extract_issue_slots(text)


def is_constraint_text(text: str) -> bool:
    """Heuristic: constraint phrase without independent verb intent."""
    if _has_save_intent(text) or _REPAIR_WORDS.search(text) or _SLASH.match(text):
        return False
    if has_stack_signal(text):
        return False
    if any(
        p.search(text)
        for p in (
            _TEST_WORDS,
            _REFACTOR_WORDS,
            _IMPLEMENT_WORDS,
            _REVIEW_WORDS,
            _DEBUG_WORDS,
            _SEARCH_WORDS,
            _PLAN_WORDS,
            _EXPLAIN_WORDS,
        )
    ):
        return False
    if _CONSTRAINT_HINT.search(text):
        return True
    if re.fullmatch(r"(?i)\s*[\w./\\-]+\.(?:py|js|ts)\s*", text.strip()):
        return True
    if re.match(r"(?i)^\s*(language|lang)\s*[:=]", text):
        return True
    return False


def _classify_enterprise(raw: str) -> RuleHit | None:
    """Enterprise coding intents; order = specificity."""
    if _TEST_WORDS.search(raw):
        return _hit("test", 0.88, "rule:test")
    if _REFACTOR_WORDS.search(raw):
        return _hit("refactor", 0.88, "rule:refactor")
    if _IMPLEMENT_WORDS.search(raw):
        return _hit("implement", 0.88, "rule:implement")
    if _REVIEW_WORDS.search(raw):
        return _hit("review", 0.86, "rule:review")
    if _DEBUG_WORDS.search(raw):
        return _hit("debug", 0.86, "rule:debug")
    if _SEARCH_WORDS.search(raw):
        return _hit("search", 0.86, "rule:search")
    if _PLAN_WORDS.search(raw):
        return _hit("plan", 0.86, "rule:plan")
    if _EXPLAIN_WORDS.search(raw):
        return _hit("explain", 0.85, "rule:explain")
    return None


def classify_rules(text: str, *, channel: ChannelName = "repl") -> RuleHit | None:
    """Classify a single segment/text with deterministic rules. Never returns None for non-empty."""
    raw = (text or "").strip()
    if not raw:
        return _hit("clarify", 0.9, "empty")

    m = _SLASH.match(raw)
    if m:
        cmd = m.group(1).lower()
        if cmd in ("help",):
            return _hit("help", 0.99, "slash:help")
        return _hit("cancel", 0.99, f"slash:{cmd}")

    stackish = has_stack_signal(raw)
    if channel == "repair" or (stackish and not _has_save_intent(raw)):
        if channel == "repair" or (stackish and not _REPAIR_WORDS.search(raw) and len(raw) > 40):
            slots = _slots_from_issue_text(raw)
            primary = "repair_issue"
            conf = 0.92 if stackish else 0.75
            return RuleHit(
                primary,
                PRIMARY_ACTIONS[primary],
                conf,
                slots=slots,
                reason="rule:repair_channel" if channel == "repair" else "rule:stack",
            )

    if _has_save_intent(raw):
        return RuleHit(
            "remember",
            "promote_memory",
            0.95,
            slots={"note": raw},
            reason="rule:save_intent",
        )

    if _HELP_WORDS.search(raw) and len(raw) < 40:
        return _hit("help", 0.9, "rule:help")

    if _CANCEL_WORDS.search(raw) and len(raw) < 30:
        return _hit("cancel", 0.9, "rule:cancel")

    # Explicit fix request wins over refactor/implement/debug wording.
    if _REPAIR_WORDS.search(raw) or (stackish and channel == "repl"):
        slots = _slots_from_issue_text(raw)
        primary = "repair_request"
        if stackish and not _REPAIR_WORDS.search(raw):
            primary = "repair_issue"
        return RuleHit(
            primary,
            PRIMARY_ACTIONS[primary],
            0.9,
            slots=slots,
            reason="rule:repair_request",
        )

    ent = _classify_enterprise(raw)
    if ent is not None:
        return ent

    if is_constraint_text(raw):
        slots = _slots_from_issue_text(raw)
        if _CONSTRAINT_HINT.search(raw):
            slots.setdefault("note", raw)
        return RuleHit(
            "ask",
            "ask",
            0.4,
            slots=slots,
            reason="rule:constraint",
        )

    if len(raw) < 2:
        return _hit("clarify", 0.85, "rule:too_short")

    if re.fullmatch(r"[\s?？.。!！…]+", raw) or raw in ("那个", "嗯嗯", "啊"):
        return _hit("clarify", 0.8, "rule:unclear")

    return _hit("ask", 0.7, "rule:default_ask")
