"""消融实验 Orchestrator 变体工厂。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from src.eval.fake_runner import fixture_repair_service_factory
from src.eval.runner import DEFAULT_CASES_DIR
from src.repair.assembly import repair_service_factory


def build_ablation_variants(
    *,
    fake: bool = False,
    skip_verify: bool = False,
    model_client=None,
    cases_dir: str | Path | None = None,
    variant_names: list[str] | None = None,
) -> dict[str, Callable[[str], object]]:
    """构建使用正式修复服务的评测变体。"""
    cases_path = Path(cases_dir or DEFAULT_CASES_DIR)
    if fake:
        factory = fixture_repair_service_factory(cases_path)
        all_variants = {
            "runtime": factory,
        }
    else:
        all_variants = {
            "runtime": repair_service_factory(
                workspace_policy="provided",
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
