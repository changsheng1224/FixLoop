"""Candidate intent discovery — collect, persist, aggregate (not auto-register).

Sources (online):
  - route_topk / clarify_residual / conflict from IntentRouter signals
  - user_cancel / user_rephrase / clarify_choice proxies from REPL

Sources (offline, optional):
  - llm_nominate via ``nominate_with_llm`` (proposal only; never mutates taxonomy)

Persisted under ``{cwd}/.agent/intent_candidates.jsonl`` as append-only events.
Aggregated ``CandidateIntentCard`` is the review unit before taxonomy changes.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agent_runtime.intent.clarify import ClarifyCandidate, candidates_from_hits
from agent_runtime.intent.models import (
    INTENT_ROUTER_VERSION,
    INTENT_TAXONOMY_VERSION,
    PRIMARY_ACTIONS,
    IntentResult,
)
from agent_runtime.intent.rules import RuleHit

DEFAULT_REL_PATH = Path(".agent") / "intent_candidates.jsonl"
GAP_PREFIX = "gap:"


@dataclass
class RunnerUp:
    primary: str
    confidence: float
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RunnerUp:
        return cls(
            primary=str(d.get("primary") or ""),
            confidence=float(d.get("confidence") or 0.0),
            reason=str(d.get("reason") or ""),
        )


@dataclass
class CandidateEvent:
    """One discovery observation (not yet a taxonomy change)."""

    source: str
    text: str
    predicted: str
    runners_up: list[RunnerUp] = field(default_factory=list)
    clarify_reason: str | None = None
    channel: str = "repl"
    ts: float = 0.0
    proposed_label: str | None = None
    merge_into: str | None = None
    note: str = ""
    severity: str = "low"  # low | medium | high (mis-exec risk)
    label_strength: str = "weak"  # weak | confirmed
    confirmed_label: str | None = None
    router_version: str = INTENT_ROUTER_VERSION
    taxonomy_version: str = INTENT_TAXONOMY_VERSION

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = time.time()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["runners_up"] = [r.to_dict() if isinstance(r, RunnerUp) else r for r in self.runners_up]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CandidateEvent:
        ups = [RunnerUp.from_dict(x) for x in (d.get("runners_up") or []) if isinstance(x, dict)]
        return cls(
            source=str(d.get("source") or "unknown"),
            text=str(d.get("text") or ""),
            predicted=str(d.get("predicted") or ""),
            runners_up=ups,
            clarify_reason=d.get("clarify_reason"),
            channel=str(d.get("channel") or "repl"),
            ts=float(d.get("ts") or 0.0),
            proposed_label=d.get("proposed_label"),
            merge_into=d.get("merge_into"),
            note=str(d.get("note") or ""),
            severity=str(d.get("severity") or "low"),
            label_strength=str(d.get("label_strength") or "weak"),
            confirmed_label=d.get("confirmed_label"),
            router_version=str(d.get("router_version") or "legacy"),
            taxonomy_version=str(d.get("taxonomy_version") or "legacy"),
        )


@dataclass
class CandidateIntentCard:
    """Review unit for taxonomy expansion (aggregated)."""

    key: str
    label_hint: str
    count: int
    example_texts: list[str] = field(default_factory=list)
    sources: dict[str, int] = field(default_factory=dict)
    closest_existing: str | None = None
    severity_max: str = "low"
    notes: list[str] = field(default_factory=list)
    weak_count: int = 0
    confirmed_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _severity_for(result: IntentResult, *, source: str) -> str:
    if source == "conflict":
        return "high"
    if result.primary in ("repair_request", "repair_issue") or any(
        (n.primary or "").startswith("repair") for n in (result.graph.nodes if result.graph else [])
    ):
        return "high"
    if source in ("user_cancel", "clarify_residual", "no_hit"):
        return "medium"
    return "low"


def _runners_from_hits(hits: list[tuple[Any, RuleHit]], *, limit: int = 5) -> list[RunnerUp]:
    cands = candidates_from_hits(hits, limit=limit)
    return [RunnerUp(primary=c.primary, confidence=c.confidence, reason=c.reason) for c in cands]


def _runners_from_clarify(result: IntentResult) -> list[RunnerUp]:
    clarify = (result.raw_signals or {}).get("clarify") or {}
    raw = clarify.get("candidates") or []
    out: list[RunnerUp] = []
    for item in raw:
        if isinstance(item, ClarifyCandidate):
            out.append(RunnerUp(item.primary, item.confidence, item.reason))
        elif isinstance(item, dict) and item.get("primary"):
            out.append(
                RunnerUp(
                    str(item["primary"]),
                    float(item.get("confidence") or 0.0),
                    str(item.get("reason") or ""),
                )
            )
    return out


def collect_from_route(
    result: IntentResult,
    *,
    text: str,
    hits: list[tuple[Any, RuleHit]] | None = None,
    channel: str = "repl",
) -> list[CandidateEvent]:
    """Extract discovery events from one route() result (no I/O)."""
    events: list[CandidateEvent] = []
    hits = hits or []
    runners = _runners_from_hits(hits) or _runners_from_clarify(result)
    predicted = result.primary or ""
    clarify_reason = (result.raw_signals or {}).get("clarify_reason") or None
    if isinstance(clarify_reason, str) and not clarify_reason.strip():
        clarify_reason = None

    # Always record top-k snapshot when there is ambiguity or clarify
    interesting = (
        result.action == "clarify"
        or result.primary == "clarify"
        or bool((result.raw_signals or {}).get("conflict"))
        or clarify_reason in ("no_hit", "ambiguous", "conflict", "unresolved_anaphora", "low_conf")
        or (len(runners) >= 2 and runners[0].primary != predicted)
    )
    if interesting and (runners or clarify_reason):
        source = "clarify_residual" if result.action == "clarify" else "route_topk"
        if clarify_reason == "conflict" or (result.raw_signals or {}).get("conflict"):
            source = "conflict"
        elif clarify_reason == "no_hit":
            source = "no_hit"
        events.append(
            CandidateEvent(
                source=source,
                text=(text or "")[:2000],
                predicted=predicted,
                runners_up=runners,
                clarify_reason=str(clarify_reason) if clarify_reason else None,
                channel=channel,
                severity=_severity_for(result, source=source),
                note="auto from route",
            )
        )

    # Gap card hint when closed-set miss
    if clarify_reason in ("no_hit", "unresolved_anaphora", "ambiguous") and not runners:
        events.append(
            CandidateEvent(
                source="no_hit",
                text=(text or "")[:2000],
                predicted=predicted or "clarify",
                runners_up=[],
                clarify_reason=str(clarify_reason),
                channel=channel,
                proposed_label=f"{GAP_PREFIX}{clarify_reason}",
                severity="medium",
                note="no strong closed-set runner-up",
            )
        )
    return events


def collect_user_feedback(
    *,
    kind: str,
    text: str,
    predicted: str = "",
    chosen: str | None = None,
    channel: str = "repl",
    previous_text: str | None = None,
) -> CandidateEvent:
    """Build event from cancel / rephrase / clarify_choice (REPL proxies)."""
    kind = kind.strip().lower()
    source = {
        "cancel": "user_cancel",
        "rephrase": "user_rephrase",
        "clarify_choice": "clarify_choice",
        "action_switch": "action_switch",
        "ground_truth": "ground_truth",
    }.get(kind, f"user_{kind}")
    runners: list[RunnerUp] = []
    proposed = None
    if chosen:
        runners = [RunnerUp(primary=chosen, confidence=1.0, reason=f"user:{kind}")]
        proposed = chosen if chosen not in PRIMARY_ACTIONS else None
    note = ""
    if kind == "rephrase" and previous_text:
        note = f"prev={previous_text[:120]}"
    return CandidateEvent(
        source=source,
        text=(text or "")[:2000],
        predicted=predicted or "",
        runners_up=runners,
        channel=channel,
        proposed_label=proposed,
        severity="high" if kind == "cancel" else "medium",
        note=note,
        label_strength=(
            "confirmed"
            if chosen and kind in {"clarify_choice", "action_switch", "ground_truth"}
            else "weak"
        ),
        confirmed_label=(
            chosen
            if chosen and kind in {"clarify_choice", "action_switch", "ground_truth"}
            else None
        ),
    )


def nominate_with_llm(
    text: str,
    *,
    light_client: Any,
    closed_set: list[str] | None = None,
) -> CandidateEvent | None:
    """Offline nominator: ask light model for merge_into or new short label.

    Never writes taxonomy. Returns None on failure / empty client.
    """
    if light_client is None:
        return None
    labels = closed_set or sorted(PRIMARY_ACTIONS.keys())
    prompt = (
        "You nominate intent labels for a coding agent. "
        "Reply ONE JSON object only with keys: "
        "merge_into (existing label or null), proposed_label (short snake_case or null), "
        "reason (short). Prefer merge_into when possible.\n"
        f"Closed set: {', '.join(labels)}\n"
        f"User text: {text[:1500]}\n"
    )
    try:
        if hasattr(light_client, "complete"):
            raw = light_client.complete(prompt)
        elif callable(light_client):
            raw = light_client(prompt)
        else:
            return None
        body = str(raw or "").strip()
        # tolerate fenced json
        if "```" in body:
            body = body.split("```", 2)[1]
            if body.startswith("json"):
                body = body[4:]
        data = json.loads(body[body.find("{") : body.rfind("}") + 1])
    except Exception:
        return None
    merge = data.get("merge_into")
    prop = data.get("proposed_label")
    if merge in ("", "null", None):
        merge = None
    if prop in ("", "null", None):
        prop = None
    if merge and merge not in PRIMARY_ACTIONS:
        merge = None
    return CandidateEvent(
        source="llm_nominate",
        text=(text or "")[:2000],
        predicted="",
        runners_up=[],
        proposed_label=str(prop) if prop else None,
        merge_into=str(merge) if merge else None,
        note=str(data.get("reason") or "")[:200],
        severity="low",
    )


class CandidateStore:
    """Append-only JSONL store for candidate events."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.path = self.root / DEFAULT_REL_PATH

    def append(self, event: CandidateEvent | list[CandidateEvent]) -> None:
        events = event if isinstance(event, list) else [event]
        if not events:
            return
        from agent_runtime.intent.observability import record_feedback_write

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps(ev.to_dict(), ensure_ascii=False, default=str) + "\n")
        except Exception:
            for ev in events:
                record_feedback_write(status="error", strength=ev.label_strength)
            raise
        for ev in events:
            record_feedback_write(status="success", strength=ev.label_strength)

    def load(self) -> list[CandidateEvent]:
        if not self.path.is_file():
            return []
        out: list[CandidateEvent] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(CandidateEvent.from_dict(json.loads(line)))
            except Exception:
                continue
        return out

    def clear(self) -> None:
        if self.path.is_file():
            self.path.unlink()


