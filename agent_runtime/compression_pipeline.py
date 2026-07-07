"""Context 压缩管线 L0–L5。

L1（Budget Reduction）：tool 结果 token 级截断 + 重要行优先。
L2（Snip）：history 超 55% window 时删除低价值旧轮，折叠为 snip 标记。
L3（Microcompact）：history 超 70% window 时旧 tool → [ref:#id] stub + metadata 侧表。
L4（Collapse）：history 超 82% window 时折叠旧 turn 为摘要行。
L5（Auto Compact）：history 超 100% window 时用 LLM 摘要前半段，保留后半段。
L0（Tier Guard）：组装前过滤不该进窗的 history / section 内容。
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from agent_runtime.context_manager import TokenBudget
    from agent_runtime.tier_policy import TierPolicy

Summarizer = Callable[[str], str]

# 按工具类型的 L1 截断上限（tokens）
TOOL_TRUNCATION_TOKENS = {
    "list_files": 60,
    "search": 230,
    "read_file": 570,
    "write_file": 90,
    "patch_file": 90,
    "run_shell": 145,
}
DEFAULT_TOOL_TRUNCATION = 150

STAGE_ORDER = ("L0", "L1", "L2", "L3", "L4", "L5")

# L2–L5 压缩阈值（占 history window 的百分比；window 随 prompt_budget 缩放）
L2_TRIGGER_RATIO = 0.55
L3_TRIGGER_RATIO = 0.70
L4_TRIGGER_RATIO = 0.82
L5_TRIGGER_RATIO = 1.0
L2_PROTECT_RECENT_TURNS = 2
TAIL_PROTECT_TOKENS = 20_000
# 小 window 时保护区不超过旧布局 2000/2600，为 L2–L4 留出可压缩空间
TAIL_PROTECT_WINDOW_RATIO = 2000 / 2600
L4_MIN_TURN_TOKENS = 40
L5_FALLBACK_KEEP_ENTRIES = 8
L5_SUMMARY_MAX_CHARS = 300
L5_PROMPT_TAIL_ENTRIES = 20

READONLY_TOOLS = frozenset({"list_files", "read_file", "search"})
HIGH_VALUE_TOOLS = frozenset({"write_file", "patch_file", "run_shell"})
PROTECTED_KEYWORDS = ("Error", "Traceback", "Fail", "FAILED", "error:")


def truncate_tool_content(
    content: str,
    tool_name: str = "",
    *,
    budget: TokenBudget | None = None,
) -> str:
    """L1：按工具类型差异化截断，重要行优先保留（token 级）。"""
    from agent_runtime.context_manager import TokenBudget as _TokenBudget

    budget = budget or _TokenBudget()
    limit = TOOL_TRUNCATION_TOKENS.get(tool_name, DEFAULT_TOOL_TRUNCATION)
    total_tokens = budget.count(content)
    if total_tokens <= limit:
        return content

    suffix = f"\n... (截断，共 {total_tokens} tokens)"
    suffix_tokens = budget.count(suffix)
    body_limit = max(16, limit - suffix_tokens)

    lines = content.splitlines()
    important = []
    other = []
    for line in lines:
        if "Error" in line or "error" in line or "Fail" in line or "/" in line:
            important.append(line)
        else:
            other.append(line)

    result_lines: list[str] = []
    for line in important + other:
        candidate = "\n".join(result_lines + [line]) if result_lines else line
        if budget.count(candidate) <= body_limit:
            result_lines.append(line)
            continue
        if not result_lines:
            body = budget.fit(line, body_limit)
            return body + suffix
        break

    body = "\n".join(result_lines)
    if not body:
        body = budget.fit(content, body_limit)
    elif budget.count(body) > body_limit:
        body = budget.fit(body, body_limit)
    return body + suffix


def apply_l1_to_request_text(
    text: str,
    budget: TokenBudget,
    *,
    tool_name: str = "",
) -> str:
    """对 request 段中「工具 … 执行完成。\\n结果:\\n」块应用 L1。"""
    marker = "\n结果:\n"
    if marker not in text:
        return text
    prefix, _, body = text.partition(marker)
    if not body:
        return text
    inferred = tool_name
    if not inferred and prefix.startswith("工具 "):
        inferred = prefix.split()[1] if len(prefix.split()) > 1 else ""
    return prefix + marker + truncate_tool_content(body, inferred, budget=budget)


def history_item_text(item: dict) -> str:
    """单条 history 的计数字符串。"""
    return f"{item.get('role', '?')}: {item.get('content', '')}"


def count_history_tokens(history: list[dict], budget: TokenBudget) -> int:
    """history 列表 token 总数。"""
    if not history:
        return 0
    return budget.count("\n".join(history_item_text(item) for item in history))


def group_history_into_turns(history: list[dict]) -> list[list[dict]]:
    """按 user 消息边界分组为 turn（一轮 = user + 后续 assistant/tool 链）。"""
    turns: list[list[dict]] = []
    current: list[dict] = []
    for item in history:
        if item.get("role") == "user":
            if current:
                turns.append(current)
            current = [item]
        else:
            if not current:
                current = [item]
            else:
                current.append(item)
    if current:
        turns.append(current)
    return turns


def _turn_tool_names(turn: list[dict]) -> list[str]:
    names: list[str] = []
    for item in turn:
        if item.get("_snip"):
            continue
        if item.get("role") == "tool":
            name = item.get("tool_name") or "tool"
            names.append(str(name))
        elif item.get("role") == "assistant" and item.get("tool_name"):
            names.append(str(item["tool_name"]))
    return names


def score_turn(turn: list[dict], *, current_turn_id: int | None = None) -> str:
    """返回 'snip' 或 'keep'。"""
    from agent_runtime.turn_tracking import is_current_turn

    if is_current_turn(turn, current_turn_id):
        return "keep"
    if any(item.get("_snip") for item in turn):
        return "keep"
    combined = "\n".join(str(item.get("content", "")) for item in turn)
    if any(keyword in combined for keyword in PROTECTED_KEYWORDS):
        return "keep"
    tool_names = _turn_tool_names(turn)
    if any(name in HIGH_VALUE_TOOLS for name in tool_names):
        return "keep"
    if tool_names and all(name in READONLY_TOOLS for name in tool_names):
        return "snip"
    return "keep"


def make_snip_marker(turn: list[dict], turn_index: int) -> dict:
    """A+B 混合：删除原 turn 内容，替换为一行 snip 标记。"""
    names = _turn_tool_names(turn)
    if not names:
        label = "dialogue"
    else:
        counts = Counter(names)
        parts = []
        for name, count in counts.items():
            parts.append(f"{name}×{count}" if count > 1 else name)
        label = ",".join(parts)
    return {
        "role": "system",
        "content": f"[snipped turn #{turn_index + 1}: {label}]",
        "_snip": True,
    }


def resolve_history_window(budget: TokenBudget, history_window: int | None = None) -> int:
    """history section 预算 = 压缩管线 window。"""
    if history_window is not None:
        return history_window
    from agent_runtime.context_manager import history_window_budget

    return history_window_budget(budget.total_limit)


def compression_threshold(ratio: float, history_window: int) -> int:
    """按 window 百分比计算阶段触发 token 阈值。"""
    return int(ratio * history_window)


def effective_tail_protect_tokens(tail_protect_tokens: int, history_window: int) -> int:
    """尾部保护区有效 token 上限。

    大 window：最多 tail_protect_tokens（默认 20k）。
    小 window：不超过 window×(2000/2600)，避免整窗豁免导致 L2–L4 失效。
    """
    if history_window <= 0:
        return 0
    window_cap = int(history_window * TAIL_PROTECT_WINDOW_RATIO)
    return min(max(0, tail_protect_tokens), window_cap)


def protected_turn_indices(
    turns: list[list[dict]],
    budget: TokenBudget,
    history_window: int,
    current_turn_id: int | None = None,
    tail_protect_tokens: int = TAIL_PROTECT_TOKENS,
) -> set[int]:
    """尾部 turn 保护区：current turn_id + 最近 N 轮 + 尾部 tail_protect（整 turn，跨边界全保护）。"""
    from agent_runtime.turn_tracking import is_current_turn

    protected: set[int] = set()
    n = len(turns)
    if n == 0:
        return protected

    if current_turn_id is not None:
        for i, turn in enumerate(turns):
            if is_current_turn(turn, current_turn_id):
                protected.add(i)

    effective_tail = effective_tail_protect_tokens(tail_protect_tokens, history_window)

    for i in range(max(0, n - L2_PROTECT_RECENT_TURNS), n):
        protected.add(i)

    token_count = 0
    for i in range(n - 1, -1, -1):
        turn_tok = count_history_tokens(turns[i], budget)
        if token_count >= effective_tail:
            break
        if token_count + turn_tok > effective_tail:
            protected.add(i)
            break
        protected.add(i)
        token_count += turn_tok
    return protected


def flatten_turns(turns: list[list[dict]]) -> list[dict]:
    flat: list[dict] = []
    for turn in turns:
        flat.extend(turn)
    return flat


def flat_index_to_turn(flat_idx: int, turns: list[list[dict]]) -> int:
    """flat history 下标 → turn 下标。"""
    offset = 0
    for turn_idx, turn in enumerate(turns):
        if flat_idx < offset + len(turn):
            return turn_idx
        offset += len(turn)
    return max(0, len(turns) - 1)


def has_protected_tool_content(content: str) -> bool:
    return any(keyword in content for keyword in PROTECTED_KEYWORDS)


def assign_tool_ref_ids(history: list[dict]) -> None:
    """为 tool 条目分配稳定 ref id（仅投影副本）。"""
    ref_id = 0
    for item in history:
        if item.get("role") != "tool" or item.get("_snip"):
            continue
        ref_id += 1
        item["_ref_id"] = ref_id


def make_compact_stub(tool_name: str, ref_id: int, original_tokens: int) -> str:
    name = tool_name or "tool"
    return f"[ref:#{ref_id}] {name} ({original_tokens} tok compacted)"


def turn_tokens(turn: list[dict], budget: TokenBudget) -> int:
    return count_history_tokens(turn, budget)


def turn_has_protected_content(turn: list[dict]) -> bool:
    for item in turn:
        if has_protected_tool_content(str(item.get("content", ""))):
            return True
    return False


def is_collapsible_turn(
    turn: list[dict],
    budget: TokenBudget,
    *,
    current_turn_id: int | None = None,
) -> bool:
    from agent_runtime.turn_tracking import is_current_turn

    if is_current_turn(turn, current_turn_id):
        return False
    if any(item.get("_snip") or item.get("_collapsed") for item in turn):
        return False
    if turn_has_protected_content(turn):
        return False
    return turn_tokens(turn, budget) >= L4_MIN_TURN_TOKENS


def make_collapse_marker(turn: list[dict], turn_index: int) -> dict:
    user_msg = next(
        (str(item.get("content", ""))[:60] for item in turn if item.get("role") == "user"),
        "",
    )
    tools = _turn_tool_names(turn)
    tool_label = ",".join(dict.fromkeys(tools)) if tools else "none"
    refs = sum(1 for item in turn if item.get("_compact_ref"))
    content = (
        f"[collapsed turn #{turn_index + 1}: user='{user_msg}' | "
        f"tools={tool_label} | refs={refs}]"
    )
    return {"role": "system", "content": content, "_collapsed": True}


def l0_tier_guard(
    history: list[dict],
    metadata: dict,
    *,
    policy: TierPolicy | None = None,
) -> list[dict]:
    """L0 Tier Guard：过滤不该进入 context 窗口的 history 投影条目。"""
    from agent_runtime.tier_policy import TierPolicy as _TierPolicy, l0_filter_history

    pipe = metadata.setdefault("compression_pipeline", {})
    tier = policy if policy is not None else _TierPolicy()
    filtered, stats = l0_filter_history(history, tier)
    pipe["l0"] = "applied" if stats["dropped"] else "skipped"
    pipe["l0_dropped"] = stats["dropped"]
    pipe["l0_rules_applied"] = stats["rules_applied"]
    return filtered


def l1_budget_reduction(
    history: list[dict],
    budget: TokenBudget,
    metadata: dict,
) -> tuple[list[dict], int]:
    """L1：对 history 中 tool 条目做 token 级截断（投影副本）。"""
    truncations = 0
    for item in history:
        if item.get("role") != "tool":
            continue
        original = str(item.get("content", ""))
        tool_name = str(item.get("tool_name", ""))
        truncated = truncate_tool_content(original, tool_name, budget=budget)
        if truncated != original:
            truncations += 1
        item["content"] = truncated
    metadata.setdefault("compression_pipeline", {})["l1_truncations"] = truncations
    return history, truncations


def l2_snip(
    history: list[dict],
    budget: TokenBudget,
    metadata: dict,
    *,
    history_window: int,
    current_turn_id: int | None = None,
    tail_protect_tokens: int = TAIL_PROTECT_TOKENS,
) -> list[dict]:
    """L2 Snip：超 55% history window 时，从最旧 turn 删除低价值只读探索轮。"""
    pipe = metadata.setdefault("compression_pipeline", {})
    threshold = compression_threshold(L2_TRIGGER_RATIO, history_window)
    tokens_before = count_history_tokens(history, budget)

    if tokens_before <= threshold:
        pipe["l2_triggered"] = False
        pipe["l2"] = "skipped"
        return history

    turns = group_history_into_turns(history)
    if len(turns) <= L2_PROTECT_RECENT_TURNS:
        pipe["l2_triggered"] = False
        pipe["l2"] = "skipped_insufficient_turns"
        return history

    protected = protected_turn_indices(
        turns, budget, history_window, current_turn_id, tail_protect_tokens
    )
    working = [list(turn) for turn in turns]
    snipped = 0

    while count_history_tokens(flatten_turns(working), budget) > threshold:
        snipped_any = False
        for i in range(len(working)):
            if i in protected:
                continue
            turn = working[i]
            if any(item.get("_snip") for item in turn):
                continue
            if score_turn(turn, current_turn_id=current_turn_id) != "snip":
                continue
            working[i] = [make_snip_marker(turn, i)]
            snipped += 1
            snipped_any = True
            break
        if not snipped_any:
            break

    result = flatten_turns(working)
    pipe["l2_triggered"] = snipped > 0
    pipe["l2_snipped_turns"] = snipped
    pipe["l2_tokens_before"] = tokens_before
    pipe["l2_tokens_after"] = count_history_tokens(result, budget)
    pipe["l2_threshold"] = threshold
    if snipped > 0:
        pipe["l2"] = "applied"
    else:
        pipe["l2"] = "no_snippable_turns"
    return result


def l3_microcompact(
    history: list[dict],
    budget: TokenBudget,
    metadata: dict,
    *,
    history_window: int,
    current_turn_id: int | None = None,
    tail_protect_tokens: int = TAIL_PROTECT_TOKENS,
) -> list[dict]:
    """L3 Microcompact：超 70% window 时，旧 tool 正文替换为 [ref:#id] stub。"""
    pipe = metadata.setdefault("compression_pipeline", {})
    threshold = compression_threshold(L3_TRIGGER_RATIO, history_window)
    tokens_before = count_history_tokens(history, budget)

    if tokens_before <= threshold:
        pipe["l3_triggered"] = False
        pipe["l3"] = "skipped"
        return history

    turns = group_history_into_turns(history)
    protected = protected_turn_indices(
        turns, budget, history_window, current_turn_id, tail_protect_tokens
    )
    working = [dict(item) for item in history]
    assign_tool_ref_ids(working)

    refs: dict[str, dict] = {}
    compacted = 0

    while count_history_tokens(working, budget) > threshold:
        compacted_any = False
        for idx, item in enumerate(working):
            if item.get("role") != "tool":
                continue
            if item.get("_snip") or item.get("_compact_ref"):
                continue
            turn_idx = flat_index_to_turn(idx, turns)
            if turn_idx in protected:
                continue
            original = str(item.get("content", ""))
            if has_protected_tool_content(original):
                continue
            tool_name = str(item.get("tool_name", ""))
            ref_id = int(item.get("_ref_id", idx + 1))
            original_tokens = budget.count(original)
            ref_key = f"#{ref_id}"
            refs[ref_key] = {
                "tool_name": tool_name,
                "tokens_saved": original_tokens,
                "preview": original[:80],
            }
            item["content"] = make_compact_stub(tool_name, ref_id, original_tokens)
            item["_compact_ref"] = ref_id
            compacted += 1
            compacted_any = True
            break
        if not compacted_any:
            break

    pipe["l3_triggered"] = compacted > 0
    pipe["l3_compacted"] = compacted
    pipe["l3_tokens_before"] = tokens_before
    pipe["l3_tokens_after"] = count_history_tokens(working, budget)
    pipe["l3_threshold"] = threshold
    if refs:
        pipe["l3_refs"] = refs
    if compacted > 0:
        pipe["l3"] = "applied"
    else:
        pipe["l3"] = "no_compactable_tools"
    return working


def l4_collapse(
    history: list[dict],
    budget: TokenBudget,
    metadata: dict,
    *,
    history_window: int,
    current_turn_id: int | None = None,
    tail_protect_tokens: int = TAIL_PROTECT_TOKENS,
) -> list[dict]:
    """L4 Collapse：超 82% window 时，旧 turn 折叠为结构化摘要行。"""
    pipe = metadata.setdefault("compression_pipeline", {})
    threshold = compression_threshold(L4_TRIGGER_RATIO, history_window)
    tokens_before = count_history_tokens(history, budget)

    if tokens_before <= threshold:
        pipe["l4_triggered"] = False
        pipe["l4"] = "skipped"
        return history

    turns = group_history_into_turns(history)
    if len(turns) <= L2_PROTECT_RECENT_TURNS:
        pipe["l4_triggered"] = False
        pipe["l4"] = "skipped_insufficient_turns"
        return history

    protected = protected_turn_indices(
        turns, budget, history_window, current_turn_id, tail_protect_tokens
    )
    working = [list(turn) for turn in turns]
    collapsed = 0
    details: list[dict] = []

    while count_history_tokens(flatten_turns(working), budget) > threshold:
        collapsed_any = False
        for i in range(len(working)):
            if i in protected:
                continue
            turn = working[i]
            if not is_collapsible_turn(turn, budget, current_turn_id=current_turn_id):
                continue
            tokens_saved = turn_tokens(turn, budget)
            details.append(
                {
                    "turn": i + 1,
                    "tokens_saved": tokens_saved,
                    "tools": _turn_tool_names(turn),
                }
            )
            working[i] = [make_collapse_marker(turn, i)]
            collapsed += 1
            collapsed_any = True
            break
        if not collapsed_any:
            break

    result = flatten_turns(working)
    pipe["l4_triggered"] = collapsed > 0
    pipe["l4_collapsed_turns"] = collapsed
    pipe["l4_tokens_before"] = tokens_before
    pipe["l4_tokens_after"] = count_history_tokens(result, budget)
    pipe["l4_threshold"] = threshold
    if details:
        pipe["l4_details"] = details
    if collapsed > 0:
        pipe["l4"] = "applied"
    else:
        pipe["l4"] = "no_collapsible_turns"
    return result


def l5_auto_compact(
    history: list[dict],
    budget: TokenBudget,
    metadata: dict,
    *,
    summarizer: Summarizer | None = None,
    summary_cache: dict[str, str] | None = None,
    trigger_tokens: int | None = None,
    history_window: int | None = None,
) -> list[dict]:
    """L5 Auto Compact：history 超 100% window 时 LLM 摘要前半段，保留后半段。

    成功：[system summary] + recent_half
    失败：保留最近 L5_FALLBACK_KEEP_ENTRIES 条
    """
    pipe = metadata.setdefault("compression_pipeline", {})
    window = history_window if history_window is not None else resolve_history_window(budget)
    trigger = (
        trigger_tokens
        if trigger_tokens is not None
        else compression_threshold(L5_TRIGGER_RATIO, window)
    )
    tokens_before = count_history_tokens(history, budget)

    if tokens_before <= trigger:
        pipe["l5_triggered"] = False
        pipe["l5"] = "skipped"
        return history

    mid = len(history) // 2
    old_history = history[:mid]
    recent_history = history[mid:]

    cache = summary_cache if summary_cache is not None else {}
    cache_key = _summary_cache_key(old_history)

    summary = ""
    cache_hit = False
    if cache_key in cache:
        summary = cache[cache_key]
        cache_hit = True
    elif summarizer is not None:
        try:
            summary = summarizer(_build_l5_summary_prompt(old_history))
        except Exception:
            summary = ""
        if summary:
            cache[cache_key] = summary

    if summary:
        result = [
            {"role": "system", "content": f"[Earlier summary]: {summary}"},
            *[dict(item) for item in recent_history],
        ]
        tokens_after = count_history_tokens(result, budget)
        pipe["l5_triggered"] = True
        pipe["l5"] = "summarized"
        pipe["l5_summary_cache_hit"] = cache_hit
        pipe["l5_fallback"] = False
        pipe["l5_tokens_before"] = tokens_before
        pipe["l5_tokens_after"] = tokens_after
        return result

    fallback = [dict(item) for item in history[-L5_FALLBACK_KEEP_ENTRIES:]]
    tokens_after = count_history_tokens(fallback, budget)
    pipe["l5_triggered"] = True
    pipe["l5"] = "fallback_trim"
    pipe["l5_summary_cache_hit"] = False
    pipe["l5_fallback"] = True
    pipe["l5_tokens_before"] = tokens_before
    pipe["l5_tokens_after"] = tokens_after
    return fallback


def _summary_cache_key(old_history: list[dict]) -> str:
    import hashlib

    raw = "".join(str(h.get("content", ""))[:100] for h in old_history[-10:])
    return hashlib.md5(raw.encode()).hexdigest()


def _build_l5_summary_prompt(old_history: list[dict]) -> str:
    prompt_lines = [
        "Summarize the following conversation in 1-2 sentences.",
        "Focus on: files read, tools used, errors encountered, decisions made.",
        "",
    ]
    for item in old_history[-L5_PROMPT_TAIL_ENTRIES:]:
        role = item.get("role", "unknown")
        content = str(item.get("content", ""))[:150]
        prompt_lines.append(f"{role}: {content}")
    return "\n".join(prompt_lines)


def make_summarizer(agent) -> Summarizer | None:
    """从 Agent 构建 L5 摘要回调（优先 light_client）。"""
    client = getattr(agent, "light_client", None) or getattr(agent, "model_client", None)
    if client is None:
        return None

    def _summarize(prompt: str) -> str:
        raw = client.complete(prompt, max_new_tokens=2048)
        return raw.strip()[:L5_SUMMARY_MAX_CHARS] if raw else ""

    return _summarize


def run_compression_pipeline(
    history: list[dict],
    budget: TokenBudget,
    *,
    metadata: dict | None = None,
    summarizer: Summarizer | None = None,
    summary_cache: dict[str, str] | None = None,
    l5_trigger_tokens: int | None = None,
    history_window: int | None = None,
    tier_policy: TierPolicy | None = None,
) -> list[dict]:
    """按 L0→L5 顺序处理 history 投影副本，不修改 canonical session。"""
    from agent_runtime.tier_policy import TierPolicy as _TierPolicy

    meta: dict[str, Any] = metadata if metadata is not None else {}
    pipe_meta = meta.setdefault("compression_pipeline", {})
    pipe_meta["stages"] = list(STAGE_ORDER)
    tier = tier_policy if tier_policy is not None else _TierPolicy()
    active_turn_id = tier.current_turn_id if tier.protect_current_turn else None
    tail_protect = tier.tail_protect_tokens

    window = resolve_history_window(budget, history_window)
    effective_tail = effective_tail_protect_tokens(tail_protect, window)
    pipe_meta["history_window"] = window
    pipe_meta["current_turn_id"] = active_turn_id
    pipe_meta["tail_protect_tokens"] = tail_protect
    pipe_meta["tail_protect_effective"] = effective_tail
    pipe_meta["l2_threshold"] = compression_threshold(L2_TRIGGER_RATIO, window)
    pipe_meta["l3_threshold"] = compression_threshold(L3_TRIGGER_RATIO, window)
    pipe_meta["l4_threshold"] = compression_threshold(L4_TRIGGER_RATIO, window)
    pipe_meta["l5_threshold"] = compression_threshold(L5_TRIGGER_RATIO, window)

    projected = [dict(item) for item in history]

    projected = l0_tier_guard(projected, meta, policy=tier)
    projected, _ = l1_budget_reduction(projected, budget, meta)
    projected = l2_snip(
        projected,
        budget,
        meta,
        history_window=window,
        current_turn_id=active_turn_id,
        tail_protect_tokens=tail_protect,
    )
    projected = l3_microcompact(
        projected,
        budget,
        meta,
        history_window=window,
        current_turn_id=active_turn_id,
        tail_protect_tokens=tail_protect,
    )
    projected = l4_collapse(
        projected,
        budget,
        meta,
        history_window=window,
        current_turn_id=active_turn_id,
        tail_protect_tokens=tail_protect,
    )
    projected = l5_auto_compact(
        projected,
        budget,
        meta,
        summarizer=summarizer,
        summary_cache=summary_cache,
        trigger_tokens=l5_trigger_tokens,
        history_window=window,
    )

    return projected


def truncate_tool_result_for_agent(agent, tool_name: str, result_text: str) -> str:
    """AgentLoop / ToolExecutor 投影层：对单次 tool 返回应用 L1。"""
    from agent_runtime.context_manager import TokenBudget, TOTAL_BUDGET

    cfg = getattr(agent, "config", None)
    budget = TokenBudget(
        model=getattr(cfg, "model", "deepseek-v4-pro"),
        total_limit=getattr(cfg, "prompt_budget", TOTAL_BUDGET),
        provider=getattr(cfg, "provider", "deepseek"),
    )
    return truncate_tool_content(result_text, tool_name, budget=budget)
