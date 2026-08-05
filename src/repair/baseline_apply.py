"""Single-Agent baseline 补丁应用（eval + degrade 共用）。"""

from __future__ import annotations

from pathlib import Path

from agent_runtime.runtime import Agent
from src.eval.runner import should_include_in_eval_diff
from src.repair.patch_applier import PatchApplier, parse_patches
from src.repair.repo_snapshot import repo_changed, snapshot_repo
from src.repair.termination import mark_fixed_skip_verify
from src.state import RepairState

_SCHEMA_RETRY_SUFFIX = (
    "\n\nYour previous reply produced no parseable patches and no file changes. "
    "Reply with ONLY a JSON array of patch objects "
    "(keys: file_path, original_lines, patched_lines, and/or diff). "
    "No markdown fences, no explanation."
)


def snapshot_baseline_sources(repo: Path) -> dict[str, str]:
    return snapshot_repo(repo, include=should_include_in_eval_diff)


def baseline_sources_changed(repo: Path, before: dict[str, str]) -> bool:
    return repo_changed(repo, before, include=should_include_in_eval_diff)


def _ask_for_patches(agent: Agent, prompt: str) -> str:
    """Prefer complete_once for structured patch JSON; fall back to ask."""
    complete_once = getattr(agent, "complete_once", None)
    if callable(complete_once):
        return complete_once(prompt)
    return agent.ask(prompt)


def apply_baseline_answer(
    agent: Agent,
    repo_root: str,
    prompt: str,
    state: RepairState,
    *,
    repo_before_apply: dict[str, str] | None = None,
    mark_fixed_on_apply: bool = True,
) -> dict[str, str]:
    """Single-Agent ask → 解析补丁 → 写盘，结果写入 *state*。"""
    repo = Path(repo_root)
    before = repo_before_apply if repo_before_apply is not None else snapshot_baseline_sources(repo)
    try:
        answer = agent.ask(prompt)
        patches = parse_patches(answer)
        # E18: ask 未产出可解析补丁且未改文件 → 一次 schema 微重试（与 patcher 同级机制）
        if not patches and not baseline_sources_changed(repo, before):
            try:
                answer_retry = _ask_for_patches(agent, prompt + _SCHEMA_RETRY_SUFFIX)
                patches = parse_patches(answer_retry)
                if patches:
                    state.node_timings["baseline_parse_recovered"] = True
                else:
                    from src.repair.loose_patch_recover import recover_patches_from_text

                    patches = recover_patches_from_text(answer_retry) or recover_patches_from_text(
                        answer
                    )
                    if patches:
                        state.node_timings["baseline_loose_recovered"] = True
            except Exception as retry_exc:
                state.agent_errors["baseline_parse_retry"] = str(retry_exc)[:200]

        state.candidate_patches = patches
        if patches:
            applier = PatchApplier(repo_root)
            applied = applier.apply_patches(patches)
            apply_errors = list(getattr(applier, "last_apply_errors", None) or [])
            if apply_errors:
                state.agent_errors["baseline_apply"] = "; ".join(apply_errors[:5])
            if applied or baseline_sources_changed(repo, before):
                if mark_fixed_on_apply:
                    mark_fixed_skip_verify(state)
            else:
                state.status = "failed"
                state.agent_errors["baseline"] = (
                    "patches parsed but not applied"
                    + (f": {apply_errors[0]}" if apply_errors else "")
                )
        elif baseline_sources_changed(repo, before):
            if mark_fixed_on_apply:
                mark_fixed_skip_verify(state)
        else:
            state.node_timings["patcher_parse_failed"] = True
            state.status = "failed"
            state.agent_errors["baseline"] = "no patches in agent output"
    except Exception as exc:
        state.status = "failed"
        state.agent_errors["baseline"] = str(exc)
    return before
