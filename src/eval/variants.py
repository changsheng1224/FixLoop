"""消融实验 Orchestrator 变体工厂。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from agent_runtime.workspace import WorkspaceContext

from src.agents.localizer import create_localizer
from src.agents.patcher import create_patcher
from src.eval.baseline import make_single_agent_factory
from src.eval.fake_runner import fake_orchestrator_factory
from src.eval.runner import DEFAULT_CASES_DIR
from src.orchestrator import Orchestrator
from src.repair_factory import create_model_client, make_orchestrator_factory, try_create_verifier
from src.state import RepairState, RetrievedContext, SuspectLocation


class NoRetrieverOrchestrator(Orchestrator):
    """3-Agent 变体：Localizer → Patcher → Verifier，跳过 Retriever。"""

    def _run_localize_and_retrieve(
        self,
        state: RepairState,
    ) -> tuple[list[SuspectLocation], RetrievedContext, dict, dict]:
        plan = state.repair_plan
        issue = state.issue_input
        prompt = self._localizer_prompt(plan, issue)
        answer, loc_timing = self._run_agent(
            self.localizer,
            prompt,
            "localizer",
            state,
        )
        suspects = self._parse_suspect_list(answer)
        if not suspects:
            suspects = self._fallback_suspects_from_plan(plan, issue)
        empty_ctx = RetrievedContext()
        ret_timing = {"total_ms": 0, "internal": {}}
        return suspects, empty_ctx, loc_timing, ret_timing


def make_no_retriever_factory(
    *,
    skip_verify: bool = False,
    dry_run: bool = False,
    model_client=None,
) -> Callable[[str], NoRetrieverOrchestrator]:
    """返回 `(repo_path) -> NoRetrieverOrchestrator` 工厂。"""

    client = create_model_client(model_client)

    def factory(repo_path: str) -> NoRetrieverOrchestrator:
        ws = WorkspaceContext.build(repo_path)
        repo = str(Path(repo_path).resolve())
        localizer = create_localizer(client, ws, cwd=repo)
        patcher = create_patcher(client, ws, cwd=repo)
        if dry_run:
            localizer.dry_run = True
            patcher.dry_run = True
        orch = NoRetrieverOrchestrator(localizer, None, patcher)
        if not skip_verify:
            verifier = try_create_verifier(client, ws, repo)
            if verifier:
                orch.verifier = verifier
        return orch

    return factory


def build_ablation_variants(
    *,
    fake: bool = False,
    skip_verify: bool = True,
    model_client=None,
    cases_dir: str | Path | None = None,
    variant_names: list[str] | None = None,
) -> dict[str, Callable[[str], object]]:
    """构建 full / single / no_retriever 三组消融变体工厂。"""
    cases_path = Path(cases_dir or DEFAULT_CASES_DIR)
    if fake:
        factory = fake_orchestrator_factory(cases_path)
        all_variants = {"full": factory, "single": factory, "no_retriever": factory}
    else:
        all_variants = {
            "full": make_orchestrator_factory(
                skip_verify=skip_verify,
                model_client=model_client,
            ),
            "single": make_single_agent_factory(model_client=model_client),
            "no_retriever": make_no_retriever_factory(
                skip_verify=skip_verify,
                model_client=model_client,
            ),
        }

    if not variant_names:
        return all_variants

    unknown = sorted(set(variant_names) - set(all_variants))
    if unknown:
        raise ValueError(f"unknown variant(s): {', '.join(unknown)}")
    return {name: all_variants[name] for name in variant_names}
