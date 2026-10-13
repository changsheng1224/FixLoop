"""Verification feedback assembly with explicit source and event dependencies."""

import hashlib
import re
from collections import namedtuple
from pathlib import Path

from src.state import CandidatePatch, RepairState, VerificationResult

# 反馈环结构化 section（V1.4-Bonus8a）
_FeedbackSection = namedtuple("_FeedbackSection", ["title", "content", "priority", "max_len"])
_MAX_PATCH_DIFF_CHARS = 600
_MAX_FAILURE_LOG_CHARS = 300
_MAX_BUILD_LOG_CHARS = 300
_MAX_FAILURE_TARGET_CHARS = 500


def _summarize_failure_targets(failure_logs: list[str]) -> str:
    if not failure_logs:
        return ""
    test_ids: list[str] = []
    files: list[str] = []
    assertions: list[str] = []

    def add_unique(items: list[str], value: str) -> None:
        value = value.strip()
        if value and value not in items:
            items.append(value)

    for raw in failure_logs[:5]:
        line = str(raw)
        for match in re.finditer(r"\b(?:FAILED\s+)?([A-Za-z0-9_./\\-]+\.py::[^\s:]+)", line):
            add_unique(test_ids, match.group(1).replace("\\", "/"))
        for match in re.finditer(r'File\s+"([^"]+\.py)"', line):
            add_unique(files, Path(match.group(1)).name)
        for match in re.finditer(r"\b([A-Za-z0-9_./\\-]+\.py)\b", line):
            add_unique(files, Path(match.group(1)).name)
        if any(key in line for key in ("assert", "AssertionError", "ValueError", "TypeError")):
            add_unique(assertions, line[:160])

    parts: list[str] = []
    if test_ids:
        parts.append("失败用例: " + ", ".join(test_ids[:3]))
    if files:
        parts.append("相关文件: " + ", ".join(files[:5]))
    if assertions:
        parts.append("关键断言/异常: " + " | ".join(assertions[:2]))
    return "\n".join(parts)