_SEV_RANK = {"low": 0, "medium": 1, "high": 2}


def _card_key(ev: CandidateEvent) -> str:
    if ev.confirmed_label:
        return f"confirmed:{ev.confirmed_label}"
    if ev.proposed_label:
        return ev.proposed_label
    if ev.merge_into:
        return f"merge:{ev.merge_into}"
    # prefer first runner-up that differs from predicted
    for r in ev.runners_up:
        if r.primary and r.primary != ev.predicted:
            return f"runner:{r.primary}"
    if ev.clarify_reason:
        return f"{GAP_PREFIX}{ev.clarify_reason}"
    return f"pred:{ev.predicted or 'unknown'}"


def aggregate_cards(
    events: list[CandidateEvent],
    *,
    min_count: int = 1,
    max_examples: int = 8,
) -> list[CandidateIntentCard]:
    """Cluster events into review cards sorted by count then severity."""
    buckets: dict[str, list[CandidateEvent]] = defaultdict(list)
    for ev in events:
        buckets[_card_key(ev)].append(ev)

    cards: list[CandidateIntentCard] = []
    for key, group in buckets.items():
        if len(group) < min_count:
            continue
        sources: dict[str, int] = defaultdict(int)
        examples: list[str] = []
        notes: list[str] = []
        closest: str | None = None
        sev = "low"
        for ev in group:
            sources[ev.source] += 1
            if ev.text and ev.text not in examples and len(examples) < max_examples:
                examples.append(ev.text[:240])
            if ev.note and ev.note not in notes and len(notes) < 5:
                notes.append(ev.note[:160])
            if ev.merge_into:
                closest = ev.merge_into
            elif ev.runners_up and not closest:
                closest = ev.runners_up[0].primary
            if _SEV_RANK.get(ev.severity, 0) > _SEV_RANK.get(sev, 0):
                sev = ev.severity
        weak_count = sum(1 for ev in group if ev.label_strength != "confirmed")
        confirmed_count = sum(1 for ev in group if ev.label_strength == "confirmed")
        label_hint = key
        if key.startswith("runner:"):
            label_hint = key.split(":", 1)[1]
        elif key.startswith("merge:"):
            label_hint = key.split(":", 1)[1]
        elif key.startswith(GAP_PREFIX):
            label_hint = key
        elif key.startswith("confirmed:"):
            label_hint = key.split(":", 1)[1]
        cards.append(
            CandidateIntentCard(
                key=key,
                label_hint=label_hint,
                count=len(group),
                example_texts=examples,
                sources=dict(sources),
                closest_existing=closest if closest in PRIMARY_ACTIONS else closest,
                severity_max=sev,
                notes=notes,
                weak_count=weak_count,
                confirmed_count=confirmed_count,
            )
        )

    cards.sort(key=lambda c: (-_SEV_RANK.get(c.severity_max, 0), -c.count, c.key))
    return cards


