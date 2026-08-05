"""定位快路径：规则优先于 LLM，避免 localize 空转超时导致空 patch。

能力向（非单例）：
- issue 栈帧 / 路径 / RepairPlan / F2P / test_patch 先落地
- 空锚 → cheap grep；LLM 只作 enrichment 且路径必须存在于磁盘
- 跨 retry memory；行级落点
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from src.state import RepairPlan, RepairState, SuspectLocation

__all__ = [
    "filter_llm_suspects_to_disk",
    "merge_llm_with_rule_first",
    "rule_first_suspects",
    "seed_rule_first_suspects",
    "suspects_from_fail_to_pass",
]


def suspects_from_fail_to_pass(
    issue: str,
    repo_root: str | Path,
    *,
    extra_hints: list[str] | None = None,
    max_keep: int = 8,
) -> list[SuspectLocation]:
    """FAIL_TO_PASS / 相关测试 hint → 实现文件嫌疑（符号索引覆盖边）。"""
    from src.repair.fail_to_pass_hints import extract_fail_to_pass_hints
    from src.repair.localize_quality import _is_test_path, normalize_repo_path
    from src.repair.symbol_index import get_or_build_index

    hints = list(extract_fail_to_pass_hints(issue or ""))
    for h in extra_hints or []:
        if h and h not in hints:
            hints.append(h)
    if not hints:
        return []

    root = Path(repo_root)
    idx = get_or_build_index(root)
    out: list[SuspectLocation] = []
    seen: set[str] = set()

    for hint in hints:
        for s in idx.impls_for_test(hint, max_hits=4):
            fp = str(s.file_path or "").replace("\\", "/")
            if not fp or fp in seen:
                continue
            seen.add(fp)
            out.append(
                SuspectLocation(
                    file_path=fp,
                    start_line=s.start_line,
                    end_line=s.end_line,
                    function_name=s.function_name,
                    reason="F2P覆盖",
                    confidence=max(0.8, float(s.confidence or 0.0)),
                )
            )
            if len(out) >= max_keep:
                return out

        file_part = hint.replace("\\", "/").split("::", 1)[0]
        rel = normalize_repo_path(file_part, root) or file_part.lstrip("./")
        if rel and rel not in seen and (root / rel).is_file():
            seen.add(rel)
            out.append(
                SuspectLocation(
                    file_path=rel,
                    start_line=1,
                    end_line=1,
                    reason="F2P测试",
                    confidence=0.45 if _is_test_path(rel) else 0.55,
                )
            )
            if len(out) >= max_keep:
                return out
    return out


def filter_llm_suspects_to_disk(
    llm_suspects: list[SuspectLocation] | None,
    repo_root: str | Path,
) -> list[SuspectLocation]:
    """LLM 路径必须真实存在于仓库，否则丢弃（防幻觉覆盖规则锚）。"""
    from src.repair.localize_quality import normalize_repo_path

    root = Path(repo_root)
    out: list[SuspectLocation] = []
    for s in llm_suspects or []:
        rel = normalize_repo_path(s.file_path or "", root)
        if not rel or not (root / rel).is_file():
            continue
        out.append(
            SuspectLocation(
                file_path=rel,
                start_line=int(s.start_line or 1),
                end_line=max(int(s.start_line or 1), int(s.end_line or 1)),
                function_name=s.function_name,
                class_name=s.class_name,
                reason=s.reason or "llm",
                confidence=float(s.confidence or 0.5),
            )
        )
    return out


def _test_patch_text(state: RepairState | None = None) -> str:
    if state is None:
        return ""
    raw = state.node_timings.get("verify_test_patch") or ""
    if isinstance(raw, str) and raw.strip():
        return raw
    return ""


def rule_first_suspects(
    issue: str,
    repo_root: str | Path,
    plan: RepairPlan | None = None,
    *,
    fallback_from_plan: Callable[[RepairPlan, str], list[SuspectLocation]] | None = None,
    related_tests: list[str] | None = None,
    fail_nodeids: list[str] | None = None,
    test_patch: str = "",
    state: RepairState | None = None,
    max_keep: int = 8,
) -> list[SuspectLocation]:
    """不等 LLM：从 issue/plan/F2P/test_patch/memory 生成可编辑嫌疑。"""
    from src.repair.localize_landing import refine_suspect_landing
    from src.repair.localize_memory import apply_localize_memory, remember_negated_files
    from src.repair.localize_quality import refine_suspects, suspects_from_issue
    from src.repair.localize_test_patch import suspects_from_test_patch

    if state is not None:
        remember_negated_files(state)

    seeded: list[SuspectLocation] = list(suspects_from_issue(issue or "", repo_root))
    if plan is not None and fallback_from_plan is not None:
        try:
            seeded.extend(fallback_from_plan(plan, issue or ""))
        except Exception:
            pass

    extra = list(related_tests or []) + list(fail_nodeids or [])
    seeded.extend(
        suspects_from_fail_to_pass(
            issue or "",
            repo_root,
            extra_hints=extra,
            max_keep=max_keep,
        )
    )

    patch_text = test_patch or _test_patch_text(state)
    if not patch_text and state is not None:
        # repair_ctx 可能挂在 orchestrator；允许 timings 旁路
        pass
    if patch_text:
        seeded.extend(suspects_from_test_patch(patch_text, repo_root, max_keep=max_keep))

    if state is not None:
        seeded = apply_localize_memory(seeded, state)

    refined = refine_suspects(
        seeded,
        issue or "",
        repo_root,
        plan=plan,
        max_keep=max_keep,
        related_tests=related_tests,
        fail_nodeids=fail_nodeids,
    )
    return refine_suspect_landing(
        refined,
        repo_root,
        issue or "",
        related_tests=related_tests,
        fail_nodeids=fail_nodeids,
    )


def merge_llm_with_rule_first(
    llm_suspects: list[SuspectLocation] | None,
    rule_suspects: list[SuspectLocation] | None,
    *,
    issue: str = "",
    repo_root: str | Path = "",
    plan: RepairPlan | None = None,
    related_tests: list[str] | None = None,
    fail_nodeids: list[str] | None = None,
    max_keep: int = 8,
) -> list[SuspectLocation]:
    """LLM（仅磁盘存在路径）优先排序，规则保底；最终 refine + 行级落点。"""
    from src.repair.localize_landing import refine_suspect_landing
    from src.repair.localize_quality import refine_suspects

    llm_ok = filter_llm_suspects_to_disk(llm_suspects, repo_root)
    combined: list[SuspectLocation] = []
    combined.extend(llm_ok)
    combined.extend(rule_suspects or [])
    if not combined and (issue or related_tests or fail_nodeids):
        combined.extend(
            suspects_from_fail_to_pass(
                issue,
                repo_root,
                extra_hints=list(related_tests or []) + list(fail_nodeids or []),
                max_keep=max_keep,
            )
        )
    if not combined:
        return []
    refined = refine_suspects(
        combined,
        issue,
        repo_root,
        plan=plan,
        max_keep=max_keep,
        related_tests=related_tests,
        fail_nodeids=fail_nodeids,
    )
    return refine_suspect_landing(
        refined,
        repo_root,
        issue,
        related_tests=related_tests,
        fail_nodeids=fail_nodeids,
    )


def seed_rule_first_suspects(
    state: RepairState,
    repo_root: str | Path,
    *,
    fallback_from_plan: Callable[[RepairPlan, str], list[SuspectLocation]] | None = None,
    test_patch: str = "",
) -> list[SuspectLocation]:
    """进入 LLM localize 前写入 state，超时也可带走。"""
    related: list[str] = []
    ctx = getattr(state, "retrieved_context", None)
    if ctx is not None:
        related = list(ctx.related_tests or [])
    fail_nids = list(state.node_timings.get("verify_failed_nodeids") or [])
    patch = test_patch or _test_patch_text(state)
    suspects = rule_first_suspects(
        state.issue_input or "",
        repo_root,
        state.repair_plan,
        fallback_from_plan=fallback_from_plan,
        related_tests=related,
        fail_nodeids=fail_nids,
        test_patch=patch,
        state=state,
    )
    if suspects:
        state.suspect_locations = list(suspects)
        state.node_timings["localize_rule_first"] = {
            "count": len(suspects),
            "top": [s.file_path for s in suspects[:3]],
            "f2p_seeded": any((s.reason or "").startswith("F2P") for s in suspects),
            "test_patch_seeded": any(
                (s.reason or "") == "test_patch覆盖" for s in suspects
            ),
        }
    return suspects
