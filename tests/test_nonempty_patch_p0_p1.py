"""P0+P1：抬高非空 patch — faithfulness soft、tier gate、路径解析、导出兜底。"""

from __future__ import annotations

from pathlib import Path

from src.benchmark.swebench.patch_export import export_model_patch
from src.repair.execution.patch_applier import PatchApplier
from src.repair.failure_tags import (
    allowed_patch_files,
    check_patch_faithfulness,
    promote_paths_to_suspects,
)
from src.repair.localization.localize_tiers import SuspectTier, decide_patch_gate, tier_for_suspect
from src.repair.path_resolve import resolve_repo_relpath
from src.repair.phase_clock import PhaseTimeoutConfig
from src.state import CandidatePatch, RepairState, SuspectLocation


def _tmp_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "django" / "contrib" / "auth").mkdir(parents=True)
    (root / "django" / "contrib" / "auth" / "validators.py").write_text(
        "ASCII = r'[A-Za-z]'\n", encoding="utf-8"
    )
    (root / "pkg").mkdir()
    (root / "pkg" / "core.py").write_text("x = 1\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_core.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    return root


class TestFaithfulnessSoft:
    def test_promotes_existing_impl_outside_allowed(self, tmp_path):
        root = _tmp_repo(tmp_path)
        state = RepairState(issue_input="x")
        state.node_timings["_repo_root_hint"] = str(root)
        state.suspect_locations = [
            SuspectLocation(file_path="pkg/core.py", start_line=1, end_line=1, reason="grep命中")
        ]
        patches = [
            CandidatePatch(
                file_path="django/contrib/auth/validators.py",
                original_lines="ASCII = r'[A-Za-z]'\n",
                patched_lines="ASCII = r'\\A[A-Za-z]\\Z'\n",
            )
        ]
        kept, rejected = check_patch_faithfulness(
            patches, state, soft_keep=True, repo_root=str(root)
        )
        assert len(kept) == 1
        assert kept[0].file_path.endswith("validators.py")
        assert state.node_timings.get("faithfulness_promoted") or state.node_timings.get(
            "faithfulness_soft"
        )
        assert "django/contrib/auth/validators.py" in allowed_patch_files(state)

    def test_still_rejects_missing_hallucination(self, tmp_path):
        root = _tmp_repo(tmp_path)
        state = RepairState(issue_input="x")
        state.node_timings["_repo_root_hint"] = str(root)
        state.suspect_locations = [
            SuspectLocation(file_path="pkg/core.py", start_line=1, end_line=1)
        ]
        patches = [CandidatePatch(file_path="evil_missing.py", diff="...")]
        kept, rejected = check_patch_faithfulness(
            patches, state, soft_keep=True, repo_root=str(root)
        )
        assert kept == []
        assert rejected == ["evil_missing.py"]


class TestTierGateLowImpl:
    def test_low_impl_allows_short_repair(self, tmp_path):
        root = _tmp_repo(tmp_path)
        low = SuspectLocation(
            file_path="pkg/core.py",
            start_line=1,
            end_line=1,
            reason="llm_guess",
            confidence=0.3,
        )
        assert tier_for_suspect(low, root) == SuspectTier.LOW
        d = decide_patch_gate([low], root)
        assert d.allow and d.force_short_repair
        assert d.reason == "low_tier_short_repair"

    def test_test_only_still_blocked(self, tmp_path):
        root = _tmp_repo(tmp_path)
        low = SuspectLocation(
            file_path="tests/test_core.py",
            start_line=1,
            end_line=1,
            reason="F2P测试",
            confidence=0.45,
        )
        d = decide_patch_gate([low], root)
        assert not d.allow
        assert d.reason == "no_editable_impl"


class TestPathSuffixResolve:
    def test_missing_package_prefix(self, tmp_path):
        root = _tmp_repo(tmp_path)
        rel = resolve_repo_relpath(root, "contrib/auth/validators.py")
        assert rel == "django/contrib/auth/validators.py"

    def test_applier_rewrites_path(self, tmp_path):
        root = _tmp_repo(tmp_path)
        applier = PatchApplier(str(root))
        patches = [
            CandidatePatch(
                file_path="contrib/auth/validators.py",
                original_lines="ASCII = r'[A-Za-z]'\n",
                patched_lines="ASCII = r'X'\n",
            )
        ]
        applied = applier.apply_patches(patches)
        assert len(applied) == 1
        assert applied[0].file_path == "django/contrib/auth/validators.py"
        text = (root / "django/contrib/auth/validators.py").read_text(encoding="utf-8")
        assert "ASCII = r'X'" in text


class TestPhaseBudgetReserve:
    def test_patch_floor_under_total_cap(self):
        cfg = PhaseTimeoutConfig.with_repair_total_cap(900)
        assert cfg.repair_total_s == 900
        assert cfg.patch_s >= 180
        assert cfg.localize_s + cfg.patch_s + cfg.verify_s <= 900 + 50  # soft check
        assert cfg.patch_s >= cfg.localize_s


class TestExportSuspectFallback:
    def test_exports_scoped_suspect_diff_without_candidates(self, tmp_path):
        _tmp_repo(tmp_path)
        original = tmp_path / "orig"
        modified = tmp_path / "mod"
        original.mkdir()
        modified.mkdir()
        (original / "pkg").mkdir()
        (modified / "pkg").mkdir()
        (original / "pkg" / "core.py").write_text("x = 1\n", encoding="utf-8")
        (modified / "pkg" / "core.py").write_text("x = 2\n", encoding="utf-8")
        (original / "noise.py").write_text("a\n", encoding="utf-8")
        (modified / "noise.py").write_text("b\n", encoding="utf-8")

        state = RepairState(
            issue_input="x",
            candidate_patches=[],
            suspect_locations=[
                SuspectLocation(file_path="pkg/core.py", start_line=1, end_line=1)
            ],
        )
        out = export_model_patch(
            state=state, original_repo=original, modified_repo=modified
        )
        assert "pkg/core.py" in out
        assert "noise.py" not in out
        assert out.strip()


class TestPromotePaths:
    def test_promote_adds_suspect(self, tmp_path):
        root = _tmp_repo(tmp_path)
        state = RepairState(issue_input="x")
        state.node_timings["_repo_root_hint"] = str(root)
        promoted = promote_paths_to_suspects(
            state, ["django/contrib/auth/validators.py"], repo_root=str(root)
        )
        assert promoted
        assert any(
            s.file_path.endswith("validators.py") for s in state.suspect_locations
        )
