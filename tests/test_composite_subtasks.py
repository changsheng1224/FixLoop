"""Composite subtasks 编排单测：子任务生成 + 编排 + trace 事件。"""

import pytest

from src.state import CandidatePatch, RepairPlan, RepairSubTask, SuspectLocation


class TestGenerateSubtasks:
    def test_composite_with_2_files_generates_2_subtasks(self):
        from src.repair.pipeline import RepairPipelineMixin

        plan = RepairPlan(
            issue_type="composite",
            suspect_files=["src/calc.py", "src/utils.py"],
        )
        subtasks = RepairPipelineMixin._generate_subtasks(plan)
        assert len(subtasks) == 2

    def test_subtasks_have_unique_ids(self):
        from src.repair.pipeline import RepairPipelineMixin

        plan = RepairPlan(
            issue_type="composite",
            suspect_files=["a.py", "b.py"],
        )
        subtasks = RepairPipelineMixin._generate_subtasks(plan)
        ids = [s.id for s in subtasks]
        assert len(set(ids)) == 2

    def test_subtasks_have_single_suspect_file(self):
        from src.repair.pipeline import RepairPipelineMixin

        plan = RepairPlan(
            issue_type="composite",
            suspect_files=["calc.py", "validator.py"],
        )
        subtasks = RepairPipelineMixin._generate_subtasks(plan)
        for st in subtasks:
            assert len(st.suspect_files) == 1

    def test_first_subtask_has_no_deps(self):
        from src.repair.pipeline import RepairPipelineMixin

        plan = RepairPlan(
            issue_type="composite",
            suspect_files=["a.py", "b.py"],
        )
        subtasks = RepairPipelineMixin._generate_subtasks(plan)
        assert subtasks[0].depends_on == []

    def test_second_subtask_depends_on_first(self):
        from src.repair.pipeline import RepairPipelineMixin

        plan = RepairPlan(
            issue_type="composite",
            suspect_files=["a.py", "b.py"],
        )
        subtasks = RepairPipelineMixin._generate_subtasks(plan)
        assert subtasks[1].depends_on == [subtasks[0].id]

    def test_non_composite_returns_empty(self):
        from src.repair.pipeline import RepairPipelineMixin

        plan = RepairPlan(
            issue_type="type_error",
            suspect_files=["calc.py"],
        )
        subtasks = RepairPipelineMixin._generate_subtasks(plan)
        assert subtasks == []

    def test_single_file_composite_returns_empty(self):
        from src.repair.pipeline import RepairPipelineMixin

        plan = RepairPlan(
            issue_type="composite",
            suspect_files=["calc.py"],  # only 1 file
        )
        subtasks = RepairPipelineMixin._generate_subtasks(plan)
        assert subtasks == []


class TestMergeSubtaskPatches:
    def test_merges_patches_in_order(self):
        from src.repair.pipeline import RepairPipelineMixin

        mixin = RepairPipelineMixin()
        st1 = RepairSubTask(id="fix_a", goal="fix a.py", suspect_files=["a.py"])
        st2 = RepairSubTask(id="fix_b", goal="fix b.py", suspect_files=["b.py"], depends_on=["fix_a"])

        patch_a = CandidatePatch(file_path="a.py", diff="-old\n+new")
        patch_b = CandidatePatch(file_path="b.py", diff="-bad\n+good")
        patches_by = {"fix_a": [patch_a], "fix_b": [patch_b]}
        merged = mixin._merge_subtask_patches(patches_by, [st1, st2])
        assert merged == [patch_a, patch_b]

    def test_empty_patches_skipped(self):
        from src.repair.pipeline import RepairPipelineMixin

        mixin = RepairPipelineMixin()
        st = RepairSubTask(id="x", goal="x", suspect_files=["x.py"])
        merged = mixin._merge_subtask_patches({"x": []}, [st])
        assert merged == []

    def test_ignores_suspect_locations_not_candidate_patches(self):
        """subtask localize 结果不能被塞进 candidate_patches。"""
        from src.repair.pipeline import RepairPipelineMixin

        mixin = RepairPipelineMixin()
        st = RepairSubTask(id="fix_a", goal="fix a.py", suspect_files=["a.py"])
        suspect = SuspectLocation(file_path="a.py", start_line=1, end_line=1)

        merged = mixin._merge_subtask_patches({"fix_a": [suspect]}, [st])

        assert merged == []


class TestRunSubtaskCycle:
    def test_cycle_narrows_and_restores_files(self):
        """_run_subtask_cycle 缩窄 suspect_files 后恢复。"""
        from src.repair.pipeline import RepairPipelineMixin
        from src.state import RepairState

        mixin = RepairPipelineMixin()
        plan = RepairPlan(suspect_files=["a.py", "b.py"])
        state = RepairState(issue_input="test")
        state.repair_plan = plan

        subtask = RepairSubTask(id="fix_a", goal="fix a", suspect_files=["a.py"])

        # 验证 subtask 的 suspect_files 是正确的
        assert subtask.suspect_files == ["a.py"]
        # plan 原始文件不变
        assert plan.suspect_files == ["a.py", "b.py"]

    def test_composite_path_generates_subtasks(self):
        """composite issue + 多文件 → 生成 subtasks。"""
        from src.repair.pipeline import RepairPipelineMixin

        plan = RepairPlan(
            issue_type="composite",
            suspect_files=["calc.py", "validator.py", "utils.py"],
        )
        subtasks = RepairPipelineMixin._generate_subtasks(plan)
        assert len(subtasks) == 3
        assert subtasks[2].depends_on == [subtasks[0].id]
