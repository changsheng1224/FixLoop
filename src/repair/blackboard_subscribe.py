"""Blackboard 前缀订阅：按 prefix 批量 read_related 并渲染 prompt 块。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from src.blackboard import Blackboard
from src.repair.blackboard_merge import (
    CONTEXT_PREFIX,
    SCRATCH_PREFIX,
    SUSPECT_PREFIX,
    read_context_from_blackboard,
    read_suspects_from_blackboard,
)
from src.repair.localization.suspect_blocks import render_suspects_with_snippets
from src.state import RepairPlan, RetrievedContext, SuspectLocation

__all__ = [
    "PrefixSubscription",
    "PATCHER_PREFIX_SUBSCRIPTIONS",
    "PatcherPrefixBlocks",
    "render_patcher_prefix_blocks",
    "subscribe_prefixes",
]


@dataclass(frozen=True)
class PrefixSubscription:
    """声明某角色订阅的 Blackboard 前缀命名空间。"""

    prefix: str
    block_field: str
    priority: int = 0


PATCHER_PREFIX_SUBSCRIPTIONS: tuple[PrefixSubscription, ...] = (
    PrefixSubscription(SUSPECT_PREFIX, "suspects_block", priority=10),
    PrefixSubscription(CONTEXT_PREFIX, "test_blocks", priority=20),
    PrefixSubscription(SCRATCH_PREFIX, "scratch_block", priority=30),
)


@dataclass
class PatcherPrefixBlocks:
    """Patcher 从前缀订阅渲染出的 prompt 块。"""

    suspects_block: str = ""
    test_blocks: str = ""
    scratch_block: str = ""
    subscribed_prefixes: list[str] | None = None
    entry_counts: dict[str, int] | None = None


def subscribe_prefixes(bb: Blackboard, prefixes: tuple[str, ...] | list[str]) -> dict[str, dict]:
    """Batch ``read_related`` for each subscribed prefix."""
    result: dict[str, dict] = {}
    for prefix in prefixes:
        result[prefix] = bb.read_related(prefix)
    return result


def _render_suspects_block(
    entries: dict[str, object],
    *,
    read_snippet: Callable[[str, int, int], str],
) -> tuple[str, list[SuspectLocation]]:
    suspects: list[SuspectLocation] = []
    for value in entries.values():
        if isinstance(value, dict):
            suspects.append(SuspectLocation.from_dict(value))
    if not suspects:
        return "", []
    block, ordered = render_suspects_with_snippets(suspects, read_snippet)
    return block, ordered


def _render_context_test_block(
    entries: dict[str, object],
    *,
    suspects: list[SuspectLocation],
    plan: RepairPlan | None,
    read_test_context: Callable[
        [RetrievedContext | None, list[SuspectLocation], RepairPlan | None],
        list[str],
    ],
) -> str:
    if not entries and not suspects:
        return ""
    context = read_context_from_blackboard(_EntriesBlackboard(entries, CONTEXT_PREFIX))
    test_blocks = read_test_context(context, suspects, plan)
    if not test_blocks:
        return ""
    return "相关测试文件（补丁必须通过这些 assert）:\n" + "\n".join(test_blocks)


def _render_scratch_block(entries: dict[str, object]) -> str:
    feedback = entries.get(f"{SCRATCH_PREFIX}feedback")
    if isinstance(feedback, str) and feedback.strip():
        return feedback.strip()
    return ""


class _EntriesBlackboard:
    """Minimal adapter so ``read_context_from_blackboard`` can read a prefix slice."""

    def __init__(self, entries: dict[str, object], prefix: str):
        self._entries = entries
        self._prefix = prefix

    def read_related(self, prefix: str) -> dict[str, object]:
        if prefix != self._prefix:
            return {}
        return dict(self._entries)


def render_patcher_prefix_blocks(
    bb: Blackboard,
    *,
    read_snippet: Callable[[str, int, int], str],
    read_test_context: Callable[
        [RetrievedContext | None, list[SuspectLocation], RepairPlan | None],
        list[str],
    ],
    plan: RepairPlan | None = None,
    subscriptions: tuple[PrefixSubscription, ...] = PATCHER_PREFIX_SUBSCRIPTIONS,
) -> PatcherPrefixBlocks:
    """Render Patcher prompt blocks from blackboard prefix subscriptions."""
    subscribed = subscribe_prefixes(bb, [sub.prefix for sub in subscriptions])
    entry_counts = {prefix: len(entries) for prefix, entries in subscribed.items()}

    suspect_entries = subscribed.get(SUSPECT_PREFIX, {})
    suspects_block, suspects = _render_suspects_block(
        suspect_entries,
        read_snippet=read_snippet,
    )

    context_entries = subscribed.get(CONTEXT_PREFIX, {})
    test_blocks = _render_context_test_block(
        context_entries,
        suspects=suspects or read_suspects_from_blackboard(bb),
        plan=plan,
        read_test_context=read_test_context,
    )

    scratch_entries = subscribed.get(SCRATCH_PREFIX, {})
    scratch_block = _render_scratch_block(scratch_entries)

    return PatcherPrefixBlocks(
        suspects_block=suspects_block,
        test_blocks=test_blocks,
        scratch_block=scratch_block,
        subscribed_prefixes=[sub.prefix for sub in subscriptions],
        entry_counts=entry_counts,
    )
