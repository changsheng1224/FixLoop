"""可执行 Skill Router 离线评测。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.skills.registry import get_default_executable_registry
from src.skills.router import SkillRouter

_CASES_DIR = Path(__file__).with_name("eval_cases")
_DEFAULT_CASES = _CASES_DIR / "router_cases.yaml"
_HARD_CASES = _CASES_DIR / "router_cases_hard.yaml"
_HELDOUT_CASES = _CASES_DIR / "router_cases_heldout.yaml"


@dataclass
class RouterEvalCase:
    id: str
    text: str
    expect: str | None  # skill name or null for fallback
    tags: list[str] = field(default_factory=list)
    previous: str | None = None  # previous_selected for switch scenarios


@dataclass
class RouterEvalReport:
    n: int
    top1: float
    mis_trigger: float
    miss_trigger: float
    fallback_rate: float
    low_margin_rate: float
    skill_switch_rate: float
    by_skill: dict[str, dict[str, float]] = field(default_factory=dict)
    by_tag: dict[str, dict[str, float]] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "top1": self.top1,
            "mis_trigger": self.mis_trigger,
            "miss_trigger": self.miss_trigger,
            "fallback_rate": self.fallback_rate,
            "low_margin_rate": self.low_margin_rate,
            "skill_switch_rate": self.skill_switch_rate,
            "by_skill": self.by_skill,
            "by_tag": self.by_tag,
        }


def _load_one(path: Path) -> list[RouterEvalCase]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases_raw = data.get("cases") if isinstance(data, dict) else data
    out: list[RouterEvalCase] = []
    for raw in cases_raw or []:
        prev = raw.get("previous")
        out.append(
            RouterEvalCase(
                id=str(raw.get("id") or f"case-{len(out)+1}"),
                text=str(raw.get("text") or ""),
                expect=raw.get("expect"),
                tags=list(raw.get("tags") or []),
                previous=str(prev) if prev else None,
            )
        )
    return out


def load_heldout_cases() -> list[RouterEvalCase]:
    """仅加载 held-out 诊断集（不含 easy/hard）。"""
    if not _HELDOUT_CASES.is_file():
        return []
    return _load_one(_HELDOUT_CASES)


def load_router_cases(
    path: Path | None = None,
    *,
    include_hard: bool = True,
    include_heldout: bool = False,
) -> list[RouterEvalCase]:
    """加载评测集。

    ``include_heldout`` 默认关闭：held-out 仅作诊断，不为刷分改 Router。
    """
    if path is not None:
        return _load_one(Path(path))
    cases = _load_one(_DEFAULT_CASES)
    if include_hard and _HARD_CASES.is_file():
        cases.extend(_load_one(_HARD_CASES))
    if include_heldout and _HELDOUT_CASES.is_file():
        cases.extend(_load_one(_HELDOUT_CASES))
    return cases


def evaluate_router(
    cases: list[RouterEvalCase] | None = None,
    *,
    router: SkillRouter | None = None,
    previous_by_id: dict[str, str] | None = None,
    include_hard: bool = True,
    include_heldout: bool = False,
) -> RouterEvalReport:
    cases = (
        cases
        if cases is not None
        else load_router_cases(include_hard=include_hard, include_heldout=include_heldout)
    )
    router = router or SkillRouter(registry=get_default_executable_registry())
    previous_by_id = previous_by_id or {}

    top1_ok = 0
    mis = 0
    miss = 0
    fallback_n = 0
    low_margin_n = 0
    switch_n = 0
    switch_denom = 0
    by_skill: dict[str, dict[str, float]] = {}
    by_tag: dict[str, dict[str, float]] = {}
    rows: list[dict[str, Any]] = []

    for case in cases:
        prev = case.previous or previous_by_id.get(case.id)
        decision = router.route(case.text, previous_selected=prev)
        expect = case.expect
        selected = decision.selected
        correct = selected == expect
        if correct:
            top1_ok += 1
        if expect is None and selected is not None:
            mis += 1
        if expect is not None and selected is None:
            miss += 1
        if expect is not None and selected is not None and selected != expect:
            mis += 1
        if decision.fallback or selected is None:
            fallback_n += 1
        if decision.low_margin:
            low_margin_n += 1
        if prev:
            switch_denom += 1
            if decision.switched_from or (selected and selected != prev):
                switch_n += 1

        key = expect or "_fallback"
        bucket = by_skill.setdefault(key, {"n": 0, "hit": 0})
        bucket["n"] += 1
        if correct:
            bucket["hit"] += 1

        for tag in case.tags:
            tb = by_tag.setdefault(tag, {"n": 0, "hit": 0})
            tb["n"] += 1
            if correct:
                tb["hit"] += 1

        rows.append(
            {
                "id": case.id,
                "expect": expect,
                "selected": selected,
                "correct": correct,
                "reason": decision.selection_reason,
                "margin": decision.margin,
                "low_margin": decision.low_margin,
                "previous": prev,
                "switched_from": decision.switched_from,
                "tags": case.tags,
            }
        )

    n = max(len(cases), 1)
    for bucket in list(by_skill.values()) + list(by_tag.values()):
        bucket["recall"] = round(bucket["hit"] / max(bucket["n"], 1), 4)

    return RouterEvalReport(
        n=len(cases),
        top1=round(top1_ok / n, 4),
        mis_trigger=round(mis / n, 4),
        miss_trigger=round(miss / n, 4),
        fallback_rate=round(fallback_n / n, 4),
        low_margin_rate=round(low_margin_n / n, 4),
        skill_switch_rate=round(switch_n / max(switch_denom, 1), 4) if switch_denom else 0.0,
        by_skill=by_skill,
        by_tag=by_tag,
        rows=rows,
    )