def _patch_retry_fingerprint(patch: CandidatePatch) -> str:
    body = "\n".join(
        [
            patch.file_path or "",
            patch.diff or "",
            patch.original_lines or "",
            patch.patched_lines or "",
        ]
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _record_patch_retry_fingerprints(state: RepairState) -> str:
    raw_counts = state.control.patch_retry_fingerprints
    if not isinstance(raw_counts, dict):
        raw_counts = {}
        state.control.patch_retry_fingerprints = raw_counts

    repeated_files: list[str] = []
    for patch in state.candidate_patches:
        fp = _patch_retry_fingerprint(patch)
        count = int(raw_counts.get(fp, 0) or 0) + 1
        raw_counts[fp] = count
        if count >= 2:
            repeated_files.append(patch.file_path)

    if not repeated_files:
        return ""
    files = ", ".join(sorted(set(repeated_files))[:5])
    return (
        f"检测到重复补丁文件: {files}。\n"
        "不要重复生成上轮相同 diff；请基于失败测试换策略，"
        "优先修改仍失败断言直接相关的表达式或边界处理。"
    )


class RepairFeedbackService:
    def __init__(self, repo_root, sandbox_context=None, emit=None):
        self.repo_root = repo_root
        self.sandbox_context = sandbox_context
        self.emit = emit

    def build(self, result: "VerificationResult", *, state: RepairState | None = None) -> str:
        """构建结构化反馈文本（V1.4-Bonus8a）。

        Sections（按优先级排列）:
        1. regression_hint — 上轮引入回归时的回滚提示
        2. previous_patches — 上轮失败的 patch diff
        3. failure_logs — 失败测试日志
        4. build_log — 构建日志
        5. guidance — 后续操作指导
        """
        sections: list[_FeedbackSection] = []

        # 1. regression hint
        if state is not None:
            from src.repair.verification.termination import introduced_regression

            if introduced_regression(state):
                sections.append(
                    _FeedbackSection(
                        "回滚提示",
                        "上轮补丁引入了回归（原本通过的测试现在失败）。"
                        "请先还原受影响的无关文件，只修改 suspect 范围内的文件。",
                        10,
                        200,
                    )
                )

        # E6a: apply 失败细节写入 feedback，供下一轮 patcher 使用
        if state is not None and state.agent_errors.get("patcher_apply"):
            sections.append(
                _FeedbackSection(
                    "Apply 失败",
                    str(state.agent_errors["patcher_apply"])[:500],
                    18,
                    500,
                )
            )
        if state is not None and state.agent_errors.get("incomplete_sibling_pattern"):
            sections.append(
                _FeedbackSection(
                    "Sibling 模式",
                    str(state.agent_errors["incomplete_sibling_pattern"])[:400],
                    19,
                    400,
                )
            )

        # 2. previous patches
        if state is not None and state.candidate_patches:
            patch_lines: list[str] = []
            for i, p in enumerate(state.candidate_patches[:2], 1):
                diff = p.diff[:_MAX_PATCH_DIFF_CHARS] if p.diff else "(空)"
                patch_lines.append(f"补丁{i} ({p.file_path}):\n{diff}")
            if patch_lines:
                sections.append(
                    _FeedbackSection(
                        "上轮改动",
                        "\n\n".join(patch_lines),
                        20,
                        _MAX_PATCH_DIFF_CHARS * 2,
                    )
                )

            repeat_feedback = _record_patch_retry_fingerprints(state)
            if repeat_feedback:
                sections.append(_FeedbackSection("重复补丁", repeat_feedback, 22, 500))

        from src.repair.verification.verify_diagnose import VerifyBucket, diagnose_verification

        diagnosis = diagnose_verification(result)
        if state is not None:
            state.control.verify_bucket = diagnosis.bucket.value
            state.node_timings["verify_bucket_reason"] = diagnosis.reason
            if diagnosis.failed_nodeids:
                state.control.verify_failed_nodeids = list(diagnosis.failed_nodeids)
            from src.repair.verification.failure_decision import (
                apply_failure_decision,
                decide_verification_failure,
                render_failure_decision,
            )

            failure_decision = decide_verification_failure(result, state=state)
            apply_failure_decision(state, failure_decision)
            if self.emit is not None:
                self.emit(
                    "orchestrator",
                    "repair_failure_decision",
                    failure_decision.to_dict(),
                    status="error",
                )
            sections.append(
                _FeedbackSection(
                    "修复决策",
                    render_failure_decision(failure_decision),
                    11,
                    700,
                )
            )
            from src.repair.verification.fail_surface import (
                apply_verify_feedback_to_state,
                build_verify_feedback_payload,
                render_verify_feedback_block,
            )

            verify_payload = build_verify_feedback_payload(
                state,
                repo_root=self.repo_root,
                result=result,
                sandbox_context=self.sandbox_context,
            )
            apply_verify_feedback_to_state(state, verify_payload)
            structured_block = render_verify_feedback_block(verify_payload)
            if structured_block:
                sections.append(_FeedbackSection("结构化反馈", structured_block, 13, 1800))

        sections.append(
            _FeedbackSection(
                "验证分桶",
                f"bucket={diagnosis.bucket.value}; reason={diagnosis.reason}",
                12,
                200,
            )
        )
        if diagnosis.failed_nodeids:
            sections.append(
                _FeedbackSection(
                    "失败用例",
                    "\n".join(f"  - {n}" for n in diagnosis.failed_nodeids[:5]),
                    23,
                    400,
                )
            )

        # 失败面：断言 + 测试原文摘要（供下一轮 toolized patcher）
        if state is not None:
            from src.repair.verification.fail_surface import (
                build_fail_surface,
                build_fail_surface_prompt_block,
            )

            surface = build_fail_surface(
                state,
                repo_root=self.repo_root,
                result=result,
                sandbox_context=self.sandbox_context,
            )
            fail_block = build_fail_surface_prompt_block(
                surface,
                max_chars=2500,
                bucket=diagnosis.bucket.value,
            )
            if fail_block:
                sections.append(_FeedbackSection("失败面", fail_block, 27, 2500))

            from src.repair.failure_ledger import build_ledger_prompt_block, load_ledger_from_state

            ledger_block = build_ledger_prompt_block(load_ledger_from_state(state))
            if ledger_block:
                sections.append(_FeedbackSection("失败账本", ledger_block, 28, 2200))

        target_feedback = _summarize_failure_targets(result.failure_logs)
        if target_feedback:
            sections.append(
                _FeedbackSection(
                    "失败定位",
                    target_feedback,
                    24,
                    _MAX_FAILURE_TARGET_CHARS,
                )
            )

        # 3. failure logs
        if result.failure_logs:
            logs = "\n".join(
                f"  - {log[:_MAX_FAILURE_LOG_CHARS]}" for log in result.failure_logs[:5]
            )
            title = "失败测试"
            if diagnosis.bucket == VerifyBucket.ENV:
                title = "验证环境"
            elif diagnosis.bucket == VerifyBucket.COLLECT:
                title = "收集失败"
            elif "verify_config:" in "\n".join(str(x) for x in result.failure_logs):
                title = "验证配置"
            sections.append(_FeedbackSection(title, logs, 30, _MAX_FAILURE_LOG_CHARS * 5))

        # 4. build log
        if result.build_log:
            sections.append(
                _FeedbackSection(
                    "构建日志",
                    result.build_log[:_MAX_BUILD_LOG_CHARS],
                    25,
                    _MAX_BUILD_LOG_CHARS,
                )
            )

        # 按 priority 排序后组装
        sections.sort(key=lambda s: s.priority)
        lines = ["补丁验证失败。"]
        for sec in sections:
            text = sec.content[: sec.max_len] if sec.content else ""
            if text:
                lines.append(f"\n[{sec.title}]\n{text}")

        # 5. guidance（按分桶给出可执行下一步）
        guide_parts: list[str] = []
        if diagnosis.guidance:
            guide_parts.append(diagnosis.guidance)
        if diagnosis.bucket == VerifyBucket.LOGIC:
            guide_parts.append(
                "使用 read_file / apply_patch 先读失败测试再改实现，完成后简短说明。"
            )
        elif diagnosis.bucket == VerifyBucket.ENV:
            guide_parts.append(
                "环境未就绪时停止堆叠同类业务补丁；若仍重试，只允许修正测试入口或导入路径相关改动。"
            )
        if state is not None and state.retry_count > 0:
            guide_parts.append(
                f"已尝试 {state.retry_count + 1} 次。不要重复上轮相同 diff；只改失败相关文件。"
            )
        elif not guide_parts:
            guide_parts.append(
                "请根据以上日志修改补丁。使用 apply_patch 直接修改文件，完成后简短说明。"
            )
        lines.append("\n[指导]\n" + " ".join(guide_parts))
        return "\n".join(lines)
