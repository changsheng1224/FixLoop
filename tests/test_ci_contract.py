"""The CI workflows must invoke modules that can actually run.

`eval.yml` ran `python -m src.eval.runner`, but `runner.py` is a library module
with no `__main__` guard: the step exited 0 having evaluated nothing, and the
regression check that followed compared a report that was never written. These
tests resolve what each workflow asks for against the source tree, and keep the
eval baseline describing the run the gate actually performs.
"""

import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))
# Third-party entry points the workflows also use are not ours to resolve.
_FIRST_PARTY = ("src.", "agent_runtime.")


def _invocable(module: str) -> tuple[bool, str]:
    """`python -m X` needs a package `__main__.py` or a module-level guard."""
    target = REPO.joinpath(*module.split("."))
    if (target / "__main__.py").is_file():
        return True, f"{module}/__main__.py"
    source = target.with_suffix(".py")
    if not source.is_file():
        return False, f"{module}.py does not exist"
    text = source.read_text(encoding="utf-8")
    if '__name__ == "__main__"' in text or "__name__ == '__main__'" in text:
        return True, f"{module}.py"
    return False, f"{module}.py has no __main__ guard"


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=[p.name for p in WORKFLOWS])
def test_every_first_party_module_the_workflow_invokes_is_runnable(workflow):
    text = workflow.read_text(encoding="utf-8")
    modules = {
        found
        for found in re.findall(r"python\s+-m\s+([A-Za-z_][\w.]*)", text)
        if found.startswith(_FIRST_PARTY)
    }
    for module in sorted(modules):
        ok, detail = _invocable(module)
        assert ok, f"{workflow.name}: python -m {module} -> {detail}"


class TestEvalBaseline:
    @pytest.fixture
    def baseline(self):
        path = REPO / "src" / "eval" / "ci_baseline_report.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_the_gate_has_the_fields_it_compares(self, baseline):
        """regression_check compares these two rates and nothing else."""
        summary = baseline["summary"]
        assert "fix_rate" in summary
        assert "regression_rate" in summary

    def test_it_covers_the_case_set_that_ci_runs(self, baseline):
        from src.eval.case_io import OFFLINE_SMOKE_CASES

        assert tuple(baseline["meta"]["cases"]) == tuple(OFFLINE_SMOKE_CASES)
        assert baseline["summary"]["total"] == len(OFFLINE_SMOKE_CASES)

    def test_it_is_self_describing_and_run_independent(self, baseline):
        assert baseline["meta"]["generated_by"] == "python -m src.eval --ci"
        # A committed baseline must not carry this machine's paths or run ids.
        assert "trace_path" not in baseline