def events_from_llm_candidates(
    *,
    text: str,
    predicted: str,
    llm_candidates: list[Any],
    channel: str = "repl",
    need_clarify: bool | None = None,
) -> list[CandidateEvent]:
    """Turn LLM refine side-channel candidates into discovery events."""
    events: list[CandidateEvent] = []
    runners: list[RunnerUp] = []
    for item in llm_candidates or []:
        if hasattr(item, "to_dict"):
            d = item.to_dict()
        elif isinstance(item, dict):
            d = item
        else:
            continue
        label = str(d.get("label") or "")
        if not label:
            continue
        runners.append(
            RunnerUp(
                primary=label if label in PRIMARY_ACTIONS else str(d.get("merge_into") or label),
                confidence=float(d.get("confidence") or 0.0),
                reason="llm_candidate",
            )
        )
        proposed = None
        merge = d.get("merge_into")
        if d.get("is_new") or label not in PRIMARY_ACTIONS:
            proposed = label
        events.append(
            CandidateEvent(
                source="llm_nominate",
                text=(text or "")[:2000],
                predicted=predicted or "",
                runners_up=[
                    RunnerUp(
                        primary=(
                            label if label in PRIMARY_ACTIONS else (str(merge) if merge else label)
                        ),
                        confidence=float(d.get("confidence") or 0.0),
                        reason="llm",
                    )
                ],
                channel=channel,
                proposed_label=proposed,
                merge_into=str(merge) if merge else (label if label in PRIMARY_ACTIONS else None),
                severity="medium" if need_clarify else "low",
                note="from llm refine candidates",
            )
        )
    # Also one bundled top-k event for aggregation by runner
    if runners and not events:
        events.append(
            CandidateEvent(
                source="llm_nominate",
                text=(text or "")[:2000],
                predicted=predicted or "",
                runners_up=runners[:5],
                channel=channel,
                severity="low",
                note="from llm refine candidates",
            )
        )
    return events


