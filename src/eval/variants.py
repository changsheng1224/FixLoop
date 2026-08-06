"""消融实验 Orchestrator 变体工厂。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from src.eval.fake_runner import fake_orchestrator_factory
from src.eval.runner import DEFAULT_CASES_DIR
from src.repair_factory import create_model_client, make_orchestrator_factory


class NaiveOrchestrator:
    """Naive 单轮基线：单次 LLM complete，无 tool schema，无 verify。

    与 Full Multi-Agent 对比，体现工具编排和分工的价值。
    """

    def __init__(self, client, repo_root: str = ""):
        self.client = client
        self._repo_root = repo_root

    def repair(self, issue: str, max_retries: int = 0, **kwargs):
        import time

        from src.repair.verification.termination import finalize_repair_state
        from src.state import RepairState

        state = RepairState(issue_input=issue)
        t0 = time.time()

        try:
            raw = self.client.complete(
                f"Fix this bug. Output only the corrected code as a unified diff:\n\n{issue}",
                max_new_tokens=1024,
            )
            # 尝试提取 patch
            from src.repair.execution.patch_applier import parse_patches

            patches = parse_patches(raw)
            if patches:
                state.candidate_patches = patches
                state.status = "patched"
            else:
                state.status = "failed"
        except Exception as e:
            state.status = "failed"
            state.agent_errors["naive"] = str(e)

        state.node_timings["total_ms"] = int((time.time() - t0) * 1000)
        finalize_repair_state(state)
        return state


def make_naive_factory(
    *,
    model_client=None,
) -> Callable[[str], NaiveOrchestrator]:
    """返回 `(repo_path) -> NaiveOrchestrator` 工厂。"""
    client = create_model_client(model_client)

    def factory(repo_path: str) -> NaiveOrchestrator:
        return NaiveOrchestrator(client, repo_root=repo_path)

    return factory


def build_ablation_variants(
    *,
    fake: bool = False,
    skip_verify: bool = False,
    model_client=None,
    cases_dir: str | Path | None = None,
    variant_names: list[str] | None = None,
) -> dict[str, Callable[[str], object]]:
    """构建 runtime / naive 两组面试消融变体。"""
    cases_path = Path(cases_dir or DEFAULT_CASES_DIR)
    if fake:
        factory = fake_orchestrator_factory(cases_path)
        all_variants = {
            "runtime": factory,
            "naive": factory,
        }
    else:
        all_variants = {
            "runtime": make_orchestrator_factory(
                skip_verify=skip_verify,
                model_client=model_client,
            ),
            "naive": make_naive_factory(model_client=model_client),
        }

    if not variant_names:
        return all_variants

    unknown = sorted(set(variant_names) - set(all_variants))
    if unknown:
        raise ValueError(f"unknown variant(s): {', '.join(unknown)}")
    return {name: all_variants[name] for name in variant_names}
