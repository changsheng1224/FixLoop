"""Memory Candidate schema：规则/LLM 双路抽取 + 冲突门控 + 路径隔离。

规则路径：从 stack trace、工具结果、最终答案中按前缀/正则抽取。
LLM 路径：light_client 仅填 kind/confidence 规划字段，禁止自由建 topic。
写 durable 前走冲突状态机（与 durable._resolve_conflict 共用管线）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# ── 路径隔离 ──


class MemoryPathError(ValueError):
    """记忆系统路径越界或逃逸异常。"""

    def __init__(self, raw_path: str, detail: str = ""):
        self.raw_path = raw_path
        self.detail = detail
        msg = f"Memory path 越界: {raw_path}"
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)


def resolve_memory_path(root: str | Path, raw_path: str) -> Path:
    """解析记忆系统路径，含 `..` 或越界抛 MemoryPathError。

    Args:
        root: workspace 根目录（如 /repo/.agent/memory）。
        raw_path: 用户/外部输入的相对路径。

    Returns:
        位于 root 内的 canonical 绝对路径。

    Raises:
        MemoryPathError: 路径含 `..` 逃逸、绝对路径越界或为空。
    """
    from agent_runtime.path_safety import resolve_under_root

    try:
        return resolve_under_root(root, raw_path)
    except ValueError as e:
        raise MemoryPathError(raw_path, detail=str(e)) from e


# ── 候选数据结构 ──

CANDIDATE_KINDS = ("error", "decision", "observation", "fact")
CandidateKind = Literal["error", "decision", "observation", "fact"]

ALLOWED_TOPICS = frozenset(
    {
        "project-conventions",
        "key-decisions",
        "dependency-facts",
        "user-preferences",
    }
)


@dataclass
class Candidate:
    """结构化记忆候选条目。

    topic 必须来自 ALLOWED_TOPICS，禁止自由创建。
    """

    topic: str
    key: str
    value: str
    kind: CandidateKind = "observation"
    confidence: float = 0.5
    source: str = ""

    def __post_init__(self):
        if self.topic not in ALLOWED_TOPICS:
            raise ValueError(f"非法 topic '{self.topic}'，允许值: {sorted(ALLOWED_TOPICS)}")
        if self.kind not in CANDIDATE_KINDS:
            raise ValueError(f"非法 kind '{self.kind}'，允许值: {CANDIDATE_KINDS}")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence 必须在 0.0~1.0，实际: {self.confidence}")
        if not self.key.strip():
            raise ValueError("key 不能为空")
        if not self.value.strip():
            raise ValueError("value 不能为空")

    @property
    def promotion(self) -> tuple[str, str]:
        """转为 durable.promote() 所需的 (topic, text) 格式。"""
        text = (
            f"{self.key}\n"
            f"kind={self.kind} confidence={self.confidence:.2f} source={self.source}\n"
            f"{self.value}"
        )
        return (self.topic, text)


# ── 规则抽取 ──

_STACK_FILE_RE = re.compile(r'File\s+"([^"]+)",\s*line\s+(\d+)')
_ERROR_TYPE_RE = re.compile(
    r"(TypeError|ValueError|ImportError|AttributeError|KeyError|"
    r"ModuleNotFoundError|SyntaxError|NameError|IndexError)"
)
_STACK_BODY_RE = re.compile(r"^\s{2,}(.+)", re.MULTILINE)


def extract_from_stack(traceback_text: str) -> list[Candidate]:
    """从 Python traceback 文本中规则抽取 error/fact 候选。"""
    result: list[Candidate] = []
    if not traceback_text:
        return result

    files = _STACK_FILE_RE.findall(traceback_text)
    error_match = _ERROR_TYPE_RE.search(traceback_text)
    error_type = error_match.group(1) if error_match else "UnknownError"

    for file_path, line_no in files[:3]:
        result.append(
            Candidate(
                topic="key-decisions",
                key=f"error:{file_path}:{line_no}",
                value=f"{error_type} at {file_path}:{line_no}",
                kind="error",
                confidence=0.8,
                source="stack_parse",
            )
        )

    # 提取堆栈中的代码片段
    import hashlib

    body_matches = _STACK_BODY_RE.findall(traceback_text)
    for snippet in body_matches[:2]:
        snippet = snippet.strip()
        if len(snippet) > 10 and not snippet.startswith("..."):
            sid = hashlib.sha256(snippet.encode()).hexdigest()[:8]
            result.append(
                Candidate(
                    topic="project-conventions",
                    key=f"stack_snippet:{sid}",
                    value=f"堆栈代码: {snippet[:150]}",
                    kind="observation",
                    confidence=0.6,
                    source="stack_parse",
                )
            )

    return result


def extract_from_tool_result(
    tool_name: str,
    tool_args: dict,
    result_text: str,
) -> list[Candidate]:
    """从工具执行结果中规则抽取候选。"""
    result: list[Candidate] = []
    if not result_text:
        return result

    path = tool_args.get("path", "") or tool_args.get("file_path", "") or ""

    # 错误检测
    error_keywords = ["Error:", "Traceback", "FAILED", "error:", "assert"]
    if any(kw in result_text for kw in error_keywords):
        first_line = result_text.split("\n")[0][:150]
        result.append(
            Candidate(
                topic="key-decisions",
                key=f"tool_error:{tool_name}:{hash(first_line) & 0xFFFF:04x}",
                value=f"{tool_name} 错误: {first_line}",
                kind="error",
                confidence=0.7,
                source=tool_name,
            )
        )

    # git blame → dependency facts
    if tool_name in ("git_blame", "search") and path:
        blob = result_text[:200].strip()
        if blob:
            result.append(
                Candidate(
                    topic="dependency-facts",
                    key=f"tool:{tool_name}:{path}",
                    value=f"{tool_name}({path}): {blob[:120]}",
                    kind="fact",
                    confidence=0.5,
                    source=tool_name,
                )
            )

    return result


def extract_from_final_answer(answer: str, issue: str = "") -> list[Candidate]:
    """从最终答案中规则抽取 decision/convention 候选。"""
    result: list[Candidate] = []
    if not answer:
        return result

    # 决策关键词检测
    decision_words = ["修复", "fix", "patch", "修改", "changed", "原因", "root cause"]
    if any(w in answer.lower() for w in decision_words):
        # 取第一句有意义的结论
        for line in answer.split("\n")[:5]:
            line = line.strip()
            if len(line) > 20 and any(w in line.lower() for w in decision_words[:3]):
                result.append(
                    Candidate(
                        topic="key-decisions",
                        key=f"decision:{hash(line) & 0xFFFF:04x}",
                        value=line[:200],
                        kind="decision",
                        confidence=0.6,
                        source="final_answer",
                    )
                )
                break

    # 惯例/约定检测
    convention_signals = ["应该", "should", "must", "必须", "惯例", "convention", "总是", "always"]
    for line in answer.split("\n")[:10]:
        line = line.strip()
        if len(line) > 15 and any(w in line.lower() for w in convention_signals):
            result.append(
                Candidate(
                    topic="project-conventions",
                    key=f"convention:{hash(line) & 0xFFFF:04x}",
                    value=line[:200],
                    kind="observation",
                    confidence=0.5,
                    source="final_answer",
                )
            )
            break

    return result


# ── LLM 辅助（仅填规划字段，禁止自由建 topic） ──

LLM_FILL_PROMPT = (
    "Classify this memory candidate. Only output a JSON object with two fields:\n"
    '{{"kind": "error|decision|observation|fact", "confidence": 0.0-1.0}}\n'
    "\nContent: {text}"
)


def llm_fill_candidate(candidate: Candidate, light_client) -> Candidate:
    """LLM 仅填 kind 和 confidence 字段，不修改 topic/key/value。"""
    import json

    try:
        prompt = LLM_FILL_PROMPT.format(text=candidate.value[:300])
        raw = light_client.complete(prompt, max_new_tokens=128)
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(raw[start:end])
            if data.get("kind") in CANDIDATE_KINDS:
                candidate.kind = data["kind"]
            if "confidence" in data and isinstance(data["confidence"], int | float):
                candidate.confidence = min(1.0, max(0.0, float(data["confidence"])))
    except Exception:
        pass
    return candidate


# ── 冲突门控 ──


@dataclass
class CandidateGateResult:
    """候选条目门控结果。"""

    allowed: bool
    reason: str = ""
    resolution: str = "none"  # none | equivalent | override | invalid | new


def gate_candidate(
    candidate: Candidate,
    existing_entries: list[str],
    authority: str = "",
) -> CandidateGateResult:
    """写 durable 前的冲突门控。

    按权威序（user > agent > auto）判定：高权威可覆盖低权威，反之拒绝。
    未指定 authority 时从 candidate.source 自动推断。
    """
    from agent_runtime.features.memory.durable import (
        _resolve_conflict,
        _subject_key,
        source_to_authority,
    )

    if not authority:
        authority = source_to_authority(candidate.source)

    if candidate.topic not in ALLOWED_TOPICS:
        return CandidateGateResult(allowed=False, reason=f"非法 topic: {candidate.topic}")

    candidate_text = candidate.promotion[1]
    candidate_subject = _subject_key(candidate_text)

    for i, entry in enumerate(existing_entries):
        existing_subject = _subject_key(entry)
        if existing_subject == candidate_subject:
            resolution = _resolve_conflict(entry, candidate_text, authority)
            if resolution.value == "override":
                return CandidateGateResult(allowed=True, resolution="override")
            elif resolution.value == "equivalent":
                return CandidateGateResult(
                    allowed=False, reason="内容等效", resolution="equivalent"
                )
            elif resolution.value == "invalid":
                return CandidateGateResult(
                    allowed=False, reason="低权威不可覆盖高权威", resolution="invalid"
                )
            break

    # 新条目
    if not any(candidate_subject in e.lower() for e in existing_entries):
        return CandidateGateResult(allowed=True, resolution="new")

    return CandidateGateResult(allowed=False, reason="疑似重复", resolution="none")


# ── Hook 入口 ──


def candidates_from_tool(name: str, args: dict, result_text: str) -> list[Candidate]:
    """after_tool hook：从工具结果抽取候选。"""
    return extract_from_tool_result(name, args, result_text)


def candidates_from_answer(answer: str, issue: str = "") -> list[Candidate]:
    """after_ask hook：从最终答案抽取候选。"""
    candidates = extract_from_final_answer(answer, issue)
    # 也尝试从 issue 的 stack trace 抽取
    if issue:
        candidates.extend(extract_from_stack(issue))
    return candidates


def promote_candidates(
    store,
    candidates: list[Candidate],
    *,
    authority: str = "auto",
    light_client=None,
) -> int:
    """将候选条目经门控后写入 durable store。返回成功写入数。"""
    written = 0
    for c in candidates:
        # 可选 LLM 补充字段
        if light_client is not None and c.confidence == 0.5:
            c = llm_fill_candidate(c, light_client)

        # 读取同 topic 已有条目
        strategy = store._topic_strategy(c.topic)
        existing = store._read_topic(c.topic, strategy=strategy)

        gate = gate_candidate(c, existing, authority=authority)
        if not gate.allowed:
            continue

        store.promote([c.promotion])
        written += 1

    return written