def record_route_candidates(
    result: IntentResult,
    *,
    text: str,
    root: str | Path | None,
    hits: list[tuple[Any, RuleHit]] | None = None,
    channel: str = "repl",
) -> list[CandidateEvent]:
    """Collect + optionally persist; attach summary onto result.raw_signals."""
    events = collect_from_route(result, text=text, hits=hits, channel=channel)
    if events and root is not None:
        try:
            CandidateStore(root).append(events)
        except Exception:
            pass
    if events:
        signals = dict(result.raw_signals or {})
        signals["candidate_events"] = [e.to_dict() for e in events]
        signals["candidate_keys"] = sorted({_card_key(e) for e in events})
        result.raw_signals = signals
    return events


def discover_from_cases(
    *,
    root: str | Path | None = None,
    case_path: Path | None = None,
    persist: bool = True,
    tags_any: list[str] | None = None,
    strata: list[str] | None = None,
) -> tuple[list[CandidateEvent], list[CandidateIntentCard]]:
    """Route offline eval cases and extract candidate-intent events.

    Uses the same gold YAML suite as eval_metrics (incl. realistic / held-out).
    Does **not** change taxonomy — only discovery cards for human review.
    """
    from agent_runtime.intent.dialogue import DialogueProjection, update_projection
    from agent_runtime.intent.eval_metrics import load_eval_cases
    from agent_runtime.intent.models import RouteContext
    from agent_runtime.intent.router import IntentRouter

    cases = load_eval_cases(case_path)
    if tags_any:
        want = set(tags_any)
        cases = [c for c in cases if want & set(c.tags)]
    if strata:
        want_s = set(strata)
        cases = [c for c in cases if c.stratum in want_s]

    router = IntentRouter()
    all_events: list[CandidateEvent] = []
    for case in cases:
        proj = (
            DialogueProjection.from_dict(case.dialogue) if case.dialogue else DialogueProjection()
        )
        built: list[dict[str, Any]] = []
        for h in case.history:
            prior = router.route(
                h,
                RouteContext(
                    channel=case.channel,  # type: ignore[arg-type]
                    history=list(built),
                    dialogue=proj,
                    candidate_root=None,
                ),
            )
            proj = update_projection(proj, prior, user_text=h, history=built)
            built.append({"role": "user", "content": h})

        result = router.route(
            case.text,
            RouteContext(
                channel=case.channel,  # type: ignore[arg-type]
                history=[{"role": "user", "content": h} for h in case.history],
                dialogue=proj if (case.history or case.dialogue) else None,
                candidate_root=None,
            ),
        )
        evs = collect_from_route(
            result,
            text=case.text,
            channel=case.channel,
        )
        # Annotate with case id for review
        for ev in evs:
            ev.note = (ev.note + f" | case={case.id}").strip(" |")
            if case.stratum:
                ev.note = f"{ev.note} | stratum={case.stratum}"

        exp = case.expect.get("primary")
        gold_mismatch = bool(exp) and result.primary != exp
        if gold_mismatch and {exp, result.primary} <= {"repair_request", "repair_issue"}:
            gold_mismatch = False
        if gold_mismatch:
            # Always record a card when offline gold disagrees (held-out discovery)
            bumped = False
            for ev in evs:
                ev.severity = "high"
                ev.proposed_label = ev.proposed_label or f"gap:expect_{exp}"
                ev.merge_into = ev.merge_into or (exp if exp in PRIMARY_ACTIONS else None)
                ev.note = f"{ev.note} | gold={exp} pred={result.primary}"
                bumped = True
            if not bumped:
                evs.append(
                    CandidateEvent(
                        source="gold_mismatch",
                        text=(case.text or "")[:2000],
                        predicted=result.primary or "",
                        runners_up=[
                            RunnerUp(
                                primary=str(exp),
                                confidence=1.0,
                                reason="eval_gold",
                            )
                        ],
                        channel=case.channel,
                        proposed_label=f"gap:expect_{exp}",
                        merge_into=str(exp) if exp in PRIMARY_ACTIONS else None,
                        severity="high",
                        note=(
                            f"case={case.id} | stratum={case.stratum} | "
                            f"gold={exp} pred={result.primary}"
                        ),
                    )
                )
        all_events.extend(evs)

    if persist and root is not None and all_events:
        CandidateStore(root).append(all_events)

    cards = aggregate_cards(all_events, min_count=1)
    return all_events, cards


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Intent candidate discovery store")
    p.add_argument("--root", default=".", help="workspace root containing .agent/")
    p.add_argument("--min-count", type=int, default=1)
    p.add_argument("--json", action="store_true", help="print cards as JSON")
    p.add_argument("--clear", action="store_true", help="delete the JSONL store")
    p.add_argument(
        "--from-eval",
        action="store_true",
        help="route all offline eval cases and extract candidate cards",
    )
    p.add_argument(
        "--tags",
        default="",
        help="comma-separated tag filter for --from-eval (e.g. heldout_gap,clarify)",
    )
    p.add_argument(
        "--strata",
        default="",
        help="comma-separated stratum filter (e.g. heldout_gap,vague_clarify)",
    )
    p.add_argument(
        "--out",
        default="",
        help="optional path to write JSON report (events+cards)",
    )
    args = p.parse_args(argv)

    store = CandidateStore(args.root)
    if args.clear:
        store.clear()
        print("cleared", store.path)
        return 0

    if args.from_eval:
        tags = [t.strip() for t in args.tags.split(",") if t.strip()] or None
        strata = [s.strip() for s in args.strata.split(",") if s.strip()] or None
        events, cards = discover_from_cases(
            root=args.root,
            persist=True,
            tags_any=tags,
            strata=strata,
        )
        cards = [c for c in cards if c.count >= args.min_count] or cards
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                json.dumps(
                    {
                        "event_count": len(events),
                        "card_count": len(cards),
                        "cards": [c.to_dict() for c in cards],
                        "events": [e.to_dict() for e in events],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"wrote {out_path}")
        if args.json:
            print(json.dumps([c.to_dict() for c in cards], ensure_ascii=False, indent=2))
        else:
            print(f"[from-eval] events={len(events)} cards={len(cards)}")
            print("-" * 60)
            for c in cards:
                print(
                    f"[{c.severity_max:6s}] n={c.count:3d}  key={c.key}\n"
                    f"         closest={c.closest_existing}  sources={c.sources}"
                )
                for ex in c.example_texts[:2]:
                    one = ex.replace("\n", " ")[:110]
                    print(f"         e.g. {one}")
                for n in c.notes[:2]:
                    print(f"         note: {n[:110]}")
                print()
        return 0

    events = store.load()
    cards = aggregate_cards(events, min_count=args.min_count)
    if args.json:
        print(json.dumps([c.to_dict() for c in cards], ensure_ascii=False, indent=2))
    else:
        print(f"events={len(events)} cards={len(cards)} path={store.path}")
        for c in cards[:50]:
            print(
                f"- {c.key}  n={c.count}  sev={c.severity_max}  "
                f"closest={c.closest_existing}  sources={c.sources}"
            )
            for ex in c.example_texts[:2]:
                print(f"    ex: {ex[:100]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
