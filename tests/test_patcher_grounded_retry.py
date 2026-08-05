"""Grounded retry / loose recover 标记行为（无真模型）。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from src.orchestrator import Orchestrator
from src.repair.loose_patch_recover import recover_patches_from_text
from src.state import CandidatePatch, RepairState, SuspectLocation


class _FakePatcher:
    def __init__(self, answer: str):
        self._answer = answer
        self.config = SimpleNamespace(max_steps=12, prompt_budget=None)
        self.model_client = None

    def complete_once(self, prompt, system_prompt=None):
        return self._answer


def test_grounded_retry_applies_json():
    raw = tempfile.mkdtemp(prefix="fixloop-grtry-")
    root = Path(raw)
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")

    orch = Orchestrator.__new__(Orchestrator)
    orch._repo_root = str(root)
    orch.patcher = _FakePatcher(
        '[{"file_path": "pkg/mod.py", "original_lines": "VALUE = 1\\n", '
        '"patched_lines": "VALUE = 2\\n"}]'
    )
    orch._last_apply_errors = []
    orch._last_sibling_warnings = []

    state = RepairState(issue_input="x")
    state.suspect_locations = [
        SuspectLocation(
            file_path="pkg/mod.py",
            start_line=1,
            end_line=1,
            reason="F2P覆盖",
            confidence=0.9,
        )
    ]

    applied, meta = orch._run_patcher_grounded_retry(state, "fix me")
    assert applied
    assert meta.get("edit_mode") == "grounded_retry"
    assert state.node_timings.get("patcher_grounded_retry") is True
    assert (pkg / "mod.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_grounded_retry_skips_without_impl():
    raw = tempfile.mkdtemp(prefix="fixloop-grtry-empty-")
    orch = Orchestrator.__new__(Orchestrator)
    orch._repo_root = raw
    orch.patcher = _FakePatcher("should not be called")
    state = RepairState(issue_input="x")
    state.suspect_locations = []
    applied, meta = orch._run_patcher_grounded_retry(state, "fix")
    assert applied == []
    assert meta == {}


def test_loose_recover_applyable_fields():
    patches = recover_patches_from_text(
        "```diff\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n```"
    )
    assert isinstance(patches[0], CandidatePatch)
    assert patches[0].diff.startswith("diff --git") or "---" in patches[0].diff
