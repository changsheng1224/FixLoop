"""失败面利用：把 verify 失败变成下一轮可执行的读改跑目标。

能力向（非单例）：
- 抽取失败 nodeid / 断言行
- 从磁盘截取失败测试函数原文
- 注入 patcher 提示与反馈
- 收窄下一轮 pytest target 到失败用例
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.state import CandidatePatch, RepairState, VerificationResult

__all__ = [
    "FailSurface",
    "VerifyFeedbackPayload",
    "apply_verify_feedback_to_state",
    "build_fail_surface",
    "build_fail_surface_prompt_block",
    "build_verify_feedback_payload",
    "preferred_verify_targets",
    "prioritize_failed_nodeids",
    "read_test_function_excerpt",
    "render_verify_feedback_block",
]

_ASSERT_LINE_RE = re.compile(
    r"(AssertionError|E\s+assert\b|^\s*assert\b|Error:|TypeError:|ValueError:|"
    r"KeyError:|AttributeError:|IndexError:)",
    re.IGNORECASE | re.MULTILINE,
)
_DEF_RE = re.compile(r"^(\s*)def\s+([A-Za-z_]\w*)\s*\(")


@dataclass
class FailSurface:
    nodeids: list[str] = field(default_factory=list)
    assertions: list[str] = field(default_factory=list)
    test_excerpts: dict[str, str] = field(default_factory=dict)
    verify_target: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.nodeids or self.assertions or self.test_excerpts)


@dataclass
class VerifyFeedbackPayload:
    """Structured verifier feedback for the next patcher turn."""

    bucket: str = ""
    reason: str = ""
    failing_tests: list[str] = field(default_factory=list)
    assertions: list[str] = field(default_factory=list)
    verify_target: str = ""
    patch_files: list[str] = field(default_factory=list)
    next_action: str = ""

    def to_dict(self) -> dict:
        return {
            "bucket": self.bucket,
            "reason": self.reason,
            "failing_tests": list(self.failing_tests),
            "assertions": list(self.assertions),
            "verify_target": self.verify_target,
            "patch_files": list(self.patch_files),
            "next_action": self.next_action,
        }


def prioritize_failed_nodeids(state: RepairState, nodeids: list[str]) -> None:
    """把失败 nodeid 插到 related_tests 最前，便于下一轮验证与预读。"""
    if not nodeids:
        return
    ctx = getattr(state, "retrieved_context", None)
    if ctx is None:
        from src.state import RetrievedContext

        state.retrieved_context = RetrievedContext()
        ctx = state.retrieved_context
    existing = [str(x) for x in (ctx.related_tests or [])]
    ordered: list[str] = []
    seen: set[str] = set()
    for nid in list(nodeids) + existing:
        if nid and nid not in seen:
            seen.add(nid)
            ordered.append(nid)
    ctx.related_tests = ordered
    state.node_timings["verify_failed_nodeids"] = list(nodeids)[:8]


def preferred_verify_targets(state: RepairState) -> list[str]:
    """下一轮 pytest target：失败 nodeid 优先。"""
    raw = state.node_timings.get("verify_failed_nodeids") or []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        s = str(item).strip().replace("\\", "/")
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    ctx = getattr(state, "retrieved_context", None)
    if ctx:
        for item in ctx.related_tests or []:
            s = str(item).strip().replace("\\", "/")
            if s and s not in seen:
                seen.add(s)
                out.append(s)
    return out


def _extract_assertions(logs: list[str] | None, *, limit: int = 8) -> list[str]:
    if not logs:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for raw in logs:
        for line in str(raw).splitlines():
            stripped = line.strip()
            if not stripped or len(stripped) < 4:
                continue
            if _ASSERT_LINE_RE.search(stripped) or stripped.startswith("E "):
                key = stripped[:240]
                if key not in seen:
                    seen.add(key)
                    found.append(key)
                    if len(found) >= limit:
                        return found
    # 兜底：取短失败行
    if not found:
        for raw in logs[:3]:
            line = str(raw).strip().splitlines()[0][:240]
            if line and line not in seen:
                found.append(line)
    return found[:limit]


def read_test_function_excerpt(
    repo_root: str | Path,
    nodeid: str,
    *,
    max_lines: int = 60,
) -> str:
    """按 pytest nodeid 截取测试函数正文（含装饰器）。"""
    root = Path(repo_root)
    rel = nodeid.split("::", 1)[0].replace("\\", "/").lstrip("./")
    func = ""
    if "::" in nodeid:
        parts = nodeid.split("::")
        func = parts[-1].split("[", 1)[0].strip()
    path = root / rel
    if not path.is_file() or not func:
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ""

    start = -1
    def_line = -1
    indent = ""
    for i, line in enumerate(lines):
        m = _DEF_RE.match(line)
        if m and m.group(2) == func:
            def_line = i
            start = i
            indent = m.group(1)
            # 向上吞装饰器
            j = i - 1
            while j >= 0 and lines[j].lstrip().startswith("@"):
                start = j
                j -= 1
            break
    if start < 0 or def_line < 0:
        return ""

    end = def_line + 1
    while end < len(lines):
        line = lines[end]
        outdented = (
            line.strip()
            and not line.startswith(indent + " ")
            and not line.startswith(indent + "\t")
        )
        if outdented:
            m = _DEF_RE.match(line)
            if m and m.group(1) == indent:
                break
            if line.startswith("class ") and not line.startswith(indent):
                break
            if not line.startswith(indent) and line.strip():
                break
        end += 1
        if end - start >= max_lines:
            break
    excerpt = "\n".join(lines[start:end])
    if end - start >= max_lines:
        excerpt += "\n# ... truncated ..."
    return excerpt


def build_fail_surface(
    state: RepairState,
    *,
    repo_root: str = "",
    result: VerificationResult | None = None,
) -> FailSurface:
    """从 state / 最近 verify 结果构建失败面。"""
    vr = result if result is not None else getattr(state, "verification_result", None)
    nodeids = list(state.node_timings.get("verify_failed_nodeids") or [])
    assertions: list[str] = []
    if vr is not None:
        from src.repair.verification.verify_diagnose import diagnose_verification

        diag = diagnose_verification(vr)
        if diag.failed_nodeids:
            # 诊断结果优先
            merged: list[str] = []
            seen: set[str] = set()
            for n in list(diag.failed_nodeids) + nodeids:
                if n not in seen:
                    seen.add(n)
                    merged.append(n)
            nodeids = merged
        assertions = _extract_assertions(vr.failure_logs)

    excerpts: dict[str, str] = {}
    root = repo_root or ""
    if root:
        for nid in nodeids[:3]:
            text = read_test_function_excerpt(root, nid)
            if text:
                excerpts[nid] = text

    target = nodeids[0] if nodeids else ""
    return FailSurface(
        nodeids=nodeids[:8],
        assertions=assertions,
        test_excerpts=excerpts,
        verify_target=target,
    )


def build_fail_surface_prompt_block(
    surface: FailSurface,
    *,
    max_chars: int = 3500,
    bucket: str = "",
) -> str:
    """生成注入 patcher / feedback 的失败面文本块。

    ``bucket=env`` 时禁止引导「读测试改业务」——那是 R8 django 误导源。
    """
    bucket_l = (bucket or "").strip().lower()
    if bucket_l == "env":
        lines = [
            "[环境失败 ENV — 非业务断言]",
            "禁止：对着 sandbox/依赖错误去改业务源码或重复同一 diff。",
            "允许：确认测试入口、导入路径、settings；环境未恢复前不要堆叠逻辑补丁。",
        ]
        if surface.assertions:
            lines.append("环境相关日志:")
            for a in surface.assertions[:6]:
                lines.append(f"  - {a}")
        elif surface.nodeids:
            lines.append("相关引用（未必是断言失败）:")
            for n in surface.nodeids[:3]:
                lines.append(f"  - {n}")
        text = "\n".join(lines)
        return text[:max_chars]

    if not surface.usable:
        return ""
    lines = ["[失败面 FAIL SURFACE]", "下一轮必须先读失败测试，再对照断言改实现；勿盲目扩搜。"]
    if surface.verify_target:
        lines.append(f"优先验证目标: {surface.verify_target}")
    if surface.nodeids:
        lines.append("失败用例:")
        for n in surface.nodeids[:5]:
            lines.append(f"  - {n}")
    if surface.assertions:
        lines.append("关键断言/异常:")
        for a in surface.assertions[:6]:
            lines.append(f"  - {a}")
    for nid, excerpt in list(surface.test_excerpts.items())[:2]:
        lines.append(f"失败测试原文 ({nid}):")
        lines.append("```python")
        lines.append(excerpt)
        lines.append("```")
    if bucket_l == "collect":
        lines.append(
            "动作: 先根据收集日志修导入/目标路径；确认能收集到测试后再 patch 业务逻辑。"
        )
    else:
        lines.append(
            "动作: 1) read_file 打开上列测试 2) 定位被测实现 3) apply_patch 最小修改 "
            "4) 保持验证目标不变直至该 nodeid 通过。"
        )
    text = "\n".join(lines)
    return text[:max_chars]


def _patch_files(patches: list[CandidatePatch] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for patch in patches or []:
        fp = str(getattr(patch, "file_path", "") or "").replace("\\", "/")
        if fp and fp not in seen:
            seen.add(fp)
            out.append(fp)
        if len(out) >= 8:
            break
    return out


def _next_action_for_bucket(bucket: str, surface: FailSurface) -> str:
    bucket_l = (bucket or "").strip().lower()
    if bucket_l == "env":
        return "stop_business_patch_and_fix_verify_environment_or_entrypoint"
    if bucket_l == "collect":
        return "fix_collection_or_import_path_before_business_logic"
    if surface.verify_target:
        return "read_failed_test_then_patch_minimal_impl_and_reverify_same_target"
    return "inspect_failure_logs_then_patch_minimal_impl"


def build_verify_feedback_payload(
    state: RepairState,
    *,
    repo_root: str = "",
    result: VerificationResult | None = None,
) -> VerifyFeedbackPayload:
    """Build reusable verifier feedback without encoding case-specific hints."""
    from src.repair.verification.verify_diagnose import diagnose_verification

    vr = result if result is not None else getattr(state, "verification_result", None)
    diag = diagnose_verification(vr) if vr is not None else None
    bucket = diag.bucket.value if diag is not None else ""
    reason = diag.reason if diag is not None else ""
    surface = build_fail_surface(state, repo_root=repo_root, result=vr)
    failing_tests = list(surface.nodeids)
    if diag is not None and diag.failed_nodeids:
        seen = set(failing_tests)
        for nodeid in diag.failed_nodeids:
            if nodeid not in seen:
                seen.add(nodeid)
                failing_tests.append(nodeid)
    return VerifyFeedbackPayload(
        bucket=bucket,
        reason=reason,
        failing_tests=failing_tests[:8],
        assertions=list(surface.assertions[:8]),
        verify_target=surface.verify_target,
        patch_files=_patch_files(getattr(state, "candidate_patches", None)),
        next_action=_next_action_for_bucket(bucket, surface),
    )


def apply_verify_feedback_to_state(
    state: RepairState,
    payload: VerifyFeedbackPayload,
) -> None:
    """Persist structured verifier feedback for prompts, trace, and reports."""
    data = payload.to_dict()
    state.node_timings["structured_verify_feedback"] = data
    state.node_timings["verify_feedback_next_action"] = payload.next_action
    if payload.verify_target:
        state.node_timings["verify_feedback_target"] = payload.verify_target


def render_verify_feedback_block(
    payload: VerifyFeedbackPayload | dict,
    *,
    max_chars: int = 1800,
) -> str:
    """Render a compact, stable feedback block for the patcher."""
    data = payload.to_dict() if isinstance(payload, VerifyFeedbackPayload) else dict(payload)
    if not any(data.values()):
        return ""
    bucket = str(data.get("bucket") or "")
    reason = str(data.get("reason") or "")
    verify_target = str(data.get("verify_target") or "")
    next_action = str(data.get("next_action") or "")
    failing_tests = list(data.get("failing_tests") or [])
    assertions = list(data.get("assertions") or [])
    patch_files = list(data.get("patch_files") or [])
    lines = ["[结构化验证反馈]"]
    if bucket or reason:
        lines.append(f"bucket={bucket}; reason={reason}")
    if verify_target:
        lines.append(f"verify_target={verify_target}")
    if next_action:
        lines.append(f"next_action={next_action}")
    if failing_tests:
        lines.append("failing_tests:")
        for item in failing_tests[:5]:
            lines.append(f"  - {item}")
    if assertions:
        lines.append("assertions:")
        for item in assertions[:5]:
            lines.append(f"  - {item}")
    if patch_files:
        lines.append("previous_patch_files: " + ", ".join(str(x) for x in patch_files[:6]))
    return "\n".join(lines)[:max_chars]
