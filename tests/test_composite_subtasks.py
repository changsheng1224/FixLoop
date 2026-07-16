"""Composite subtasks 编排单测：子任务生成 + 编排 + trace 事件。"""

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
        st2 = RepairSubTask(
            id="fix_b", goal="fix b.py", suspect_files=["b.py"], depends_on=["fix_a"]
        )

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

        RepairPipelineMixin()
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


class TestCompositeSubtaskPreparation:
    def test_subtask_suspects_are_merged_back_to_main_state(self):
        """Composite subtask 定位结果应进入主 state，供统一 patch loop 使用。"""
        from src.repair.pipeline import RepairPipelineMixin
        from src.state import RepairState

        class DummyPipeline(RepairPipelineMixin):
            def _run_subtask_cycle(self, state, subtask, suspects_by_subtask):
                suspect = SuspectLocation(
                    file_path=subtask.suspect_files[0],
                    start_line=1,
                    end_line=3,
                    function_name=f"fn_{subtask.id}",
                )
                suspects_by_subtask[subtask.id] = [suspect]
                return [suspect]

        pipeline = DummyPipeline()
        state = RepairState(issue_input="composite")
        state.repair_plan = RepairPlan(
            issue_type="composite",
            suspect_files=["calc.py", "validator.py"],
        )

        prepared = pipeline._prepare_composite_subtasks(state)

        assert prepared is True
        assert [s.file_path for s in state.suspect_locations] == ["calc.py", "validator.py"]
        assert state.candidate_patches == []
        assert state.status == "pending"

    def test_duplicate_subtask_suspects_are_deduplicated(self):
        """同一定位结果被多个 subtask 命中时只保留一份。"""
        from src.repair.pipeline import RepairPipelineMixin
        from src.state import RepairState

        duplicate = SuspectLocation(file_path="shared.py", start_line=10, end_line=12)

        class DummyPipeline(RepairPipelineMixin):
            def _run_subtask_cycle(self, state, subtask, suspects_by_subtask):
                suspects_by_subtask[subtask.id] = [duplicate]
                return [duplicate]

        pipeline = DummyPipeline()
        state = RepairState(issue_input="composite")
        state.repair_plan = RepairPlan(
            issue_type="composite",
            suspect_files=["a.py", "b.py"],
        )

        assert pipeline._prepare_composite_subtasks(state) is True
        assert state.suspect_locations == [duplicate]

    def test_composite_without_subtask_suspects_is_failed_not_pending(self):
        """subtask 全部无定位结果时不能泄漏 pending 终态。"""
        from src.repair.pipeline import RepairPipelineMixin
        from src.state import RepairState

        class DummyPipeline(RepairPipelineMixin):
            def _run_subtask_cycle(self, state, subtask, suspects_by_subtask):
                suspects_by_subtask[subtask.id] = []
                return []

        pipeline = DummyPipeline()
        state = RepairState(issue_input="composite")
        state.repair_plan = RepairPlan(
            issue_type="composite",
            suspect_files=["a.py", "b.py"],
        )

        assert pipeline._prepare_composite_subtasks(state) is False
        assert state.status == "failed"
        assert "subtask_no_suspects" in state.failure_tags
