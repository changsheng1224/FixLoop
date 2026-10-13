from __future__ import annotations

import time

from agent_runtime.logging_setup import get_logger
from src.repair.prompt_router import repair_plan_intent_snapshot
from src.repair.timing_schema import set_phase_ms
from src.state import (
    RepairState,
    RetrievedContext,
    SuspectLocation,
)

log = get_logger("repair.pipeline")


log = get_logger("orchestrator")

DEFAULT_REPAIR_TIMEOUT_S = 180


class ExplorationService:
    def __init__(self, session):
        self.session = session

    def issue_preparation(self):
        from src.repair.issue_preparation import IssuePreparationService

        return IssuePreparationService(
            self.session.repo_root,
            read_source=lambda path: self.read_line_range(path, 1, 10000),
            parse_line=self.parse_file_line,
            patcher=self.session.patcher,
            classify=self.classify_issue_type,
            light_client=getattr(self.session, "light_client", None),
        )

    def parse_issue(self, issue):
        from agent_runtime.intent.models import RouteContext

        ctx = self.session.repair_ctx
        tracer = ctx.repair_tracer if ctx else None
        plan, self.session.last_intent_result = self.issue_preparation().prepare(
            issue,
            RouteContext(
                channel="repair",
                light_client=getattr(self.session, "light_client", None),
                emit=lambda name, payload: (
                    tracer.emit("orchestrator", name, payload) if tracer else None
                ),
                cancel_token=ctx.cancel_token if ctx else None,
                budget=getattr(self.session, "model_budget", None),
                deadline=getattr(self.session, "model_deadline", None),
                model_input_limit=self.session.patcher.config.input_window(384)
                if getattr(self.session, "model_budget", None) is not None
                else None,
            ),
        )
        return plan

    @staticmethod
    def classify_issue_type(issue):
        from src.repair.issue_preparation import IssuePreparationService

        return IssuePreparationService.classify_issue_type(issue)

    def llm_classify_issue(self, issue):
        return self.issue_preparation().classify_with_model(issue)

    @staticmethod
    def parse_file_line(issue, file_path):
        from src.repair.issue_preparation import IssuePreparationService

        return IssuePreparationService.parse_file_line(issue, file_path)

    def fallback_suspects_from_plan(self, plan, issue):
        return self.issue_preparation().fallback_suspects_from_plan(plan, issue)

    def find_import_line_number(self, file_path):
        return self.issue_preparation().find_import_line_number(file_path)

    def match_skill(self, issue: str, *, language: str = "python"):
        """从 YAML Skill 文件中匹配 Issue 对应的修复策略。"""
        from src.skills.matcher import match_skill_semantic

        return match_skill_semantic(issue, language=language)

    def context_reader(self):
        from src.repair.context_reader import RepairContextReader

        return RepairContextReader(self.session.repo_root)

    def read_code_snippet(self, file_path, start_line, end_line):
        return self.context_reader().read_code_snippet(file_path, start_line, end_line)

    def read_line_range(self, file_path, start_line, end_line):
        return self.context_reader().read_line_range(file_path, start_line, end_line)

    def read_test_context(self, context, suspects, plan=None):
        return self.context_reader().read_test_context(context, suspects, plan)

    @staticmethod
    def select_test_excerpt(content, target_names, *, max_chars, max_lines):
        from src.repair.context_reader import RepairContextReader

        return RepairContextReader.select_test_excerpt(
            content, target_names, max_chars=max_chars, max_lines=max_lines
        )

    def resolve_test_path(self, ref):
        return self.context_reader().resolve_test_path(ref)

    def guess_test_file(self, source_file, function_name):
        return self.context_reader().guess_test_file(source_file, function_name)

    def discover_repo_test_files(self):
        return self.context_reader().discover_repo_test_files()

    def seed_patcher_primary(
        self, state: RepairState
    ) -> tuple[list[SuspectLocation], RetrievedContext]:
        """规则种子 only：不调用 Localizer/Retriever。"""
        started = time.monotonic()
        from src.repair.execution.edit_lock import EditLockState
        from src.repair.localization.localize_fastpath import seed_rule_first_suspects
        from src.repair.localization.localize_quality import _is_test_path

        state.node_timings["repair_mode"] = "patcher_primary"
        state.node_timings["localize_skipped"] = True
        state.node_timings["retrieve_skipped"] = True
        from src.repair.phase_fsm import RepairPhaseFSM

        RepairPhaseFSM.from_state(state).apply(state, "seed", "primary localization seed")
        suspects = seed_rule_first_suspects(
            state,
            self.session.repo_root,
            fallback_from_plan=self.fallback_suspects_from_plan,
            max_keep=5,
            enable_semantic_expand=False,
        )
        if not suspects:
            state.suspect_locations = []
            state.node_timings["primary_seed_empty"] = True
        context = state.retrieved_context or RetrievedContext()
        state.retrieved_context = context
        allowed: list[str] = []
        for s in state.suspect_locations or []:
            fp = (s.file_path or "").replace("\\", "/")
            if not fp or _is_test_path(fp):
                continue
            if not fp.endswith(".py"):
                continue
            allowed.append(fp)
        plan = state.repair_plan
        if plan is not None:
            for fp in plan.suspect_files or []:
                if not fp or _is_test_path(fp):
                    continue
                allowed.append(fp.replace("\\", "/"))
        allowed_unique = list(dict.fromkeys(allowed))[:8]
        lock = EditLockState(
            repo_root=self.session.repo_root,
            allowed_edit=set(),
            max_auto_allow=5,
            sandbox_only=bool(
                self.session.patcher is not None
                and (
                    self.session.patcher.tool_context.sandbox_task_binding is not None
                    or self.session.patcher.tool_context.sandbox_backend is not None
                )
            ),
        )
        lock.seed_and_preread(allowed_unique)
        if not lock.allowed_edit:
            state.node_timings["primary_seed_empty_lock"] = True
        lock.require_expand_before_auto = False
        lock.write_serial = False
        self.session.edit_lock = lock
        if self.session.patcher is not None:
            self.session.patcher.tool_context.edit_lock = lock
        state.control.allowed_edit = sorted(lock.allowed_edit)
        state.node_timings["unread_write_reject_count"] = 0
        state.node_timings["apply_path_reject_count"] = 0
        set_phase_ms(state.node_timings, "context", int((time.monotonic() - started) * 1000))
        try:
            self.session.evidence.write_seed_context_to_blackboard(
                state, state.suspect_locations, context
            )
        except RuntimeError:
            pass
        em = self.session.events.progress_emitter()
        empty_note = " empty_lock→expand_lock" if not lock.allowed_edit else ""
        seed_payload = {
            "summary": (
                f"allowed_edit={len(lock.allowed_edit)} "
                f"suspects={len(state.suspect_locations or [])}{empty_note}"
            ),
            "allowed_edit": sorted(lock.allowed_edit),
        }
        em.emit("seed_ready", **seed_payload)
        self.session.events.emit_repair_span(
            "seed_span",
            {"summary": seed_payload["summary"], "allowed_edit_n": len(lock.allowed_edit)},
        )
        log.info(
            "[patcher_primary] seed ready allowed_edit=%d suspects=%d (Loc/Ret skipped)",
            len(lock.allowed_edit),
            len(state.suspect_locations or []),
        )
        return (list(state.suspect_locations or []), context)

    def prepare_repair(self, state: RepairState) -> None:
        """Prepare public issue context and baseline for a new repair."""
        ctx = self.session.asks.active_repair_ctx()
        issue = state.issue_input
        t0 = time.time()
        state.repair_plan = self.parse_issue(issue)
        parse_ms = int((time.time() - t0) * 1000)
        if state.repair_plan and state.repair_plan.language != "python":
            log.info(
                "检测到 language=%s（%s），Verifier 将使用语言感知静态验证",
                state.repair_plan.language,
                state.repair_plan.language_source,
            )
        t_skill = time.time()
        from src.skills.resolve import resolve_skill_for_plan, skill_matched_trace_payload

        matched = None
        skill_fallback = None
        if state.repair_plan:
            matched, skill_fallback = resolve_skill_for_plan(
                state.repair_plan,
                issue,
                language=state.repair_plan.language,
                match_skill_fn=self.match_skill,
            )
        skill_ms = int((time.time() - t_skill) * 1000)
        tracer = ctx.repair_tracer
        if state.repair_plan and tracer is not None:
            tracer.emit(
                "orchestrator", "prompt_routing", repair_plan_intent_snapshot(state.repair_plan)
            )
            tracer.emit(
                "orchestrator",
                "skill_matched",
                skill_matched_trace_payload(matched, skill_fallback)
                if skill_fallback is not None
                else {"matched_skill": None},
            )
            try:
                from src.skills.decision import build_canonical_skill_decision
                from src.skills.router import SkillRouter

                decision = SkillRouter().route(issue)
                tracer.emit("orchestrator", "skill_routed", decision.to_trace_payload())
                canonical = build_canonical_skill_decision(matched, decision)
                state.repair_plan.skill.canonical_decision = canonical.to_dict()
                tracer.emit("orchestrator", "skill_decided", canonical.to_dict())
                if canonical.fallback:
                    tracer.emit("orchestrator", "skill_fallback", canonical.to_dict())
            except Exception:
                pass
        state.node_timings["parse_issue_ms"] = parse_ms
        state.node_timings["skill_resolve_ms"] = skill_ms
        log.info("parse_issue: %dms, skill_resolve: %dms", parse_ms, skill_ms)
        if state.repair_plan and state.repair_plan.issue_type:
            from src.repair.precedent import RepairPrecedentStore

            store = RepairPrecedentStore(
                self.session.repo_root,
                access_context=getattr(self.session.patcher, "memory_access", None),
                state_root=str(
                    getattr(getattr(self.session.patcher, "tool_context", None), "state_root", "")
                    or ""
                ),
            )
            similar = store.load_similar(state.repair_plan.issue_type, query="", use_semantic=False)
            if similar:
                state.node_timings["similar_fixes"] = similar
            state.node_timings["precedent_semantic_skipped"] = True
        self.seed_patcher_primary(state)
        if (
            not self.session.lifecycle.abort_repair_if_cancelled(state)
            and self.session.verification.verification_enabled()
        ):
            self.session.verification.record_validation_exit(state, "baseline_pytest_code")
