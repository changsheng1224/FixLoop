"""Offline fixture model; all execution uses the formal repair service.

Expected patches are fixture outputs only, never supplied to a real model.
"""

from pathlib import Path

from agent_runtime.model_turn import FinishKind, ModelTurnResult, ProviderFinish, ToolCall
from agent_runtime.patch_engine import parse_unified_patch
from agent_runtime.providers.clients import FakeModelClient
from src.eval.case_io import build_case_issue, read_expected_patch
from src.repair.assembly import create_repair_service


class FixtureModelClient(FakeModelClient):
    def __init__(self, cases_dir, repo_path):
        super().__init__([])
        self.cases_dir = cases_dir
        self.case = None
        root = Path(repo_path)
        for case in cases_dir.iterdir():
            source = case / "repo"
            if not source.is_dir():
                continue
            candidates = [
                p
                for p in source.rglob("*")
                if p.is_file()
                and not any(
                    part.startswith(".") or part == "__pycache__"
                    for part in p.relative_to(source).parts
                )
            ]
            if candidates and all(
                (root / p.relative_to(source)).is_file()
                and (root / p.relative_to(source)).read_bytes() == p.read_bytes()
                for p in candidates
            ):
                if self.case is not None:
                    raise ValueError("ambiguous offline fixture workspace")
                self.case = case
        self.actions = []

    @property
    def capabilities(self):
        from agent_runtime.providers.contracts import ProviderCapabilities

        return ProviderCapabilities(provider="fake", model="fixture", native_tools=True, usage=True)

    def complete(self, prompt, **kwargs):
        for case in self.cases_dir.iterdir():
            if case.is_dir() and case.name.startswith("case_"):
                issue = build_case_issue(case)
                if issue.split("\n\nCandidate source files:")[0] in prompt:
                    self.case = case
                    break
        result = '{"conclusion":"Use the reported source and verify the change."}'
        self._record_usage(prompt, result)
        return result

    def complete_turn(self, request):
        if not self.actions:
            if self.case is None:
                self.complete(request.system_prompt + str(request.messages))
            if self.case is None:
                raise ValueError("offline fixture issue does not match an installed case")
            patches = parse_unified_patch(read_expected_patch(self.case))
            operations = ["*** Begin Patch"]
            for patch in patches:
                if patch.action != "add":
                    self.actions.append(ToolCall("read_file", {"path": patch.path}))
                label = {"add": "Add", "delete": "Delete", "update": "Update"}[patch.action]
                operations.append(f"*** {label} File: {patch.path}")
                if patch.action != "delete":
                    for hunk in patch.plan.hunks:
                        if patch.action != "add":
                            operations.append(hunk.header)
                        for index, (kind, line) in enumerate(hunk.lines):
                            operations.append(kind + line)
                            if index in hunk.no_newline:
                                operations.append("\\ No newline at end of file")
            operations.append("*** End Patch")
            self.actions.extend(
                [ToolCall("apply_patch", {"patch": "\n".join(operations) + "\n"}), None]
            )
        action = self.actions.pop(0)
        if action is None:
            self.actions = [None]
            return ModelTurnResult(
                text="Fixture changes applied; run the real verification.",
                finish=ProviderFinish(FinishKind.TEXT_COMPLETE),
            )
        return ModelTurnResult(
            tool_calls=[ToolCall(action.name, action.arguments, f"fixture-{len(self.actions)}")],
            finish=ProviderFinish(FinishKind.TOOL_CALLS),
        )


def fixture_repair_service_factory(cases_dir):
    cases = Path(cases_dir)

    def factory(repo_path):
        return create_repair_service(
            FixtureModelClient(cases, repo_path),
            repo_path,
            workspace_policy="provided",
            execution_tier="host",
            runtime_overrides={
                "provider": "fake",
                "model": "gpt-4o",
                "auto_context_extraction": False,
            },
        )

    factory.offline_fixture = True
    return factory
