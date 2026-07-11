"""Single-Agent baseline 补丁应用（eval + degrade 共用）。"""

from __future__ import annotations

from pathlib import Path

from agent_runtime.runtime import Agent
from src.eval.runner import should_include_in_eval_diff
from src.repair.patch_applier import PatchApplier, parse_patches
from src.repair.repo_snapshot import repo_changed, snapshot_repo
from src.repair.termination import mark_fixed_skip_verify
from src.state import RepairState


def snapshot_baseline_sources(repo: Path) -> dict[str, str]:
    return snapshot_repo(repo, include=should_include_in_eval_diff)


def baseline_sources_changed(repo: Path, before: dict[str, str]) -> bool:
    return repo_changed(repo, before, include=should_include_in_eval_diff)


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
        state.candidate_patches = patches
        if patches:
            applier = PatchApplier(repo_root)
            applied = applier.apply_patches(patches)
            if applied or baseline_sources_changed(repo, before):
                if mark_fixed_on_apply:
                    mark_fixed_skip_verify(state)
            else:
                state.status = "failed"
                state.agent_errors["baseline"] = "patches parsed but not applied"
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
