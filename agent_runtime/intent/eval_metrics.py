"""Offline enterprise-grade Intent Router evaluation metrics."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent_runtime.intent.models import PRIMARY_ACTIONS, IntentResult, RouteContext
from agent_runtime.intent.router import IntentRouter

# High-severity misroute pairs (unordered normalized as frozenset of two primaries,
# plus special singleton rules).
SEVERE_PAIRS: frozenset[frozenset[str]] = frozenset(
    {
        frozenset({"repair_request", "ask"}),
        frozenset({"repair_issue", "ask"}),
        frozenset({"repair_request", "remember"}),
        frozenset({"repair_issue", "remember"}),
        frozenset({"repair_request", "help"}),
        frozenset({"repair_issue", "help"}),
        frozenset({"repair_request", "implement"}),
        frozenset({"repair_issue", "implement"}),
        frozenset({"repair_request", "refactor"}),
        frozenset({"repair_issue", "refactor"}),
        frozenset({"repair_request", "explain"}),
        frozenset({"repair_issue", "explain"}),
        frozenset({"cancel", "ask"}),
        frozenset({"cancel", "help"}),
        frozenset({"implement", "refactor"}),
    }
)
# Missed cancel: expected cancel, predicted other
SEVERE_MISS_CANCEL = True

TAU_EXEC_DEFAULT = 0.60
TAU_CLARIFY_DEFAULT = 0.45


@dataclass
class IntentEvalCase:
    id: str
    text: str
    channel: str = "repl"
    expect: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=list)  # prior user turns (oldest→newest)
    dialogue: dict[str, Any] = field(default_factory=dict)  # optional thin projection seed
    stratum: str = ""  # distribution bucket, e.g. ask_howto / repair_stack
    weight: float = 1.0  # for weighted_misroute under simulated mix


@dataclass
class IntentEvalRow:
    case_id: str
    channel: str
    expected_primary: str | None
    predicted_primary: str
    expected_mode: str | None
    predicted_mode: str
    expected_action: str | None
    predicted_action: str
    primary_ok: bool
    mode_ok: bool
    action_ok: bool
    severe_misroute: bool
    confidence: float
    latency_ms: float
    expected_exec_primaries: list[str] = field(default_factory=list)
    predicted_exec_primaries: list[str] = field(default_factory=list)
    node_recall: float = 0.0
    sequence_ok: bool | None = None
    slot_hits: dict[str, bool] = field(default_factory=dict)
    false_split: bool = False
    false_merge: bool = False
    expected_clarify: bool = False
    predicted_clarify: bool = False
    tags: list[str] = field(default_factory=list)
    stratum: str = ""
    weight: float = 1.0

    def to_dict(self) -> dict:
        return asdict(self)


def load_eval_cases(path: Path | None = None) -> list[IntentEvalCase]:
    """Load gold cases from primary YAML plus optional realistic-stack companion."""
    primary = path or Path(__file__).with_name("eval_cases.yaml")
    paths = [primary]
    companion_names = (
        "eval_cases_realistic_stacks.yaml",
        "eval_cases_enterprise.yaml",
        "eval_cases_realistic_users.yaml",
        "eval_cases_heldout_gaps.yaml",
        "eval_cases_ood_hardening.yaml",
    )
    if path is None:
        for name in companion_names:
            companion = primary.with_name(name)
            if companion.is_file():
                paths.append(companion)

    cases: list[IntentEvalCase] = []
    seen: set[str] = set()
    for p in paths:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or []
        for item in raw:
            cid = str(item["id"])
            if cid in seen:
                continue
            seen.add(cid)
            cases.append(
                IntentEvalCase(
                    id=cid,
                    text=str(item["text"]),
                    channel=str(item.get("channel", "repl")),
                    expect=dict(item.get("expect") or {}),
                    tags=list(item.get("tags") or []),
                    history=[
                        h
                        for h in (_normalize_history_item(x) for x in (item.get("history") or []))
                        if h
                    ],
                    dialogue=dict(item.get("dialogue") or {}),
                    stratum=str(item.get("stratum") or _stratum_from_tags(item.get("tags") or [])),
                    weight=float(item.get("weight") or 1.0),
                )
            )
    return cases


def _stratum_from_tags(tags: list[Any]) -> str:
    for t in tags:
        s = str(t)
        if s.startswith("stratum:"):
            return s.split(":", 1)[1]
    return ""


def _normalize_history_item(item: Any) -> str | None:
    if item is None:
        return None
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        content = item.get("content")
        if content is None:
            return None
        return str(content)
    return str(item)


def _is_severe(expected: str | None, predicted: str) -> bool:
    if not expected:
        return False
    if expected == predicted:
        return False
    if SEVERE_MISS_CANCEL and expected == "cancel" and predicted != "cancel":
        return True
    if predicted == "cancel" and expected != "cancel":
        return True
    return frozenset({expected, predicted}) in SEVERE_PAIRS


def _exec_primaries(result: IntentResult) -> list[str]:
    nodes = [n for n in result.graph.nodes if n.role == "executable"]
    # stable order by span then id
    nodes = sorted(nodes, key=lambda n: (n.span.get("start", 0), n.id))
    return [n.primary for n in nodes]


def _sequence_ok(result: IntentResult, expected_seq: list[str] | None) -> bool | None:
    if not expected_seq or len(expected_seq) < 2:
        return None
    pred = _exec_primaries(result)
    return pred == expected_seq


def _slot_hits(result: IntentResult, expect_slots: dict | None) -> dict[str, bool]:
    if not expect_slots:
        return {}
    hits: dict[str, bool] = {}
    slots = result.slots or {}
    # also merge from executable nodes
    for n in result.graph.nodes:
        if n.role == "executable":
            for k, v in n.slots.items():
                if k not in slots or not slots[k]:
                    slots[k] = v
    for key, expected in expect_slots.items():
        got = slots.get(key)
        if isinstance(expected, list):
            got_list = list(got) if isinstance(got, list) else ([got] if got else [])
            got_norm = {str(x).replace("\\", "/") for x in got_list}
            exp_norm = {str(x).replace("\\", "/") for x in expected}
            hits[key] = exp_norm.issubset(got_norm)
        else:
            hits[key] = str(got) == str(expected) if got is not None else False
    return hits


def evaluate_case(
    case: IntentEvalCase,
    router: IntentRouter | None = None,
    *,
    tau_clarify: float = TAU_CLARIFY_DEFAULT,
    tau_exec: float = TAU_EXEC_DEFAULT,
) -> IntentEvalRow:
    router = router or IntentRouter()
    t0 = time.perf_counter()
    history_msgs = [{"role": "user", "content": h} for h in case.history]
    # Build thin projection from prior turns so multi-turn gold works offline
    from agent_runtime.intent.dialogue import DialogueProjection, update_projection

    proj = DialogueProjection.from_dict(case.dialogue) if case.dialogue else DialogueProjection()
    built_hist: list[dict[str, Any]] = []
    for h in case.history:
        prior = router.route(
            h,
            RouteContext(
                channel=case.channel,
                history=list(built_hist),
                dialogue=proj,
            ),  # type: ignore[arg-type]
        )
        proj = update_projection(proj, prior, user_text=h, history=built_hist)
        built_hist.append({"role": "user", "content": h})
    result = router.route(
        case.text,
        RouteContext(
            channel=case.channel,
            tau_clarify=tau_clarify,
            tau_exec=tau_exec,
            history=history_msgs,
            dialogue=proj if (case.history or case.dialogue) else None,
        ),  # type: ignore[arg-type]
    )
    latency_ms = (time.perf_counter() - t0) * 1000.0

    exp = case.expect
    exp_primary = exp.get("primary")
    exp_mode = exp.get("mode")
    exp_action = exp.get("action")
    exp_exec = list(exp.get("exec_primaries") or ([] if not exp_primary else [exp_primary]))
    pred_exec = _exec_primaries(result)

    primary_ok = exp_primary is None or result.primary == exp_primary
    # repair_request vs repair_issue are equivalent for anaphora rewrite paths
    if not primary_ok and {exp_primary, result.primary} <= {
        "repair_request",
        "repair_issue",
    }:
        primary_ok = True
    # For multi with run_graph, primary may be first root; also accept if exec list matches
    if not primary_ok and exp.get("exec_primaries"):
        primary_ok = pred_exec == list(exp["exec_primaries"]) or (
            exp_primary in pred_exec and result.graph.mode == exp_mode
        )

    mode_ok = exp_mode is None or result.graph.mode == exp_mode
    action_ok = exp_action is None or result.action == exp_action

    exp_anaphora = exp.get("anaphora_outcome")
    if exp_anaphora is not None:
        got_ana = ((result.raw_signals or {}).get("anaphora") or {}).get("outcome")
        if got_ana != exp_anaphora:
            primary_ok = False
            action_ok = False

    # node recall
    if exp_exec:
        hit = sum(1 for p in exp_exec if p in pred_exec)
        node_recall = hit / len(exp_exec)
    else:
        node_recall = 1.0 if primary_ok else 0.0

    false_split = bool(exp.get("mode") == "hybrid" and result.graph.mode == "multi")
    false_split = false_split or bool(
        exp.get("mode") == "single"
        and result.graph.mode == "multi"
        and "ask" in (exp_primary or "")
    )
    # more general: expected single/hybrid but got multi with >1 exec
    if exp_mode in ("single", "hybrid") and result.graph.mode == "multi" and len(pred_exec) > 1:
        false_split = True
    false_merge = bool(exp_mode == "multi" and result.graph.mode in ("single", "hybrid"))

    exp_clarify = bool(exp.get("clarify") or exp_primary == "clarify")
    pred_clarify = result.primary == "clarify" or result.action == "clarify"

    # Optional clarify_reason label check (A+B+C observability)
    exp_reason = exp.get("clarify_reason")
    if exp_reason is not None:
        got_reason = (result.raw_signals or {}).get("clarify_reason")
        if got_reason != exp_reason:
            primary_ok = False
            action_ok = False

    return IntentEvalRow(
        case_id=case.id,
        channel=case.channel,
        expected_primary=exp_primary,
        predicted_primary=result.primary,
        expected_mode=exp_mode,
        predicted_mode=result.graph.mode,
        expected_action=exp_action,
        predicted_action=result.action,
        primary_ok=bool(primary_ok),
        mode_ok=bool(mode_ok),
        action_ok=bool(action_ok),
        severe_misroute=_is_severe(exp_primary, result.primary),
        confidence=result.confidence,
        latency_ms=latency_ms,
        expected_exec_primaries=exp_exec,
        predicted_exec_primaries=pred_exec,
        node_recall=node_recall,
        sequence_ok=_sequence_ok(result, exp.get("exec_primaries")),
        slot_hits=_slot_hits(result, exp.get("slots")),
        false_split=false_split,
        false_merge=false_merge,
        expected_clarify=exp_clarify,
        predicted_clarify=pred_clarify,
        tags=list(case.tags),
        stratum=case.stratum or _stratum_from_tags(case.tags),
        weight=float(case.weight or 1.0),
    )


def _safe_div(a: float, b: float) -> float:
    return round(a / b, 6) if b else 0.0


def _prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    prec = _safe_div(tp, tp + fp)
    rec = _safe_div(tp, tp + fn)
    f1 = _safe_div(2 * prec * rec, prec + rec) if (prec + rec) else 0.0
    return {"precision": prec, "recall": rec, "f1": f1, "support": tp + fn}


def compute_intent_metrics(rows: list[IntentEvalRow]) -> dict[str, Any]:
    total = len(rows)
    if total == 0:
        return {"summary": {"total": 0}, "per_class": {}, "confusion": {}, "cases": []}

    labeled_primary = [r for r in rows if r.expected_primary]
    primary_correct = sum(1 for r in labeled_primary if r.primary_ok)
    mode_labeled = [r for r in rows if r.expected_mode]
    action_labeled = [r for r in rows if r.expected_action]

    misroute_rate = _safe_div(len(labeled_primary) - primary_correct, len(labeled_primary))
    severe = sum(1 for r in labeled_primary if r.severe_misroute)

    # confusion + per-class
    labels = sorted(PRIMARY_ACTIONS.keys())
    confusion: dict[str, dict[str, int]] = {e: {p: 0 for p in labels} for e in labels}
    for r in labeled_primary:
        e = r.expected_primary or "ask"
        p = r.predicted_primary
        if e not in confusion:
            confusion[e] = {x: 0 for x in labels}
        if p not in confusion[e]:
            confusion[e][p] = 0
        confusion[e][p] = confusion[e].get(p, 0) + 1

    per_class: dict[str, dict] = {}
    f1s: list[float] = []
    for label in labels:
        tp = sum(
            1
            for r in labeled_primary
            if r.expected_primary == label and r.predicted_primary == label
        )
        fp = sum(
            1
            for r in labeled_primary
            if r.predicted_primary == label and r.expected_primary != label
        )
        fn = sum(
            1
            for r in labeled_primary
            if r.expected_primary == label and r.predicted_primary != label
        )
        stats = _prf(tp, fp, fn)
        per_class[label] = stats
        if stats["support"] > 0:
            f1s.append(stats["f1"])

    # micro
    tp_m = sum(1 for r in labeled_primary if r.primary_ok)
    # micro precision/recall for multi-class exact match equals accuracy when one label each
    micro_f1 = _safe_div(tp_m, len(labeled_primary))

    # clarify PR
    tp_c = sum(1 for r in rows if r.expected_clarify and r.predicted_clarify)
    fp_c = sum(1 for r in rows if r.predicted_clarify and not r.expected_clarify)
    fn_c = sum(1 for r in rows if r.expected_clarify and not r.predicted_clarify)
    clarify_stats = _prf(tp_c, fp_c, fn_c)

    # slots
    slot_keys: set[str] = set()
    for r in rows:
        slot_keys.update(r.slot_hits.keys())
    slot_f1: dict[str, float] = {}
    for k in sorted(slot_keys):
        vals = [r.slot_hits[k] for r in rows if k in r.slot_hits]
        slot_f1[k] = _safe_div(sum(1 for v in vals if v), len(vals))

    # calibration ECE (10 bins)
    bins = 10
    bucket_totals = [0] * bins
    bucket_correct = [0] * bins
    bucket_conf_sum = [0.0] * bins
    for r in labeled_primary:
        b = min(bins - 1, int(r.confidence * bins))
        bucket_totals[b] += 1
        bucket_conf_sum[b] += r.confidence
        if r.primary_ok:
            bucket_correct[b] += 1
    ece = 0.0
    for i in range(bins):
        if bucket_totals[i] == 0:
            continue
        acc = bucket_correct[i] / bucket_totals[i]
        conf = bucket_conf_sum[i] / bucket_totals[i]
        ece += (bucket_totals[i] / len(labeled_primary)) * abs(acc - conf)

    correct_rows = [r for r in labeled_primary if r.primary_ok]
    wrong_rows = [r for r in labeled_primary if not r.primary_ok]
    avg = lambda xs: _safe_div(sum(xs), len(xs)) if xs else 0.0  # noqa: E731

    seq_rows = [r for r in rows if r.sequence_ok is not None]
    latencies = sorted(r.latency_ms for r in rows)

    def pct(p: float) -> float:
        if not latencies:
            return 0.0
        idx = min(len(latencies) - 1, int(round((p / 100.0) * (len(latencies) - 1))))
        return round(latencies[idx], 3)

    by_channel: dict[str, dict] = {}
    for ch in sorted({r.channel for r in rows}):
        sub = [r for r in rows if r.channel == ch]
        lab = [r for r in sub if r.expected_primary]
        ok = sum(1 for r in lab if r.primary_ok)
        by_channel[ch] = {
            "total": len(sub),
            "primary_accuracy": _safe_div(ok, len(lab)),
            "misroute_rate": _safe_div(len(lab) - ok, len(lab)),
            "severe_misroute_rate": _safe_div(sum(1 for r in lab if r.severe_misroute), len(lab)),
        }

    by_stratum: dict[str, dict] = {}
    for st in sorted({r.stratum or "unspecified" for r in labeled_primary}):
        sub = [r for r in labeled_primary if (r.stratum or "unspecified") == st]
        by_stratum[st] = {
            "total": len(sub),
            "weight_sum": round(sum(r.weight for r in sub), 4),
            "primary_accuracy": _safe_div(sum(1 for r in sub if r.primary_ok), len(sub)),
            "misroute_rate": _safe_div(sum(1 for r in sub if not r.primary_ok), len(sub)),
            "severe_misroute_rate": _safe_div(sum(1 for r in sub if r.severe_misroute), len(sub)),
            "clarify_rate": _safe_div(sum(1 for r in sub if r.predicted_clarify), len(sub)),
        }

    w_total = sum(r.weight for r in labeled_primary) or 1.0
    w_wrong = sum(r.weight for r in labeled_primary if not r.primary_ok)
    w_severe = sum(r.weight for r in labeled_primary if r.severe_misroute)
    weighted_misroute_rate = _safe_div(w_wrong, w_total)
    weighted_severe_misroute_rate = _safe_div(w_severe, w_total)

    in_dist = [r for r in labeled_primary if r.stratum != "heldout_gap"]
    heldout = [r for r in labeled_primary if r.stratum == "heldout_gap"]
    in_distribution_misroute_rate = _safe_div(
        sum(1 for r in in_dist if not r.primary_ok), len(in_dist)
    )
    heldout_gap_misroute_rate = _safe_div(sum(1 for r in heldout if not r.primary_ok), len(heldout))

    summary = {
        "total": total,
        "labeled_primary": len(labeled_primary),
        "primary_accuracy": _safe_div(primary_correct, len(labeled_primary)),
        "misroute_rate": misroute_rate,
        "severe_misroute_rate": _safe_div(severe, len(labeled_primary)),
        "mode_accuracy": _safe_div(sum(1 for r in mode_labeled if r.mode_ok), len(mode_labeled)),
        "action_accuracy": _safe_div(
            sum(1 for r in action_labeled if r.action_ok), len(action_labeled)
        ),
        "exact_graph_match_rate": _safe_div(
            sum(
                1
                for r in rows
                if r.primary_ok
                and r.mode_ok
                and (r.sequence_ok is not False)
                and (
                    not r.expected_exec_primaries
                    or r.predicted_exec_primaries == r.expected_exec_primaries
                )
            ),
            total,
        ),
        "partial_node_recall": avg([r.node_recall for r in rows]),
        "sequence_order_accuracy": _safe_div(
            sum(1 for r in seq_rows if r.sequence_ok), len(seq_rows)
        ),
        "f1_macro": avg(f1s),
        "f1_micro": micro_f1,
        "precision_macro": avg(
            [per_class[lab]["precision"] for lab in labels if per_class[lab]["support"] > 0]
        ),
        "recall_macro": avg(
            [per_class[lab]["recall"] for lab in labels if per_class[lab]["support"] > 0]
        ),
        "false_split_rate": _safe_div(sum(1 for r in rows if r.false_split), total),
        "false_merge_rate": _safe_div(sum(1 for r in rows if r.false_merge), total),
        "clarify_precision": clarify_stats["precision"],
        "clarify_recall": clarify_stats["recall"],
        "false_clarify_rate": _safe_div(fp_c, total),
        "missed_clarify_rate": _safe_div(fn_c, total),
        "avg_confidence": avg([r.confidence for r in rows]),
        "avg_confidence_correct": avg([r.confidence for r in correct_rows]),
        "avg_confidence_wrong": avg([r.confidence for r in wrong_rows]),
        "ece": round(ece, 6),
        "overconfident_error_rate": _safe_div(
            sum(1 for r in wrong_rows if r.confidence >= TAU_EXEC_DEFAULT),
            len(labeled_primary),
        ),
        "underconfident_correct_rate": _safe_div(
            sum(1 for r in correct_rows if r.confidence < TAU_CLARIFY_DEFAULT),
            len(labeled_primary),
        ),
        "slot_hit_rates": slot_f1,
        "latency_ms_p50": pct(50),
        "latency_ms_p95": pct(95),
        "latency_ms_p99": pct(99),
        "by_channel": by_channel,
        "by_stratum": by_stratum,
        "weighted_misroute_rate": weighted_misroute_rate,
        "weighted_severe_misroute_rate": weighted_severe_misroute_rate,
        "in_distribution_misroute_rate": in_distribution_misroute_rate,
        "heldout_gap_misroute_rate": heldout_gap_misroute_rate,
        "distribution_note": (
            "weighted_* uses case.weight to approximate traffic mix; "
            "in_distribution_* excludes stratum=heldout_gap; "
            "unweighted misroute_rate mixes regression + held-out stress"
        ),
    }
    from agent_runtime.intent.slo import evaluate_eval_slo

    summary["slo"] = evaluate_eval_slo(summary)

    return {
        "summary": summary,
        "per_class": per_class,
        "confusion": confusion,
        "cases": [r.to_dict() for r in rows],
    }


def run_intent_eval(
    cases_path: Path | None = None,
    *,
    router: IntentRouter | None = None,
) -> dict[str, Any]:
    cases = load_eval_cases(cases_path)
    router = router or IntentRouter()
    rows = [evaluate_case(c, router) for c in cases]
    return compute_intent_metrics(rows)


def write_intent_eval_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def format_summary_table(summary: dict[str, Any]) -> str:
    keys = [
        "total",
        "primary_accuracy",
        "misroute_rate",
        "severe_misroute_rate",
        "mode_accuracy",
        "action_accuracy",
        "exact_graph_match_rate",
        "f1_macro",
        "f1_micro",
        "false_split_rate",
        "false_merge_rate",
        "clarify_precision",
        "clarify_recall",
        "ece",
        "overconfident_error_rate",
        "latency_ms_p50",
        "latency_ms_p95",
        "weighted_misroute_rate",
        "weighted_severe_misroute_rate",
        "in_distribution_misroute_rate",
        "heldout_gap_misroute_rate",
    ]
    lines = ["Intent Router Offline Eval Summary", "-" * 40]
    for k in keys:
        if k in summary:
            lines.append(f"{k:28s} {summary[k]}")
    if summary.get("slot_hit_rates"):
        lines.append(f"{'slot_hit_rates':28s} {summary['slot_hit_rates']}")
    if summary.get("by_channel"):
        lines.append(f"{'by_channel':28s} {summary['by_channel']}")
    if summary.get("by_stratum"):
        lines.append("by_stratum:")
        for st, stats in summary["by_stratum"].items():
            lines.append(f"  {st:22s} {stats}")
    if summary.get("distribution_note"):
        lines.append(f"note: {summary['distribution_note']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Offline Intent Router evaluation")
    parser.add_argument("--cases", type=Path, default=None)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("artifacts/intent_eval_report.json"),
    )
    args = parser.parse_args(argv)
    report = run_intent_eval(args.cases)
    print(format_summary_table(report["summary"]))
    write_intent_eval_report(report, args.out)
    print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
