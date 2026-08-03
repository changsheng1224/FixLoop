"""Multi-turn dialogue helpers: history-first anaphora + thin intent projection.

Source of truth for *what was said* is ``agent.session["history"]`` /
``Agent.read_history()``.  ``session["intent_dialogue"]`` only stores metadata
history lacks (pending clarify, last IntentResult summary, resolved referents).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from agent_runtime.intent.models import IntentResult

SESSION_KEY = "intent_dialogue"
MAX_REFERENTS = 3
MAX_HISTORY_SCAN = 12  # recent messages to scan for files / stacks

_ANAPHORA = re.compile(
    r"(?i)^("
    r"这个|那个|它|这个呢|那个呢|刚才那个|剛才那個|刚才的|剛才的|"
    r"同上|继续|繼續|接着|接著|再来一次|再來一次|"
    r"修一下|fix\s*it|fix\s*this|帮我看看|幫我看看"
    r")[\s?？!！.。]*$"
)
_CONTINUE = re.compile(r"(?i)^(继续|繼續|同上|接着|接著|再来一次|再來一次)[\s?？!！.。]*$")
_FIX_SHORT = re.compile(r"(?i)^(修一下|fix\s*it|fix\s*this|帮我看看|幫我看看)[\s?？!！.。]*$")
_DEIXIS_ONLY = re.compile(
    r"(?i)^(这个|那个|它|这个呢|那个呢|刚才那个|剛才那個|刚才的|剛才的)[\s?？!！.。]*$"
)
_CLARIFY_CHOICE = re.compile(
    r"(?i)^(修|修复|fix|解释|說明|说明|explain|重构|重構|refactor|"
    r"测试|測試|test|审查|review|实现|實現|implement|"
    r"排查|debug|搜索|search|计划|計畫|plan|"
    r"记住|記住|remember)[\s?？!！.。]*$"
)
_FILE_TOKEN = re.compile(
    r"\b([\w./\\-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|md|yaml|yml|toml))\b",
    re.I,
)
_ISSUE_TYPE = re.compile(
    r"\b(TypeError|AttributeError|KeyError|ValueError|ImportError|"
    r"IndexError|RuntimeError|NameError|AssertionError|TimeoutError)\b"
)


@dataclass
class Referent:
    kind: str  # file | issue_type | text | primary
    value: str
    source_turn: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Referent:
        return cls(
            kind=str(d.get("kind") or "text"),
            value=str(d.get("value") or ""),
            source_turn=d.get("source_turn"),
        )


@dataclass
class DialogueProjection:
    """Thin intent-layer overlay persisted in session[intent_dialogue]."""

    pending_clarify: dict[str, Any] | None = None
    last_primary: str | None = None
    last_action: str | None = None
    last_text: str = ""
    last_slots: dict[str, Any] = field(default_factory=dict)
    referents: list[Referent] = field(default_factory=list)
    turn_id: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pending_clarify": self.pending_clarify,
            "last_primary": self.last_primary,
            "last_action": self.last_action,
            "last_text": self.last_text,
            "last_slots": dict(self.last_slots),
            "referents": [r.to_dict() for r in self.referents],
            "turn_id": self.turn_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> DialogueProjection:
        if not d:
            return cls()
        refs = [Referent.from_dict(x) for x in (d.get("referents") or []) if isinstance(x, dict)]
        return cls(
            pending_clarify=d.get("pending_clarify"),
            last_primary=d.get("last_primary"),
            last_action=d.get("last_action"),
            last_text=str(d.get("last_text") or ""),
            last_slots=dict(d.get("last_slots") or {}),
            referents=refs[:MAX_REFERENTS],
            turn_id=int(d.get("turn_id") or 0),
        )


@dataclass
class ResolveResult:
    text: str
    outcome: str  # passthrough | resolved | clarify_resume | unresolved
    reason: str = ""
    used_history: bool = False
    used_projection: bool = False


def load_projection(session: dict[str, Any] | None) -> DialogueProjection:
    if not session:
        return DialogueProjection()
    return DialogueProjection.from_dict(session.get(SESSION_KEY))


def save_projection(session: dict[str, Any], proj: DialogueProjection) -> None:
    session[SESSION_KEY] = proj.to_dict()


def clear_projection(session: dict[str, Any]) -> None:
    session.pop(SESSION_KEY, None)


def recent_user_texts(history: list[dict[str, Any]] | None, *, limit: int = MAX_HISTORY_SCAN) -> list[str]:
    """Newest-first user message contents from agent history."""
    if not history:
        return []
    out: list[str] = []
    for item in reversed(history):
        if item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, list):
            # multimodal-ish: join text parts
            parts = []
            for p in content:
                if isinstance(p, dict) and p.get("text"):
                    parts.append(str(p["text"]))
                elif isinstance(p, str):
                    parts.append(p)
            text = "\n".join(parts).strip()
        else:
            text = str(content or "").strip()
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _push_referent(refs: list[Referent], ref: Referent) -> list[Referent]:
    # dedupe by (kind, value), keep newest first
    refs = [r for r in refs if not (r.kind == ref.kind and r.value == ref.value)]
    return ([ref] + refs)[:MAX_REFERENTS]


def extract_referents_from_text(text: str, *, turn_id: int | None = None) -> list[Referent]:
    refs: list[Referent] = []
    for m in _FILE_TOKEN.finditer(text or ""):
        refs.append(Referent(kind="file", value=m.group(1).replace("\\", "/"), source_turn=turn_id))
    for m in _ISSUE_TYPE.finditer(text or ""):
        refs.append(Referent(kind="issue_type", value=m.group(1), source_turn=turn_id))
    return refs


def referents_from_history(
    history: list[dict[str, Any]] | None,
    *,
    projection: DialogueProjection | None = None,
) -> list[Referent]:
    """Merge projection referents with files/issues mined from recent user turns."""
    refs: list[Referent] = list(projection.referents) if projection else []
    for i, text in enumerate(recent_user_texts(history)):
        for ref in extract_referents_from_text(text, turn_id=-(i + 1)):
            refs = _push_referent(refs, ref)
    return refs[:MAX_REFERENTS]


def _top_file(refs: list[Referent]) -> str | None:
    for r in refs:
        if r.kind == "file":
            return r.value
    return None


def _top_issue(refs: list[Referent]) -> str | None:
    for r in refs:
        if r.kind == "issue_type":
            return r.value
    return None


def _last_substantive_user(
    history: list[dict[str, Any]] | None,
    *,
    skip_anaphora: bool = True,
) -> str | None:
    for text in recent_user_texts(history):
        if skip_anaphora and _ANAPHORA.match(text.strip()):
            continue
        if len(text.strip()) >= 4:
            return text
    return None


def _choice_to_prefix(choice: str) -> str:
    c = choice.strip().lower()
    mapping = {
        "修": "帮我修",
        "修复": "帮我修",
        "fix": "please fix",
        "解释": "解释",
        "说明": "解释",
        "說明": "解释",
        "explain": "explain",
        "重构": "重构",
        "重構": "重构",
        "refactor": "refactor",
        "测试": "补单测",
        "測試": "补单测",
        "test": "add tests",
        "审查": "代码审查",
        "review": "please review",
        "实现": "实现",
        "實現": "实现",
        "implement": "implement",
        "排查": "排查",
        "debug": "debug",
        "搜索": "搜索",
        "search": "search",
        "计划": "出个方案",
        "計畫": "出个方案",
        "plan": "make a plan",
        "记住": "请记住",
        "記住": "请记住",
        "remember": "remember",
    }
    for k, v in mapping.items():
        if c == k or c.startswith(k):
            return v
    return choice.strip()


def resolve_utterance(
    text: str,
    *,
    history: list[dict[str, Any]] | None = None,
    projection: DialogueProjection | None = None,
) -> ResolveResult:
    """Rewrite short anaphora / clarify answers using history + thin projection."""
    raw = (text or "").strip()
    proj = projection or DialogueProjection()
    hist_users = recent_user_texts(history)
    refs = referents_from_history(history, projection=proj)

    if not raw:
        return ResolveResult(text=raw, outcome="passthrough")

    # 1) Resume after pending clarify: short choice or file name
    if proj.pending_clarify:
        original = str(proj.pending_clarify.get("original_text") or "").strip()
        if _CLARIFY_CHOICE.match(raw) and original:
            prefix = _choice_to_prefix(raw)
            merged = f"{prefix}：{original}"
            return ResolveResult(
                text=merged,
                outcome="clarify_resume",
                reason="clarify_choice",
                used_projection=True,
            )
        # bare file token as clarification answer
        fm = _FILE_TOKEN.fullmatch(raw)
        if fm and original:
            merged = f"{original}（只改 {fm.group(1)}）"
            return ResolveResult(
                text=merged,
                outcome="clarify_resume",
                reason="clarify_file",
                used_projection=True,
            )
        # short answer + original
        if len(raw) <= 40 and original and not original.startswith(raw):
            # only when answer looks like a constraint / action fragment
            if re.search(r"(?i)(只|仅|用|改|修|解释|重构|\.py)", raw):
                merged = f"{original}。{raw}"
                return ResolveResult(
                    text=merged,
                    outcome="clarify_resume",
                    reason="clarify_append",
                    used_projection=True,
                )

    # 2) Anaphora / continue / short fix
    if not _ANAPHORA.match(raw):
        return ResolveResult(text=raw, outcome="passthrough")

    last_text = (proj.last_text or "").strip()
    if not last_text:
        last_text = (_last_substantive_user(history) or "").strip()
        used_hist = bool(last_text)
        used_proj = False
    else:
        used_hist = bool(hist_users)
        used_proj = bool(proj.last_text)

    file_ref = _top_file(refs)
    issue_ref = _top_issue(refs)
    # slots from last intent
    slots = proj.last_slots or {}
    if not file_ref:
        files = slots.get("suspect_files") or []
        if isinstance(files, list) and files:
            file_ref = str(files[0])
    if not issue_ref and slots.get("issue_type"):
        issue_ref = str(slots["issue_type"])

    if _FIX_SHORT.match(raw):
        if last_text and (
            "Traceback" in last_text
            or _ISSUE_TYPE.search(last_text)
            or proj.last_primary in ("repair_request", "repair_issue")
        ):
            return ResolveResult(
                text=f"帮我修：{last_text}",
                outcome="resolved",
                reason="fix_last_issue",
                used_history=used_hist,
                used_projection=used_proj,
            )
        parts = ["帮我修这个问题"]
        if issue_ref:
            parts.append(issue_ref)
        if file_ref:
            parts.append(f"文件 {file_ref}")
        if len(parts) > 1:
            return ResolveResult(
                text=" ".join(parts),
                outcome="resolved",
                reason="fix_from_referents",
                used_history=used_hist or bool(file_ref or issue_ref),
                used_projection=used_proj,
            )
        if last_text:
            return ResolveResult(
                text=f"帮我修：{last_text}",
                outcome="resolved",
                reason="fix_last_text",
                used_history=used_hist,
                used_projection=used_proj,
            )
        return ResolveResult(
            text=raw,
            outcome="unresolved",
            reason="unresolved_anaphora",
        )

    if _CONTINUE.match(raw) or _DEIXIS_ONLY.match(raw):
        if last_text:
            # Prefer last primary framing
            if proj.last_primary in ("repair_request", "repair_issue"):
                rewritten = f"帮我修：{last_text}"
            elif proj.last_primary == "explain":
                rewritten = f"解释：{last_text}"
            elif proj.last_primary == "remember":
                rewritten = last_text  # already a remember utterance
            else:
                rewritten = last_text
            return ResolveResult(
                text=rewritten,
                outcome="resolved",
                reason="deixis_last_turn",
                used_history=used_hist,
                used_projection=used_proj,
            )
        if file_ref:
            return ResolveResult(
                text=f"解释一下 {file_ref}",
                outcome="resolved",
                reason="deixis_file_referent",
                used_history=True,
                used_projection=used_proj,
            )
        return ResolveResult(
            text=raw,
            outcome="unresolved",
            reason="unresolved_anaphora",
        )

    return ResolveResult(text=raw, outcome="passthrough")


def update_projection(
    proj: DialogueProjection,
    result: IntentResult,
    *,
    user_text: str,
    history: list[dict[str, Any]] | None = None,
) -> DialogueProjection:
    """Update thin projection after a route (and optional history mine)."""
    proj.turn_id += 1
    if result.action == "clarify" or result.primary == "clarify":
        clarify = (result.raw_signals or {}).get("clarify")
        if isinstance(clarify, dict):
            proj.pending_clarify = dict(clarify)
            proj.pending_clarify.setdefault("original_text", user_text)
        else:
            proj.pending_clarify = {
                "reason": result.reason or "ambiguous",
                "question": (result.slots or {}).get("clarify_question", ""),
                "original_text": user_text,
                "allow_execute": False,
            }
        # still record last text for anaphora to the unclear ask
        if user_text and not _ANAPHORA.match(user_text.strip()):
            proj.last_text = user_text
    else:
        proj.pending_clarify = None
        proj.last_primary = result.primary
        proj.last_action = result.action
        # Prefer node/full substantive text over anaphora rewrite source
        node_text = ""
        for n in result.graph.nodes if result.graph else []:
            if n.role == "executable" and n.text:
                node_text = n.text
                break
        substantive = node_text or user_text
        if substantive and not _ANAPHORA.match(substantive.strip()):
            proj.last_text = substantive
        proj.last_slots = dict(result.slots or {})

    # Refresh referents from slots + history + this turn
    refs = referents_from_history(history, projection=proj)
    for ref in extract_referents_from_text(user_text, turn_id=proj.turn_id):
        refs = _push_referent(refs, ref)
    for f in (result.slots or {}).get("suspect_files") or []:
        refs = _push_referent(
            refs, Referent(kind="file", value=str(f).replace("\\", "/"), source_turn=proj.turn_id)
        )
    if (result.slots or {}).get("issue_type"):
        refs = _push_referent(
            refs,
            Referent(
                kind="issue_type",
                value=str(result.slots["issue_type"]),
                source_turn=proj.turn_id,
            ),
        )
    proj.referents = refs[:MAX_REFERENTS]
    return proj
