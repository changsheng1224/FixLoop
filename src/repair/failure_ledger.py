"""失败信息账本：假设版本化 + 反例 + 回归缩 scope。

能力向（非单例）：
- 每轮补丁记为 hypothesis，失败断言/哈希记为 counterexample
- 重复失败或换策略时否定假设，并记录已否定文件
- 检测到回归时缩回嫌疑范围，禁止继续改「引入回归」的文件
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.state import CandidatePatch, RepairState, SuspectLocation, VerificationResult

__all__ = [
    "FailureLedger",
    "Hypothesis",
    "apply_ledger_to_state",
    "build_ledger_prompt_block",
    "load_ledger_from_state",
    "record_verify_into_ledger",
    "shrink_suspects_for_regression",
]


def _patch_files(patches: Iterable[CandidatePatch] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for p in patches or []:
        fp = str(getattr(p, "file_path", "") or "").replace("\\", "/")
        if fp and fp not in seen:
            seen.add(fp)
            out.append(fp)
    return out


def _verify_hash(result: VerificationResult | None) -> str:
    if result is None:
        return ""
    parts = [str(x)[:220] for x in list(result.failure_logs or [])[:6]]
    blob = "\n".join(parts)
    if not blob.strip():
        blob = f"t={result.total_tests}:f={result.failed}"
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()[:12]


def _assertions(result: VerificationResult | None, *, limit: int = 4) -> list[str]:
    if result is None:
        return []
    out: list[str] = []
    for raw in list(result.failure_logs or [])[:8]:
        for line in str(raw).splitlines():
            s = line.strip()
            if not s:
                continue
            low = s.lower()
            if (
                "assert" in low
                or s.startswith("E ")
                or "Error" in s
                or "FAILED" in s
            ):
                if s not in out:
                    out.append(s[:220])
            if len(out) >= limit:
                return out
    return out


@dataclass
class Hypothesis:
    id: str
    files: list[str] = field(default_factory=list)
    status: str = "active"  # active | negated | superseded
    counterexamples: list[str] = field(default_factory=list)
    verify_hashes: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "files": list(self.files),
            "status": self.status,
            "counterexamples": list(self.counterexamples)[:6],
            "verify_hashes": list(self.verify_hashes)[:6],
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Hypothesis:
        return cls(
            id=str(raw.get("id") or ""),
            files=[str(x) for x in (raw.get("files") or [])],
            status=str(raw.get("status") or "active"),
            counterexamples=[str(x) for x in (raw.get("counterexamples") or [])],
            verify_hashes=[str(x) for x in (raw.get("verify_hashes") or [])],
            note=str(raw.get("note") or ""),
        )


@dataclass
class FailureLedger:
    """跨 retry 的失败/假设记忆。"""

    next_id: int = 1
    hypotheses: list[Hypothesis] = field(default_factory=list)
    negated_files: list[str] = field(default_factory=list)
    regression_files: list[str] = field(default_factory=list)
    last_verify_hash: str = ""
    last_bucket: str = ""

    def active(self) -> Hypothesis | None:
        for h in reversed(self.hypotheses):
            if h.status == "active":
                return h
        return None

    def open_hypothesis(self, files: list[str], *, note: str = "") -> Hypothesis:
        hid = f"H{self.next_id}"
        self.next_id += 1
        # 旧 active → superseded
        for h in self.hypotheses:
            if h.status == "active":
                h.status = "superseded"
        hyp = Hypothesis(id=hid, files=list(files), status="active", note=note)
        self.hypotheses.append(hyp)
        return hyp

    def negate(self, hyp: Hypothesis, *, reason: str = "") -> None:
        hyp.status = "negated"
        if reason:
            hyp.note = reason
        for fp in hyp.files:
            if fp and fp not in self.negated_files:
                self.negated_files.append(fp)

    def mark_regression(self, files: list[str]) -> None:
        for fp in files:
            if fp and fp not in self.regression_files:
                self.regression_files.append(fp)
            if fp and fp not in self.negated_files:
                self.negated_files.append(fp)

    def forbidden_files(self) -> set[str]:
        return set(self.negated_files) | set(self.regression_files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "next_id": self.next_id,
            "hypotheses": [h.to_dict() for h in self.hypotheses[-8:]],
            "negated_files": list(self.negated_files)[:12],
            "regression_files": list(self.regression_files)[:12],
            "last_verify_hash": self.last_verify_hash,
            "last_bucket": self.last_bucket,
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> FailureLedger:
        if not isinstance(raw, dict):
            return cls()
        hyps = [
            Hypothesis.from_dict(item)
            for item in (raw.get("hypotheses") or [])
            if isinstance(item, dict)
        ]
        return cls(
            next_id=int(raw.get("next_id") or 1),
            hypotheses=hyps,
            negated_files=[str(x) for x in (raw.get("negated_files") or [])],
            regression_files=[str(x) for x in (raw.get("regression_files") or [])],
            last_verify_hash=str(raw.get("last_verify_hash") or ""),
            last_bucket=str(raw.get("last_bucket") or ""),
        )


def apply_ledger_to_state(state: RepairState, ledger: FailureLedger) -> None:
    state.node_timings["failure_ledger"] = ledger.to_dict()


def load_ledger_from_state(state: RepairState) -> FailureLedger:
    return FailureLedger.from_dict(state.node_timings.get("failure_ledger"))


def record_verify_into_ledger(
    state: RepairState,
    *,
    result: VerificationResult | None,
    bucket: str = "",
    is_regression: bool = False,
) -> FailureLedger:
    """verify 失败后更新账本；返回更新后的 ledger。"""
    ledger = load_ledger_from_state(state)
    files = _patch_files(getattr(state, "candidate_patches", None))
    vhash = _verify_hash(result)
    asserts = _assertions(result)
    ledger.last_bucket = bucket or ledger.last_bucket

    if is_regression and files:
        ledger.mark_regression(files)
        hyp = ledger.active()
        if hyp is None or set(hyp.files) != set(files):
            hyp = ledger.open_hypothesis(files, note="regression")
        ledger.negate(hyp, reason="introduced_regression")
        ledger.last_verify_hash = vhash
        apply_ledger_to_state(state, ledger)
        return ledger

    if bucket in ("env", "collect"):
        # 环境失败不否定业务假设，只记哈希
        ledger.last_verify_hash = vhash or ledger.last_verify_hash
        apply_ledger_to_state(state, ledger)
        return ledger

    hyp = ledger.active()
    if files:
        if hyp is None or set(hyp.files) != set(files):
            hyp = ledger.open_hypothesis(files, note="patch_attempt")
    if hyp is None:
        apply_ledger_to_state(state, ledger)
        return ledger

    for a in asserts:
        if a not in hyp.counterexamples:
            hyp.counterexamples.append(a)
    if vhash and vhash not in hyp.verify_hashes:
        hyp.verify_hashes.append(vhash)

    # 同一假设连续撞上相同失败面 → 否定
    if vhash and vhash == ledger.last_verify_hash and hyp.status == "active":
        ledger.negate(hyp, reason="repeated_counterexample")
    elif len(hyp.verify_hashes) >= 2 and len(set(hyp.verify_hashes[-2:])) == 1:
        ledger.negate(hyp, reason="stagnant_verify_hash")

    ledger.last_verify_hash = vhash or ledger.last_verify_hash
    apply_ledger_to_state(state, ledger)
    return ledger


def shrink_suspects_for_regression(
    suspects: list[SuspectLocation] | None,
    ledger: FailureLedger,
) -> list[SuspectLocation]:
    """回归后：把引入回归的文件降到末尾，优先其它嫌疑。"""
    forbidden = ledger.forbidden_files()
    if not suspects:
        return []
    if not forbidden:
        return list(suspects)
    keep: list = []
    demoted: list = []
    for s in suspects:
        fp = str(getattr(s, "file_path", "") or "").replace("\\", "/")
        if fp in forbidden:
            demoted.append(s)
        else:
            keep.append(s)
    return keep + demoted


def build_ledger_prompt_block(ledger: FailureLedger, *, max_chars: int = 2200) -> str:
    if not ledger.hypotheses and not ledger.negated_files and not ledger.regression_files:
        return ""
    lines = ["[失败账本 FAILURE LEDGER]", "利用历史失败，禁止重复已否定假设。"]
    active = ledger.active()
    if active:
        lines.append(
            f"当前假设 {active.id}: files={', '.join(active.files) or '(none)'}; "
            f"status={active.status}"
        )
        if active.counterexamples:
            lines.append("反例/断言:")
            for c in active.counterexamples[:4]:
                lines.append(f"  - {c}")
    negated = [h for h in ledger.hypotheses if h.status == "negated"][-3:]
    if negated:
        lines.append("已否定假设:")
        for h in negated:
            lines.append(
                f"  - {h.id} files={', '.join(h.files)}; reason={h.note or 'negated'}"
            )
    if ledger.regression_files:
        lines.append(
            "回归源文件（本轮勿再改，除非有更强证据）: "
            + ", ".join(ledger.regression_files[:6])
        )
    if ledger.negated_files:
        lines.append(
            "已否定文件: " + ", ".join(ledger.negated_files[:8])
        )
    lines.append(
        "动作: 若当前假设被否定，换文件/换符号；对照反例断言做最小修改；"
        "回归时先缩 scope，不要继续堆叠同一文件 diff。"
    )
    return "\n".join(lines)[:max_chars]
