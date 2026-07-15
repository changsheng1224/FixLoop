"""Blackboard 前缀订阅单测。"""

from src.blackboard import Blackboard
from src.prompts.patcher_task_builder import assemble_patcher_variables
from src.repair.blackboard_merge import (
    write_feedback_to_blackboard,
    write_localize_phase_to_blackboard,
)
from src.repair.blackboard_subscribe import (
    PATCHER_PREFIX_SUBSCRIPTIONS,
    render_patcher_prefix_blocks,
    subscribe_prefixes,
)
from src.state import RepairPlan, RetrievedContext, SuspectLocation


class TestSubscribePrefixes:
    def test_subscribe_prefixes_batch_read(self):
        bb = Blackboard()
        bb.write(
            "suspect:a.py:1", {"file_path": "a.py", "start_line": 1, "end_line": 1}, "localizer"
        )
        bb.write("context:related_tests", ["test_a.py"], "retriever")
        result = subscribe_prefixes(bb, ["suspect:", "context:"])
        assert len(result["suspect:"]) == 1
        assert result["context:"]["context:related_tests"] == ["test_a.py"]


class TestRenderPatcherPrefixBlocks:
    def test_renders_suspects_and_context(self, temp_workspace):
        (temp_workspace / "calc.py").write_text("line41\nline42\nline43\n", encoding="utf-8")
        bb = Blackboard()
        write_localize_phase_to_blackboard(
            bb,
            [SuspectLocation(file_path="calc.py", start_line=2, end_line=2, reason="stack")],
            RetrievedContext(related_tests=["test_calc.py"]),
        )

        def read_snippet(path, start, end):
            full = temp_workspace / path
            if not full.is_file():
                return ""
            lines = full.read_text(encoding="utf-8").splitlines()
            return "\n".join(lines[start - 1 : end])

        def read_test_context(ctx, suspects, plan):
            if ctx and ctx.related_tests:
                return [f"  {ctx.related_tests[0]}:"]
            return []

        blocks = render_patcher_prefix_blocks(
            bb,
            read_snippet=read_snippet,
            read_test_context=read_test_context,
        )
        assert "calc.py:2" in blocks.suspects_block
        assert "line42" in blocks.suspects_block
        assert "test_calc.py" in blocks.test_blocks
        assert blocks.subscribed_prefixes == [s.prefix for s in PATCHER_PREFIX_SUBSCRIPTIONS]

    def test_scratch_feedback_in_block(self):
        bb = Blackboard()
        write_feedback_to_blackboard(bb, "assert failed")
        blocks = render_patcher_prefix_blocks(
            bb,
            read_snippet=lambda *_: "",
            read_test_context=lambda *_: [],
        )
        assert blocks.scratch_block == "assert failed"


class TestAssemblePatcherWithBlackboard:
    def test_uses_prefix_subscription_when_blackboard_present(self, temp_workspace):
        (temp_workspace / "app.py").write_text("bug\n", encoding="utf-8")
        bb = Blackboard()
        write_localize_phase_to_blackboard(
            bb,
            [SuspectLocation(file_path="app.py", start_line=1, end_line=1, reason="err")],
            None,
        )

        variables, _render, meta = assemble_patcher_variables(
            suspects=[],
            context=None,
            feedback="",
            plan=RepairPlan(suspect_files=["app.py"]),
            issue="TypeError at app.py:1",
            read_snippet=lambda p, s, e: "snippet",
            read_test_context=lambda *_: [],
            fallback_suspects=lambda plan, issue: [],
            blackboard=bb,
        )
        assert "app.py:1" in variables["suspects_block"]
        assert meta is not None
        assert "suspect:" in meta["subscribed_prefixes"]
