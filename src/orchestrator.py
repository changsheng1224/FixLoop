"""Orchestrator：纯 Python 编排器（不调 LLM）。

工作流：Issue 解析与规则种子 → Patcher 工具环 → Critic → Verifier 回灌。
"""

import hashlib
import os
import re
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import contextmanager
from pathlib import Path

from agent_runtime.logging_setup import get_logger
from src.prompts.loader import load_role_prompt
from src.prompts.patcher_task_builder import assemble_patcher_variables
from src.prompts.repair_tasks import (
    build_verifier_variables,
    render_repair_task,
)
from src.repair.execution.patch_applier import PatchApplier, apply_patch_to_text
from src.repair.language_detect import detect_repair_language
from src.repair.output_parsers import (
    parse_verification,
)
from src.repair.phase_clock import PhaseTimeoutConfig
from src.repair.pipeline import RepairPipelineMixin
from src.repair.prompt_router import (
    ROUTED_ISSUE_TYPES,
    classify_exception,
    fallback_suspect_uses_import_line,
    patcher_variant_for,
)
from src.repair.repo_snapshot import restore_repo_snapshot, snapshot_repo
from src.repair.run_context import RepairRunContext
from src.repair.verification.verify import (
    DockerVerifyStrategy,
    ProfileVerifyStrategy,
    PytestVerifyStrategy,
    StaticVerifyStrategy,
    record_verify_timings,
)
from src.state import (
    CandidatePatch,
    RepairPlan,
    RepairState,
    RetrievedContext,
    SuspectLocation,
    VerificationResult,
)

log = get_logger("orchestrator")

__all__ = ["Orchestrator", "apply_patch_to_text"]

DEFAULT_REPAIR_TIMEOUT_S = 180

# issue_type 分类规则链（按序匹配，首次命中即返回）
_IssueTypeRule = namedtuple("_IssueTypeRule", ["name", "pattern", "issue_type"])

# 反馈环结构化 section（V1.4-Bonus8a）
_FeedbackSection = namedtuple("_FeedbackSection", ["title", "content", "priority", "max_len"])
_MAX_PATCH_DIFF_CHARS = 600
_MAX_FAILURE_LOG_CHARS = 300
_MAX_BUILD_LOG_CHARS = 300
_MAX_FAILURE_TARGET_CHARS = 500

_ISSUE_TYPE_RULES: list[_IssueTypeRule] = [
    # test_failure 必须在 explicit_exception 之前：
    # "FAILED test_app.py::test_add - AssertionError" 应识别为 test_failure 而非 type_error
    _IssueTypeRule(
        "test_failure",
        r"(?i)(FAILED\s+\S*test|AssertionError|assert\s+\S+\s*[=!<>])",
        "test_failure",
    ),
    _IssueTypeRule("explicit_exception", r"\w+(?:Error|Exception|Warning)", None),
    _IssueTypeRule("composite_keyword", r"(?i)composite", "composite"),
    _IssueTypeRule("config_error", r"(?i)pyproject\.toml|\[tool\.", "config_error"),
    _IssueTypeRule(
        "logic_error",
        r"(?i)(wrong\s+(result|output|return|value)|"
        r"incorrect\s+(result|output|behavio)|"
        r"should\s+(return|be|have)|"
        r"expected\s+\S+\s+but|"
        r"unexpected\s+(result|behavio)|"
        r"\bbug\b.*\b(function|logic|code)\b)",
        "logic_error",
    ),
]


def _summarize_failure_targets(failure_logs: list[str]) -> str:
    if not failure_logs:
        return ""
    test_ids: list[str] = []
    files: list[str] = []
    assertions: list[str] = []

    def add_unique(items: list[str], value: str) -> None:
        value = value.strip()
        if value and value not in items:
            items.append(value)

    for raw in failure_logs[:5]:
        line = str(raw)
        for match in re.finditer(r"\b(?:FAILED\s+)?([A-Za-z0-9_./\\-]+\.py::[^\s:]+)", line):
            add_unique(test_ids, match.group(1).replace("\\", "/"))
        for match in re.finditer(r'File\s+"([^"]+\.py)"', line):
            add_unique(files, Path(match.group(1)).name)
        for match in re.finditer(r"\b([A-Za-z0-9_./\\-]+\.py)\b", line):
            add_unique(files, Path(match.group(1)).name)
        if any(key in line for key in ("assert", "AssertionError", "ValueError", "TypeError")):
            add_unique(assertions, line[:160])

    parts: list[str] = []
    if test_ids:
        parts.append("失败用例: " + ", ".join(test_ids[:3]))
    if files:
        parts.append("相关文件: " + ", ".join(files[:5]))
    if assertions:
        parts.append("关键断言/异常: " + " | ".join(assertions[:2]))
    return "\n".join(parts)


def _repair_constraint_hints(failure_logs: list[str]) -> str:
    combined = "\n".join(str(log) for log in failure_logs[:5])
    hints: list[str] = []
    if re.search(r"invalid literal for int\(\).*['\"]N/A['\"]", combined, re.IGNORECASE):
        hints.append(
            "检测到 int() 转换非数字字符串 N/A。不要只写 dict.get/data.get(..., '0') 或 `or 0`，"
            "因为 'N/A' 是 truthy；必须显式处理非数字字符串，例如先取 raw，"
            "raw.isdigit() 时 int(raw)，否则按测试期望返回 0。"
        )
    if re.search(r"unsupported operand type\(s\).*['\"]int['\"].*['\"]str['\"]", combined):
        hints.append("检测到 int 与 str 拼接；字符串输出场景应在拼接前对数值结果使用 str(...)。")
    return "\n".join(hints)


def _patch_retry_fingerprint(patch: CandidatePatch) -> str:
    body = "\n".join(
        [
            patch.file_path or "",
            patch.diff or "",
            patch.original_lines or "",
            patch.patched_lines or "",
        ]
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _record_patch_retry_fingerprints(state: RepairState) -> str:
    raw_counts = state.node_timings.setdefault("patch_retry_fingerprints", {})
    if not isinstance(raw_counts, dict):
        raw_counts = {}
        state.node_timings["patch_retry_fingerprints"] = raw_counts

    repeated_files: list[str] = []
    for patch in state.candidate_patches:
        fp = _patch_retry_fingerprint(patch)
        count = int(raw_counts.get(fp, 0) or 0) + 1
        raw_counts[fp] = count
        if count >= 2:
            repeated_files.append(patch.file_path)

    if not repeated_files:
        return ""
    files = ", ".join(sorted(set(repeated_files))[:5])
    return (
        f"检测到重复补丁文件: {files}。\n"
        "不要重复生成上轮相同 diff；请基于失败测试换策略，"
        "优先修改仍失败断言直接相关的表达式或边界处理。"
    )


class Orchestrator(RepairPipelineMixin):
    """纯 Python 修复编排器。

    不调 LLM，只做调度和状态管理。
    """

    def __init__(
        self,
        patcher,
        verifier=None,
        *,
        use_pytest_verify: bool = False,
        require_sandbox: bool = False,
        sandbox_policy: str = "",
        allow_static_verify_fallback: bool = False,
        l1_prompt_cache_key: str = "",
    ):
        self.patcher = patcher
        self.verifier = verifier
        self.use_pytest_verify = use_pytest_verify
        configured_policy = str(sandbox_policy or os.environ.get("FIXLOOP_SANDBOX_POLICY", "preferred")).strip().lower()
        if configured_policy not in {"required", "preferred", "disabled"}:
            configured_policy = "preferred"
        self.sandbox_policy = configured_policy
        self.require_sandbox = bool(require_sandbox or configured_policy == "required")
        self.allow_static_verify_fallback = allow_static_verify_fallback
        self.l1_prompt_cache_key = l1_prompt_cache_key or self._resolve_l1_prompt_cache_key()
        self._repair_ctx: RepairRunContext | None = None
        self._log_run_id_token = None
        # 修复目标目录：优先 --repo / Agent cwd，而非 git 顶层仓库
        self._repo_root = str(Path.cwd())
        for agent in (patcher, verifier):
            if agent is None:
                continue
            workspace = getattr(agent, "workspace", None)
            candidate = (
                getattr(agent, "_cwd", "")
                or getattr(workspace, "cwd", "")
                or getattr(workspace, "repo_root", "")
            )
            if candidate:
                self._repo_root = candidate
                break
        self._repair_gateways = self._collect_repair_gateways(
            patcher,
            verifier,
        )

    @staticmethod
    def _collect_repair_gateways(*agents) -> tuple:
        gateways = []
        seen: set[int] = set()
        for agent in agents:
            gw = getattr(agent, "_repair_gateway", None)
            if gw is None:
                continue
            marker = id(gw)
            if marker in seen:
                continue
            seen.add(marker)
            gateways.append(gw)
        return tuple(gateways)

    @staticmethod
    def _resolve_l1_prompt_cache_key_from_agents(*agents) -> str:
        hashes = []
        for agent in agents:
            if agent is None:
                continue
            prefix = getattr(agent, "_prefix", None)
            if prefix is not None and getattr(prefix, "hash", ""):
                hashes.append(prefix.hash)
        if hashes and len(set(hashes)) == 1:
            return hashes[0]
        return ""

    def _resolve_l1_prompt_cache_key(self) -> str:
        return self._resolve_l1_prompt_cache_key_from_agents(
            self.patcher,
            self.verifier,
        )

    def repair(
        self,
        issue: str,
        max_retries: int = 3,
        repair_timeout_s: int = DEFAULT_REPAIR_TIMEOUT_S,
        phase_timeouts: PhaseTimeoutConfig | None = None,
        cancel_token=None,
        resume_run_id: str = "",
        verify_test_patch: str = "",
    ) -> RepairState:
        """执行修复流水线。

        Args:
            issue: Issue 描述（含堆栈和错误信息）。
            max_retries: 最大重试次数。
            repair_timeout_s: 全流程超时秒数（≤0 表示不限制）。
            phase_timeouts: 分阶段超时；默认由 ``repair_timeout_s`` 推导。
            cancel_token: 可选协作式取消 token（CLI Ctrl+C 注入）。
            resume_run_id: L2 续跑 run_id（从 repair_checkpoint.json 恢复，
                跳过 parse/localize，直接进入 patch 循环）。
            verify_test_patch: 可选；SWE 等官方 test_patch，仅在 verify 时临时应用。

        Returns:
            RepairState 实例。
        """
        from agent_runtime.cancellation import CancellationToken

        if phase_timeouts is None:
            phase_timeouts = PhaseTimeoutConfig.for_patcher_primary(repair_timeout_s)

        state = RepairState(
            issue_input=issue,
            max_retries=max_retries,
            repair_run_id=resume_run_id or "",
            active_roles=[
                role
                for role, agent in (("patcher", self.patcher), ("verifier", self.verifier))
                if agent is not None
            ],
            field_owners={
                "phase": "runtime",
                "status": "runtime",
                "changed_files": "patch_runtime",
                "verification_result": "verifier",
                "tool_budget": "runtime",
            },
        )
        state.intent = {"mode": "repair", "verification_required": True}
        state.role_lifecycle = {
            role: {"status": "active", "reason": "initial repair roles"}
            for role in state.active_roles
        }
        token = cancel_token or CancellationToken()
        initial_snapshot = self._snapshot_repo()

        self._repair_ctx = RepairRunContext(
            phase_timeout_config=phase_timeouts,
            cancel_token=token,
            verify_test_patch=verify_test_patch or "",
        )
        self._set_collaboration_context(state)
        try:
            with self._repair_cancel_scope(token):
                if repair_timeout_s <= 0:
                    return self._repair_impl(state, initial_snapshot=initial_snapshot)

                # Phase C：勿用 with ThreadPoolExecutor（退出时 wait=True 会拖尾）
                pool = ThreadPoolExecutor(max_workers=1)
                wall_started = time.monotonic()
                fut = pool.submit(self._repair_impl, state, initial_snapshot)
                try:
                    return fut.result(timeout=repair_timeout_s)
                except FuturesTimeoutError:
                    from src.repair.verification.repair_timeout import handle_repair_wall_timeout

                    handle_repair_wall_timeout(
                        self,
                        state,
                        initial_snapshot=initial_snapshot,
                        repair_timeout_s=repair_timeout_s,
                        cancel_token=token,
                        wall_started=wall_started,
                    )
                    return state
                finally:
                    pool.shutdown(wait=False, cancel_futures=True)
        finally:
            self._repair_ctx = None

    def _set_collaboration_context(self, state: RepairState) -> None:
        """Pass current mode/phase to gateways when the runtime owns the state."""
        # Localizer/Retriever are legacy compatibility agents.  Governance is
        # intentionally enforced only on the current repair roles.
        for agent in (self.patcher, self.verifier):
            gateway = getattr(agent, "_tool_dispatch", None)
            if gateway is not None and hasattr(gateway, "set_context"):
                gateway.set_context(
                    getattr(agent, "_agent_name", "") or "agent",
                    mode=str(state.intent.get("mode", "repair")),
                    phase=str(state.phase),
                    evidence=bool(state.evidence or state.suspect_locations),
                    read_before_write=bool(
                        state.node_timings.get("allowed_edit")
                        or state.suspect_locations
                    ),
                )

    def _on_collaboration_phase(self, state: RepairState, phase: str, reason: str = "") -> None:
        """Synchronize policy context and role set at a runtime phase boundary."""
        from src.collaboration_governance import CollaborationGovernance, RoleLifecycle

        transition = state.transition_phase(
            phase, reason, allow_recovery=phase == "recovery"
        )
        if not transition.valid:
            state.node_timings.setdefault("phase_transition_rejections", []).append(
                transition.to_dict()
            )
            return
        if phase == "patch":
            for role in ("verifier",):
                if role in state.active_roles:
                    event = CollaborationGovernance.lifecycle_transition(
                        state, role, RoleLifecycle.SUSPENDED, reason or "patch in progress"
                    )
                    self._emit_collaboration_event(event)
            role = "patcher"
            if getattr(self, role, None) is not None:
                event = CollaborationGovernance.lifecycle_transition(
                    state, role, RoleLifecycle.REACTIVATED, reason or "patch phase"
                )
                self._emit_collaboration_event(event)
        elif phase == "verify":
            if getattr(self, "patcher", None) is not None and "patcher" in state.active_roles:
                event = CollaborationGovernance.lifecycle_transition(
                    state, "patcher", RoleLifecycle.SUSPENDED, reason or "verification phase"
                )
                self._emit_collaboration_event(event)
            if getattr(self, "verifier", None) is not None:
                event = CollaborationGovernance.lifecycle_transition(
                    state, "verifier", RoleLifecycle.REACTIVATED, reason or "verification phase"
                )
                self._emit_collaboration_event(event)
        elif phase in {"done", "failed"}:
            for role in list(state.role_lifecycle):
                event = CollaborationGovernance.lifecycle_transition(
                    state, role, RoleLifecycle.COMPLETED, reason or "repair finalized"
                )
                self._emit_collaboration_event(event)
        self._set_collaboration_context(state)

    def _emit_collaboration_event(self, event: dict) -> None:
        ctx = getattr(self, "_repair_ctx", None)
        tracer = getattr(ctx, "repair_tracer", None) if ctx else None
        if tracer is not None:
            tracer.emit("orchestrator", "agent_set_changed", event)

    @contextmanager
    def _repair_cancel_scope(self, token):
        """绑定 cancel token 到子 Agent，退出时解绑。"""
        ctx = self._repair_ctx
        if ctx is not None:
            ctx.cancel_token = token
        self._bind_cancel_token(token)
        try:
            yield
        finally:
            self._unbind_cancel_token()

    def _abort_repair_if_cancelled(self, state: RepairState) -> bool:
        """若用户已 cancel 则返回 True（由 pipeline finally 负责 restore）。"""
        return self._is_repair_cancelled()

    def _bind_cancel_token(self, token) -> None:
        for agent in (self.patcher, self.verifier):
            if agent is not None:
                agent.cancel_token = token

    def _unbind_cancel_token(self) -> None:
        for agent in (self.patcher, self.verifier):
            if agent is not None:
                agent.cancel_token = None

    def _is_repair_cancelled(self) -> bool:
        ctx = self._repair_ctx
        token = ctx.cancel_token if ctx is not None else None
        return token is not None and token.is_cancelled

    def _emit_repair_cancelled(self, state: RepairState) -> None:
        ctx = self._repair_ctx
        self._maybe_leave_worktree(cancelled=True)
        tracer = ctx.repair_tracer if ctx is not None else None
        if tracer is None:
            return
        tracer.close_dangling_ask_spans()
        tracer.emit(
            "orchestrator",
            "repair_cancelled",
            {
                "status": state.status,
                "repo_restored": True,
            },
            status="cancelled",
        )
        tracer.end_root_span()

    def _parse_issue(self, issue: str) -> RepairPlan:
        """经 IntentRouter（repair 通道）折叠后构建 RepairPlan。

        分类优先级（_ISSUE_TYPE_RULES 规则链，由 IssueIntentAdapter 调用）：
        1. explicit_exception → classify_exception 归一化
        2. test_failure → pytest 断言失败
        3. composite_keyword → 多错误组合
        4. config_error → pyproject.toml 配置
        5. logic_error → 无异常名的错误行为描述
        6. unknown → LLM fallback
        """
        from agent_runtime.intent.adapters import IssueIntentAdapter
        from agent_runtime.intent.models import RouteContext
        from agent_runtime.intent.router import IntentRouter

        tracer = None
        repair_ctx = getattr(self, "_repair_ctx", None)
        if repair_ctx is not None:
            tracer = getattr(repair_ctx, "repair_tracer", None)

        def _emit(name: str, payload: dict) -> None:
            if tracer is not None:
                tracer.emit("orchestrator", name, payload)

        result = IntentRouter().route(
            issue,
            RouteContext(
                channel="repair",
                light_client=getattr(self, "_light_client", None),
                emit=_emit,
                cancel_token=getattr(repair_ctx, "cancel_token", None),
            ),
        )
        self._last_intent_result = result

        adapter = IssueIntentAdapter(
            classify_issue_type=self._classify_issue_type,
            parse_file_line=self._parse_file_line,
            detect_language=detect_repair_language,
            llm_classify=self._llm_classify_issue,
            repo_root=self._repo_root,
        )
        return adapter.to_repair_plan(result, issue)

    @staticmethod
    def _classify_issue_type(issue: str) -> tuple[str, str]:
        """规则链分类：依次匹配 _ISSUE_TYPE_RULES，首次命中返回 (issue_type, rule_name)。

        explicit_exception 命中时调用 classify_exception 归一化异常类名。
        """
        for rule in _ISSUE_TYPE_RULES:
            m = re.search(rule.pattern, issue)
            if not m:
                continue
            if rule.name == "explicit_exception":
                return classify_exception(m.group(0)), rule.name
            return rule.issue_type, rule.name
        return "unknown", "none"

    def _llm_classify_issue(self, issue: str) -> str | None:
        """用 light_client 将歧义 issue 分类为已知 issue_type。"""
        light = getattr(self, "_light_client", None)
        if light is None:
            return None
        types = ", ".join(ROUTED_ISSUE_TYPES)
        prompt = (
            f"Classify this software issue into exactly one category: {types}.\n"
            f"Reply with ONLY the category name.\n\n{issue[:800]}"
        )
        try:
            raw = light.complete(prompt, max_new_tokens=32)
            for word in raw.strip().split():
                word = word.strip(".,;:\"'").lower()
                if word in ROUTED_ISSUE_TYPES:
                    return word
        except Exception:
            pass
        return None

    def _parse_file_line(self, issue: str, file_path: str) -> int:
        """从 issue 文本提取行号（支持 file.py:42 或 line 42）。"""
        if file_path:
            m = re.search(rf"{re.escape(file_path)}:(\d+)", issue)
            if m:
                return int(m.group(1))
        m = re.search(r"line (\d+)", issue, re.IGNORECASE)
        if m:
            return int(m.group(1))
        return 0

    def _fallback_suspects_from_plan(
        self,
        plan: RepairPlan,
        issue: str,
    ) -> list[SuspectLocation]:
        """Localizer 无输出时，从 RepairPlan 生成粗粒度嫌疑位置。"""
        if not plan.suspect_files:
            return []
        suspects: list[SuspectLocation] = []
        for file_path in plan.suspect_files:
            line = self._parse_file_line(issue, file_path) or 1
            reason = "RepairPlan 降级定位"
            if fallback_suspect_uses_import_line(plan.issue_type):
                import_line = self._find_import_line_number(file_path)
                if import_line:
                    line = import_line
                reason = "import 语句"
            suspects.append(
                SuspectLocation(
                    file_path=file_path,
                    start_line=line,
                    end_line=line,
                    reason=reason,
                    confidence=0.7,
                )
            )
        return suspects

    def _find_import_line_number(self, file_path: str) -> int | None:
        """定位文件中第一条 import/from 语句行号。"""
        path = Path(self._repo_root) / file_path
        if not path.is_file():
            return None
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("from ", "import ")):
                return i
        return None

    def _patch_applier(self) -> PatchApplier:
        return PatchApplier(self._repo_root)

    def _resolve_repo_file(self, file_path: str) -> Path | None:
        return self._patch_applier().resolve_repo_file(file_path)

    def _match_skill(self, issue: str, *, language: str = "python"):
        """从 YAML Skill 文件中匹配 Issue 对应的修复策略。"""
        from src.skills.matcher import match_skill_semantic

        return match_skill_semantic(issue, language=language)

    def _classify_error(self, exc_type: str) -> str:
        """兼容旧调用；新代码请用 ``classify_exception``。"""
        return classify_exception(exc_type)

    def _verification_enabled(self) -> bool:
        return (
            self.verifier is not None or self.use_pytest_verify or self.allow_static_verify_fallback
        )

    def _snapshot_repo(self) -> dict[str, str]:
        return snapshot_repo(self._repo_root)

    def _restore_repo_snapshot(self, snapshot: dict[str, str]) -> None:
        restore_repo_snapshot(self._repo_root, snapshot)

    @staticmethod
    def _patcher_system_prompt(plan: RepairPlan | None) -> str:
        """Patcher system 文本（base + issue suffix）。

        工具环使用 Agent 构造时的 system prompt。
        """
        return load_role_prompt("patcher", patcher_variant_for(plan))

    @staticmethod
    def _inject_repair_task_summary(agent, state: RepairState) -> None:
        """将 _parse_issue 的结构化摘要注入 Agent working memory。

        供 L1 memory 检索（§4.4）使用。
        """
        plan = state.repair_plan
        if plan is None:
            return
        from agent_runtime.features.memory import set_task_summary

        parts = [f"[{plan.issue_type}]"]
        if plan.reasoning:
            parts.append(plan.reasoning[:200])
        if plan.suspect_files:
            parts.append(f"files: {', '.join(plan.suspect_files[:3])}")
        summary = " ".join(parts)
        set_task_summary(agent.session["memory"], summary)

    def _run_agent(
        self,
        agent,
        prompt: str,
        agent_name: str,
        state: RepairState | None = None,
        *,
        l2_phase: str = "",
        l2_attempt: int = 0,
    ) -> tuple[str, dict]:
        """执行 Agent 调用（Verifier 用，保留 Agent loop）。"""
        from agent_runtime.log_context import log_context

        # 注入 repair 任务上下文到 Agent working memory
        if agent is not None and state is not None and state.repair_plan is not None:
            self._inject_repair_task_summary(agent, state)

        t0 = time.time()
        run_id = getattr(agent, "shared_run_id", None)
        task_id = ""
        if state is not None and l2_phase:
            task_id = self._begin_l2_agent_ask(
                state,
                agent,
                agent_name=agent_name,
                phase=l2_phase,
                attempt=l2_attempt,
            )
        try:
            with log_context(run_id=run_id, agent=agent_name):
                answer = agent.ask(prompt, skip_plan=True)
        except Exception as e:
            if state is not None:
                state.agent_errors[agent_name] = str(e)
            log.warning("[%s] Agent 失败: %s", agent_name, e)
            elapsed_ms = int((time.time() - t0) * 1000)
            if state is not None and task_id:
                self._finish_l2_agent_ask(
                    state,
                    agent,
                    agent_name=agent_name,
                    phase=l2_phase,
                    attempt=l2_attempt,
                    task_id=task_id,
                    elapsed_ms=elapsed_ms,
                    stop_reason="error",
                )
            return "", {"total_ms": elapsed_ms, "internal": {}}
        elapsed_ms = int((time.time() - t0) * 1000)
        internal = getattr(agent, "_last_run_node_timings", None) or {}
        last_ts = getattr(agent, "_last_task_state", None)
        if state is not None and task_id:
            self._finish_l2_agent_ask(
                state,
                agent,
                agent_name=agent_name,
                phase=l2_phase,
                attempt=l2_attempt,
                task_id=task_id,
                elapsed_ms=elapsed_ms,
                stop_reason=getattr(last_ts, "stop_reason", "") if last_ts else "",
                tool_steps=getattr(last_ts, "tool_steps", 0) if last_ts else 0,
            )
        return answer, {"total_ms": elapsed_ms, "internal": dict(internal)}

    def _begin_repair_trace(self, state: RepairState) -> None:
        from agent_runtime.log_context import bind_run_id
        from agent_runtime.tokenizers import resolve_tokenizer_spec
        from src.repair.run_trace import RepairRunTracer

        tracer = RepairRunTracer(self._repo_root)
        l1_meta = {}
        if self.l1_prompt_cache_key:
            l1_meta["l1_prompt_cache_key"] = self.l1_prompt_cache_key
            ref = self.patcher or self.verifier
            prefix = getattr(ref, "_prefix", None) if ref is not None else None
            if prefix is not None:
                if prefix.tool_signature:
                    l1_meta["tool_signature"] = prefix.tool_signature
                if prefix.workspace_fingerprint:
                    l1_meta["workspace_fingerprint"] = prefix.workspace_fingerprint
        phase_cfg = self._repair_ctx.phase_timeout_config if self._repair_ctx else None
        if phase_cfg is not None and phase_cfg.any_enabled():
            l1_meta["phase_timeout_budgets"] = phase_cfg.budget_dict()
        run_id = tracer.begin(state.issue_input, **l1_meta)
        tracer.bind_agents(
            self.patcher,
            self.verifier,
        )
        if self._repair_ctx is not None:
            self._repair_ctx.repair_tracer = tracer
        state.repair_run_id = run_id
        if self._repair_ctx is not None:
            self._repair_ctx.log_run_id_token = bind_run_id(run_id)

        tokenizer_by_agent: dict[str, dict] = {}
        for name, agent in (("patcher", self.patcher), ("verifier", self.verifier)):
            if agent is None:
                continue
            config = getattr(agent, "config", None)
            spec = resolve_tokenizer_spec(
                getattr(config, "model", ""),
                getattr(config, "provider", ""),
            )
            tokenizer_by_agent[name] = {
                "rule_id": spec.rule_id,
                "tokenizer_fallback": spec.fallback,
                "tokenizer_id": spec.tokenizer_id,
            }
        if tokenizer_by_agent:
            state.node_timings["tokenizer_by_agent"] = tokenizer_by_agent

        self._maybe_enter_worktree(state, tracer)
        if self._repair_ctx is not None and self._repair_ctx.worktree_handle is not None:
            self._repair_ctx.worktree_initial_snapshot = self._snapshot_repo()

    def _maybe_enter_worktree(self, state: RepairState, tracer) -> None:
        """可选：FIXLOOP_USE_WORKTREE=1 时创建独立 worktree 并重定向 Agent 工具根。"""
        from agent_runtime.worktree import WorktreeError, create_worktree, worktree_enabled

        if not worktree_enabled():
            return
        ctx = self._repair_ctx
        if ctx is None:
            return
        run_id = state.repair_run_id or "run"
        try:
            handle = create_worktree(self._repo_root, run_id)
        except WorktreeError as e:
            if tracer is not None:
                tracer.emit(
                    "orchestrator",
                    "sandbox_violation",
                    {"reason": "worktree_create_failed", "error": str(e)},
                    status="error",
                )
            return

        originals: dict[str, str] = {}
        ctx.original_repo_root = self._repo_root
        for name, agent in (
            ("patcher", self.patcher),
            ("verifier", self.verifier),
        ):
            if agent is None:
                continue
            tc = getattr(agent, "tool_context", None)
            if tc is not None:
                originals[name] = str(tc.root)
                tc.root = str(handle.path)
            if hasattr(agent, "_cwd"):
                agent._cwd = str(handle.path)
        ctx.worktree_handle = handle
        ctx.worktree_original_roots = originals
        self._repo_root = str(handle.path)
        if tracer is not None:
            tracer.emit(
                "orchestrator",
                "worktree_created",
                handle.as_dict(),
                status="ok",
            )

    def _maybe_leave_worktree(self, *, cancelled: bool = False) -> None:
        """回收 worktree 并恢复 Agent 工具根。"""
        from agent_runtime.worktree import remove_worktree

        ctx = self._repair_ctx
        if ctx is None:
            return
        handle = getattr(ctx, "worktree_handle", None)
        originals = getattr(ctx, "worktree_original_roots", None) or {}
        for name, agent in (
            ("patcher", self.patcher),
            ("verifier", self.verifier),
        ):
            if agent is None:
                continue
            root = originals.get(name)
            if root is None:
                continue
            tc = getattr(agent, "tool_context", None)
            if tc is not None:
                tc.root = root
            if hasattr(agent, "_cwd"):
                agent._cwd = root
        tracer = ctx.repair_tracer
        if handle is not None:
            remove_worktree(handle.repo_root, handle.path, force=True)
            if tracer is not None:
                tracer.emit(
                    "orchestrator",
                    "worktree_removed",
                    {**handle.as_dict(), "cancelled": cancelled},
                    status="cancelled" if cancelled else "ok",
                )
        ctx.worktree_handle = None
        ctx.worktree_original_roots = {}
        if ctx.original_repo_root:
            self._repo_root = ctx.original_repo_root
            ctx.original_repo_root = ""

    def _end_repair_trace(self, state: RepairState) -> None:
        from agent_runtime.log_context import reset_run_id

        self._maybe_leave_worktree(cancelled=False)
        ctx = self._repair_ctx
        tracer = ctx.repair_tracer if ctx is not None else None
        if tracer is None:
            return
        token_summary = state.node_timings.get("token_usage") or {}
        tracer.finalize(state, token_summary)
        tracer.unbind_agents(
            self.patcher,
            self.verifier,
        )
        if ctx is not None:
            ctx.repair_tracer = None
        token = ctx.log_run_id_token if ctx is not None else None
        if token is not None:
            reset_run_id(token)
            if ctx is not None:
                ctx.log_run_id_token = None

    def _reset_token_tracking(self) -> None:
        from src.eval.token_usage import reset_clients_session_usage

        reset_clients_session_usage(self.patcher, self.verifier)

    def _attach_token_usage(self, state: RepairState) -> None:
        from src.eval.token_usage import build_repair_token_usage, resolve_model_clients

        clients = resolve_model_clients(self.patcher, self.verifier)
        if not clients:
            return
        summary = build_repair_token_usage(
            clients,
            Path(self._repo_root),
            since_ts=self._repair_ctx.repair_started_at if self._repair_ctx else None,
            repair_run_id=state.repair_run_id or None,
        )
        state.node_timings["total_tokens"] = summary["total_tokens"]
        state.node_timings["token_usage"] = summary
        if summary.get("token_usage_by_agent"):
            state.node_timings["token_usage_by_agent"] = summary["token_usage_by_agent"]
        if summary.get("tool_usage_by_agent"):
            state.node_timings["tool_usage_by_agent"] = summary["tool_usage_by_agent"]
        if summary.get("total_tool_steps") is not None:
            state.node_timings["total_tool_steps"] = summary["total_tool_steps"]
        # 分 Agent latency
        latency_by_agent: dict[str, dict] = {}
        for agent in (self.patcher, self.verifier):
            if agent is None:
                continue
            client = getattr(agent, "model_client", None)
            name = getattr(agent, "_agent_name", "") or "agent"
            if client and hasattr(client, "latency_stats"):
                latency_by_agent[name] = client.latency_stats()
        if latency_by_agent:
            state.node_timings["latency_by_agent"] = latency_by_agent

    def _attach_rejection_stats(self, state: RepairState) -> None:
        from src.repair.rejection_aggregate import (
            apply_gateway_denials_to_agent_errors,
            summarize_repair_rejections,
        )

        run_id = state.repair_run_id or ""
        if not run_id:
            return
        run_dir = Path(self._repo_root) / ".agent" / "runs" / run_id
        summary = summarize_repair_rejections(run_dir)
        for key, value in summary.items():
            state.node_timings[key] = value
        apply_gateway_denials_to_agent_errors(
            state.agent_errors,
            summary.get("permission_denied_by_agent"),
        )

    def _run_patcher(self, state: RepairState) -> tuple[list[CandidatePatch], dict]:
        """Run the sole patch path: governed tools mutate disk, Runtime snapshots the diff."""
        import os

        plan = state.repair_plan
        self._merge_blackboard_for_patch(state)
        feedback = state.feedback or ""
        # 失败面：注入断言 + 失败测试原文，驱动定点读改
        from src.repair.execution.control import pop_patcher_depth, push_patcher_depth
        from src.repair.execution.patcher_contract import (
            classify_patcher_attempt,
            record_patcher_terminal_status,
            render_patcher_runtime_contract,
        )
        from src.repair.verification.fail_surface import (
            build_fail_surface,
            build_fail_surface_prompt_block,
            build_verify_feedback_payload,
            render_verify_feedback_block,
        )

        surface = build_fail_surface(state, repo_root=self._repo_root)
        bucket = str(state.node_timings.get("verify_bucket") or "")
        fail_block = build_fail_surface_prompt_block(surface, bucket=bucket)
        if fail_block:
            state.node_timings["fail_surface_target"] = surface.verify_target
            state.node_timings["fail_surface_nodeids"] = list(surface.nodeids)
            feedback = f"{fail_block}\n\n{feedback}".strip() if feedback else fail_block

        structured_feedback = state.node_timings.get("structured_verify_feedback")
        if not isinstance(structured_feedback, dict) and state.verification_result is not None:
            payload = build_verify_feedback_payload(
                state,
                repo_root=self._repo_root,
                result=state.verification_result,
            )
            state.node_timings["structured_verify_feedback"] = payload.to_dict()
            structured_feedback = payload.to_dict()
        if isinstance(structured_feedback, dict):
            feedback_block = render_verify_feedback_block(structured_feedback)
            if feedback_block and feedback_block not in feedback:
                feedback = f"{feedback_block}\n\n{feedback}".strip() if feedback else feedback_block

        allowed = list(state.node_timings.get("allowed_edit") or [])
        runtime_bits = [
            "【repair runtime】read → apply_patch"
            "（*** Begin/End Patch，Update 须含 - 上下文）→ quick_test。",
            "可 expand_lock；不要用 run_shell/sandbox_test（无权限）。真源=磁盘 diff。",
        ]
        if allowed:
            runtime_bits.append("allowed_edit: " + ", ".join(allowed[:12]))
        block = "\n".join(runtime_bits)
        feedback = f"{block}\n\n{feedback}".strip() if feedback else block

        suspects_for_prompt = state.suspect_locations
        raw_steps = (os.environ.get("FIXLOOP_PATCHER_MAX_STEPS") or "24").strip()
        try:
            patch_steps = max(10, int(raw_steps))
        except ValueError:
            patch_steps = 24
        depth_token = push_patcher_depth(self.patcher, patch_steps)
        state.node_timings["patch_steps"] = patch_steps

        prompt, tpl_meta = self._patcher_prompt(
            suspects_for_prompt,
            state.retrieved_context,
            feedback,
            plan=plan,
            issue=state.issue_input,
            runtime_contract_block=render_patcher_runtime_contract(state),
        )

        try:
            applied, meta = self._run_patcher_toolized(state, prompt, tpl_meta)
            status = classify_patcher_attempt(
                state,
                applied,
                apply_failed=bool(state.agent_errors.get("patcher_apply")),
            )
            record_patcher_terminal_status(state, status, reason="patcher_turn_complete", meta=meta)
            return applied, meta
        finally:
            pop_patcher_depth(self.patcher, depth_token)

    def _compact_patcher_history(self, state) -> None:
        """CC 序 compact；thrash 时记停损标志。"""
        if self.patcher is None:
            return
        from src.repair.context_compact import compact_tool_history

        session = getattr(self.patcher, "session", None) or {}
        hist = list(session.get("history") or [])
        if not hist:
            return
        prior = int(state.node_timings.get("compact_count") or 0)
        result = compact_tool_history(hist, prior_compacts=prior)
        session["history"] = result.messages
        state.node_timings["compact_count"] = result.compact_count
        if result.dropped_chars:
            state.node_timings["compact_dropped_chars"] = (
                int(state.node_timings.get("compact_dropped_chars") or 0)
                + result.dropped_chars
            )
        if result.thrash:
            n = (
                int(state.node_timings.get("compact_thrash_count") or 0)
                + result.compact_thrash_count
            )
            state.node_timings["compact_thrash_count"] = n
            tip = "compact_thrash：上下文压缩后仍爆满，停止空转 compact，请缩小改动面。"
            state.feedback = f"{state.feedback}\n{tip}".strip() if state.feedback else tip
            log.warning("[compact] thrash stop-loss")

    def _run_patcher_toolized(
        self,
        state: RepairState,
        prompt: str,
        tpl_meta: dict,
    ) -> tuple[list[CandidatePatch], dict]:
        """Agent loop：工具改盘 → 快照 diff 成 CandidatePatch（不再二次 apply）。"""
        import os

        from src.repair.execution.edit_from_disk import patches_from_snapshot_diff
        from src.repair.failure_tags import check_patch_faithfulness

        t_start = time.time()
        compact_enabled = (
            (os.environ.get("FIXLOOP_PATCHER_COMPACT") or "1").strip().lower()
            not in (
                "0",
                "false",
                "off",
                "no",
            )
        )
        if compact_enabled:
            self._compact_patcher_history(state)
        before = self._snapshot_repo()
        answer, agent_timing = self._run_agent(
            self.patcher,
            prompt,
            "patcher",
            state,
            l2_phase="patch",
            l2_attempt=state.retry_count,
        )
        after = self._snapshot_repo()
        lock = getattr(self, "_edit_lock", None)
        if lock is not None:
            state.node_timings["apply_patch_ok_count"] = int(
                getattr(lock, "apply_patch_ok_count", 0) or 0
            )
            state.node_timings["edit_lint_reject_count"] = int(
                getattr(lock, "edit_lint_reject_count", 0) or 0
            )
            state.node_timings["unread_write_reject_count"] = int(
                getattr(lock, "unread_write_reject_count", 0) or 0
            )
            state.node_timings["apply_path_reject_count"] = int(
                getattr(lock, "apply_path_reject_count", 0) or 0
            )
            if hasattr(self, "_progress_emitter"):
                summary = (
                    f"apply_ok={lock.apply_patch_ok_count} "
                    f"lint_rej={lock.edit_lint_reject_count}"
                )
                self._progress_emitter().emit("tool_progress", summary=summary)
                if hasattr(self, "_emit_repair_span"):
                    self._emit_repair_span(
                        "apply_patch_span",
                        {
                            "summary": summary,
                            "apply_patch_ok_count": lock.apply_patch_ok_count,
                        },
                    )
        patches = patches_from_snapshot_diff(before, after)
        patches, rejected = check_patch_faithfulness(
            patches, state, soft_keep=False, repo_root=str(self._repo_root or "")
        )
        if rejected:
            state.agent_errors["patcher_hallucinated_file"] = rejected[0]
            for rel in rejected:
                if rel in before:
                    target = Path(self._repo_root) / rel
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(before[rel], encoding="utf-8")
                    except OSError as e:
                        log.warning("[patcher] restore rejected file %s: %s", rel, e)
            # 重新 diff 仅保留 allowed（含 soft 晋升）
            after = self._snapshot_repo()
            patches = patches_from_snapshot_diff(before, after)
            patches, _ = check_patch_faithfulness(
                patches, state, soft_keep=False, repo_root=str(self._repo_root or "")
            )

        total_ms = int((time.time() - t_start) * 1000)
        model_call_ms = int(agent_timing.get("total_ms") or total_ms)
        meta = {
            "model_call_ms": model_call_ms,
            "parse_apply_ms": max(0, total_ms - model_call_ms),
            "total_ms": total_ms,
            "edit_mode": "tools",
            "internal": agent_timing.get("internal") or {},
            "task_template_source": (tpl_meta or {}).get("task_template_source"),
        }
        if not patches:
            from src.repair.execution.loose_patch_recover import parse_patches_with_recover

            parsed = parse_patches_with_recover(answer or "")
            if parsed:
                recovered = any(
                    (p.explanation or "") == "loose_diff_recover" for p in parsed
                )
                if recovered:
                    state.node_timings["patcher_loose_recovered"] = True
                log.info(
                    "[patcher] toolized 无落盘，尝试解析 final 文本 (%d%s)",
                    len(parsed),
                    ", loose" if recovered else "",
                )
                applied = self._apply_patches_on_disk(parsed, state=state)
                if applied:
                    mode = "tools→loose" if recovered else "tools→json_final"
                    state.node_timings["patcher_edit_mode"] = mode
                    state.node_timings["patcher_text_salvage"] = True
                    meta["edit_mode"] = mode
                    return applied, meta
            state.node_timings["patcher_edit_mode"] = "tools_empty"
            return [], meta

        state.node_timings["patcher_edit_mode"] = "tools"
        self._last_apply_errors = []
        self._last_sibling_warnings = []
        log.info("[patcher] toolized 落盘 %d 个文件", len(patches))
        return patches, meta

    def _apply_patches_on_disk(
        self, patches: list[CandidatePatch], *, state=None
    ) -> list[CandidatePatch]:
        # faithfulness 闸口：过滤操作无关文件的幻觉 patch（soft_keep 防空导出）
        if state is not None:
            from src.repair.failure_tags import check_patch_faithfulness

            if not state.node_timings.get("_repo_root_hint"):
                state.node_timings["_repo_root_hint"] = str(self._repo_root or "")
            patches, rejected = check_patch_faithfulness(
                patches, state, soft_keep=True, repo_root=str(self._repo_root or "")
            )
            if rejected:
                state.agent_errors["patcher_hallucinated_file"] = rejected[0]
                import sys

                print(
                    f"  [patcher] ⚠ faithfulness gate: 拒绝无关文件 patch {rejected}",
                    file=sys.stderr,
                    flush=True,
                )
            if state.node_timings.get("faithfulness_soft") or state.node_timings.get(
                "faithfulness_promoted"
            ):
                print(
                    "  [patcher] faithfulness soft/promoted — kept existing impl files",
                    file=sys.stderr,
                    flush=True,
                )
        # Patcher-primary：allowed_edit + 未读不可写
        lock = getattr(self, "_edit_lock", None)
        if lock is not None and patches:
            kept: list[CandidatePatch] = []
            for p in patches:
                ok, reason = lock.check_write(p.file_path or "")
                if ok:
                    kept.append(p)
                else:
                    import sys

                    print(
                        f"  [edit_lock] reject {p.file_path}: {reason}",
                        file=sys.stderr,
                        flush=True,
                    )
                    if state is not None:
                        state.agent_errors["edit_lock"] = reason
                        state.node_timings["unread_write_reject_count"] = (
                            lock.unread_write_reject_count
                        )
                        state.node_timings["apply_path_reject_count"] = (
                            lock.apply_path_reject_count
                        )
            patches = kept
        applier = self._patch_applier()
        allowlist = None
        if lock is not None and getattr(lock, "allowed_edit", None):
            allowlist = set(lock.allowed_edit)
        applied = applier.apply_patches(patches, allowed_paths=allowlist)
        self._last_sibling_warnings = list(applier.last_sibling_warnings or [])
        self._last_apply_errors = list(getattr(applier, "last_apply_errors", None) or [])
        return applied

    def _run_verifier(self, state: RepairState) -> "VerificationResult":
        """Docker 沙箱或本地 pytest 验证（不走 LLM Agent loop）。"""
        from src.repair.verification.verify_test_patch import VerifyTestPatchOverlay

        cancel_token = self._repair_ctx.cancel_token if self._repair_ctx else None
        test_patch = ""
        if self._repair_ctx is not None:
            test_patch = getattr(self._repair_ctx, "verify_test_patch", "") or ""
        language = "python"
        if state.repair_plan is not None and state.repair_plan.language:
            language = state.repair_plan.language
        if language != "python":
            run = ProfileVerifyStrategy().run(
                self._repo_root,
                test_path=self._pick_test_path(state),
                cancel_token=cancel_token,
                language=language,
            )
            if run.error == "verification_environment_failed":
                run = StaticVerifyStrategy().run(
                    self._repo_root,
                    cancel_token=cancel_token,
                    language=language,
                )
            record_verify_timings(state, run)
            return run.result

        try:
            with VerifyTestPatchOverlay(self._repo_root, test_patch) as overlay:
                if overlay.applied:
                    state.node_timings["verify_test_patch_applied"] = True
                return self._run_verifier_python(state, cancel_token=cancel_token)
        except Exception as exc:
            log.warning("[verifier] test_patch overlay failed: %s", exc)
            return VerificationResult(
                all_passed=False,
                total_tests=0,
                failed=1,
                failure_logs=[f"verify_config: test_patch_apply_failed: {exc}"],
            )

    def _run_verifier_python(
        self, state: RepairState, *, cancel_token=None
    ) -> "VerificationResult":
        """Python 路径：Docker / host pytest / static（假定 test_patch 已在树上如需要）。"""
        try:
            from agent_runtime.metrics import get_registry

            get_registry().counter_inc(
                "fixloop_sandbox_policy_events_total",
                labels={"action": "evaluate", "reason": self.sandbox_policy},
            )
        except Exception:
            pass
        if self.verifier is not None:
            run = DockerVerifyStrategy().run(
                self._repo_root,
                test_path=self._pick_test_path(state),
                cancel_token=cancel_token,
            )
            if run.error:
                log.warning("[verifier] 沙箱验证失败: %s", run.error)
                if run.error == "sandbox_unavailable" and self.require_sandbox:
                    record_verify_timings(state, run)
                    return run.result
                if run.error == "sandbox_unavailable" and self.use_pytest_verify:
                    log.info("[verifier] 沙箱不可用，降级到宿主机 pytest（execution_tier=host）")
                    run = PytestVerifyStrategy().run(self._repo_root, cancel_token=cancel_token)
                    record_verify_timings(state, run)
                    return run.result
                if run.error == "sandbox_unavailable" and self.allow_static_verify_fallback:
                    log.info("[verifier] 沙箱不可用，降级到静态验证（execution_tier=static）")
                    language = "python"
                    if state.repair_plan is not None and state.repair_plan.language:
                        language = state.repair_plan.language
                    run = StaticVerifyStrategy().run(
                        self._repo_root,
                        cancel_token=cancel_token,
                        language=language,
                    )
                    record_verify_timings(state, run)
                    return run.result
            record_verify_timings(state, run, log_sandbox=True)
            return run.result
        if self.use_pytest_verify and self.require_sandbox:
            return VerificationResult(
                all_passed=False,
                failure_logs=["verify_config: sandbox_required_but_verifier_unavailable"],
            )
        if self.use_pytest_verify:
            run = PytestVerifyStrategy().run(self._repo_root, cancel_token=cancel_token)
            record_verify_timings(state, run)
            return run.result
        if self.allow_static_verify_fallback:
            language = "python"
            if state.repair_plan is not None and state.repair_plan.language:
                language = state.repair_plan.language
            run = StaticVerifyStrategy().run(
                self._repo_root,
                cancel_token=cancel_token,
                language=language,
            )
            record_verify_timings(state, run)
            return run.result
        return VerificationResult(all_passed=False, failure_logs=["verifier 未配置"])

    def _pick_test_path(self, state: RepairState) -> str:
        """从失败面 / Retriever / FAIL_TO_PASS / test_patch 提取 pytest 可收集 target。"""
        from src.benchmark.swebench.convert import (
            extract_fail_to_pass_hints,
            normalize_related_test_refs,
            resolve_test_ref_for_pytest,
        )
        from src.repair.verification.fail_surface import preferred_verify_targets
        from src.repair.verification.verify_test_patch import extract_targets_from_test_patch

        candidates: list[str] = []
        # 失败面优先：上一轮 FAILED nodeid
        candidates.extend(preferred_verify_targets(state))
        ctx = state.retrieved_context
        if ctx and ctx.related_tests:
            for item in ctx.related_tests:
                if isinstance(item, str) and item.strip():
                    candidates.append(item.strip())
                elif isinstance(item, dict):
                    for key in ("nodeid", "name", "path"):
                        value = item.get(key, "")
                        if value:
                            candidates.append(str(value).strip())
                            break
        try:
            candidates.extend(extract_fail_to_pass_hints(state.issue_input or ""))
        except Exception:
            pass
        test_patch = ""
        repair_ctx = getattr(self, "_repair_ctx", None)
        if repair_ctx is not None:
            test_patch = getattr(repair_ctx, "verify_test_patch", "") or ""
        try:
            candidates.extend(extract_targets_from_test_patch(test_patch))
        except Exception:
            pass

        # 去重保序
        deduped: list[str] = []
        seen: set[str] = set()
        for c in candidates:
            key = c.strip().replace("\\", "/")
            if key and key not in seen:
                seen.add(key)
                deduped.append(key)
        candidates = deduped

        repo_root = getattr(self, "_repo_root", "") or ""
        normalized = normalize_related_test_refs(candidates, repo_root or None)
        if repo_root:
            root = Path(repo_root)
            for ref in normalized:
                file_part = ref.split("::", 1)[0]
                if file_part and (root / file_part).is_file():
                    return ref
            # bare name：在仓内搜 def <name>
            for ref in list(candidates) + list(normalized):
                name = ref.strip()
                if "::" in name or "/" in name or "\\" in name or not name.isidentifier():
                    continue
                if not name.startswith("test_"):
                    continue
                hit = self._find_test_def(root, name)
                if hit:
                    return hit
        if normalized:
            return normalized[0]
        if candidates:
            return resolve_test_ref_for_pytest(candidates[0], repo_root or None)
        return ""

    def _find_test_def(self, root: Path, test_name: str) -> str:
        """裸 test 名 → 第一个匹配 ``def test_name`` 的 pytest nodeid。"""
        needle = f"def {test_name}"
        try:
            for path in root.rglob("test_*.py"):
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if needle not in text:
                    continue
                rel = str(path.relative_to(root)).replace("\\", "/")
                return f"{rel}::{test_name}"
        except OSError:
            return ""
        return ""

    def _revert_changes(self, state: RepairState):
        """回滚 Patcher 修改的文件（git checkout）。"""
        import subprocess

        files = set()
        for p in state.candidate_patches:
            files.add(p.file_path)
        for f in sorted(files):
            try:
                subprocess.run(
                    ["git", "checkout", "--", f],
                    cwd=self._repo_root,
                    capture_output=True,
                    timeout=10,
                )
            except Exception:
                pass

    def _build_feedback(
        self, result: "VerificationResult", *, state: RepairState | None = None
    ) -> str:
        """构建结构化反馈文本（V1.4-Bonus8a）。

        Sections（按优先级排列）:
        1. regression_hint — 上轮引入回归时的回滚提示
        2. previous_patches — 上轮失败的 patch diff
        3. failure_logs — 失败测试日志
        4. build_log — 构建日志
        5. guidance — 后续操作指导
        """
        sections: list[_FeedbackSection] = []

        # 1. regression hint
        if state is not None:
            from src.repair.verification.termination import introduced_regression

            if introduced_regression(state):
                sections.append(
                    _FeedbackSection(
                        "回滚提示",
                        "上轮补丁引入了回归（原本通过的测试现在失败）。"
                        "请先还原受影响的无关文件，只修改 suspect 范围内的文件。",
                        10,
                        200,
                    )
                )

        # E6a: apply 失败细节写入 feedback，供下一轮 patcher 使用
        if state is not None and state.agent_errors.get("patcher_apply"):
            sections.append(
                _FeedbackSection(
                    "Apply 失败",
                    str(state.agent_errors["patcher_apply"])[:500],
                    18,
                    500,
                )
            )
        if state is not None and state.agent_errors.get("incomplete_sibling_pattern"):
            sections.append(
                _FeedbackSection(
                    "Sibling 模式",
                    str(state.agent_errors["incomplete_sibling_pattern"])[:400],
                    19,
                    400,
                )
            )

        # 2. previous patches
        if state is not None and state.candidate_patches:
            patch_lines: list[str] = []
            for i, p in enumerate(state.candidate_patches[:2], 1):
                diff = p.diff[:_MAX_PATCH_DIFF_CHARS] if p.diff else "(空)"
                patch_lines.append(f"补丁{i} ({p.file_path}):\n{diff}")
            if patch_lines:
                sections.append(
                    _FeedbackSection(
                        "上轮改动",
                        "\n\n".join(patch_lines),
                        20,
                        _MAX_PATCH_DIFF_CHARS * 2,
                    )
                )

            repeat_feedback = _record_patch_retry_fingerprints(state)
            if repeat_feedback:
                sections.append(_FeedbackSection("重复补丁", repeat_feedback, 22, 500))

        from src.repair.verification.verify_diagnose import VerifyBucket, diagnose_verification

        diagnosis = diagnose_verification(result)
        if state is not None:
            state.node_timings["verify_bucket"] = diagnosis.bucket.value
            state.node_timings["verify_bucket_reason"] = diagnosis.reason
            if diagnosis.failed_nodeids:
                state.node_timings["verify_failed_nodeids"] = list(diagnosis.failed_nodeids)
            from src.repair.verification.failure_decision import (
                apply_failure_decision,
                decide_verification_failure,
                render_failure_decision,
            )

            failure_decision = decide_verification_failure(result, state=state)
            apply_failure_decision(state, failure_decision)
            ctx = getattr(self, "_repair_ctx", None)
            if ctx and ctx.repair_tracer:
                ctx.repair_tracer.emit(
                    "orchestrator",
                    "repair_failure_decision",
                    failure_decision.to_dict(),
                    status="error",
                )
            sections.append(
                _FeedbackSection(
                    "修复决策",
                    render_failure_decision(failure_decision),
                    11,
                    700,
                )
            )
            from src.repair.verification.fail_surface import (
                apply_verify_feedback_to_state,
                build_verify_feedback_payload,
                render_verify_feedback_block,
            )

            verify_payload = build_verify_feedback_payload(
                state,
                repo_root=getattr(self, "_repo_root", "") or "",
                result=result,
            )
            apply_verify_feedback_to_state(state, verify_payload)
            structured_block = render_verify_feedback_block(verify_payload)
            if structured_block:
                sections.append(
                    _FeedbackSection("结构化反馈", structured_block, 13, 1800)
                )

        sections.append(
            _FeedbackSection(
                "验证分桶",
                f"bucket={diagnosis.bucket.value}; reason={diagnosis.reason}",
                12,
                200,
            )
        )
        if diagnosis.failed_nodeids:
            sections.append(
                _FeedbackSection(
                    "失败用例",
                    "\n".join(f"  - {n}" for n in diagnosis.failed_nodeids[:5]),
                    23,
                    400,
                )
            )

        # 失败面：断言 + 测试原文摘要（供下一轮 toolized patcher）
        if state is not None:
            from src.repair.verification.fail_surface import (
                build_fail_surface,
                build_fail_surface_prompt_block,
            )

            surface = build_fail_surface(
                state,
                repo_root=getattr(self, "_repo_root", "") or "",
                result=result,
            )
            fail_block = build_fail_surface_prompt_block(
                surface,
                max_chars=2500,
                bucket=diagnosis.bucket.value,
            )
            if fail_block:
                sections.append(_FeedbackSection("失败面", fail_block, 27, 2500))

            from src.repair.failure_ledger import build_ledger_prompt_block, load_ledger_from_state

            ledger_block = build_ledger_prompt_block(load_ledger_from_state(state))
            if ledger_block:
                sections.append(_FeedbackSection("失败账本", ledger_block, 28, 2200))

        target_feedback = _summarize_failure_targets(result.failure_logs)
        if target_feedback:
            sections.append(
                _FeedbackSection(
                    "失败定位",
                    target_feedback,
                    24,
                    _MAX_FAILURE_TARGET_CHARS,
                )
            )

        constraint_inputs = list(result.failure_logs)
        if state is not None and state.issue_input:
            constraint_inputs.append(state.issue_input)
        constraint_hints = _repair_constraint_hints(constraint_inputs)
        if constraint_hints:
            sections.append(_FeedbackSection("修复约束", constraint_hints, 26, 700))

        # 3. failure logs
        if result.failure_logs:
            logs = "\n".join(
                f"  - {log[:_MAX_FAILURE_LOG_CHARS]}" for log in result.failure_logs[:5]
            )
            title = "失败测试"
            if diagnosis.bucket == VerifyBucket.ENV:
                title = "验证环境"
            elif diagnosis.bucket == VerifyBucket.COLLECT:
                title = "收集失败"
            elif "verify_config:" in "\n".join(str(x) for x in result.failure_logs):
                title = "验证配置"
            sections.append(_FeedbackSection(title, logs, 30, _MAX_FAILURE_LOG_CHARS * 5))

        # 4. build log
        if result.build_log:
            sections.append(
                _FeedbackSection(
                    "构建日志",
                    result.build_log[:_MAX_BUILD_LOG_CHARS],
                    25,
                    _MAX_BUILD_LOG_CHARS,
                )
            )

        # 按 priority 排序后组装
        sections.sort(key=lambda s: s.priority)
        lines = ["补丁验证失败。"]
        for sec in sections:
            text = sec.content[: sec.max_len] if sec.content else ""
            if text:
                lines.append(f"\n[{sec.title}]\n{text}")

        # 5. guidance（按分桶给出可执行下一步）
        guide_parts: list[str] = []
        if diagnosis.guidance:
            guide_parts.append(diagnosis.guidance)
        if diagnosis.bucket == VerifyBucket.LOGIC:
            guide_parts.append(
                "使用 read_file / patch_file 先读失败测试再改实现，"
                "最后输出 CandidatePatch JSON。"
            )
        elif diagnosis.bucket == VerifyBucket.ENV:
            guide_parts.append(
                "环境未就绪时停止堆叠同类业务补丁；"
                "若仍重试，只允许修正测试入口或导入路径相关改动。"
            )
        if state is not None and state.retry_count > 0:
            guide_parts.append(
                f"已尝试 {state.retry_count + 1} 次。"
                "不要重复上轮相同 diff；只改失败相关文件。"
            )
        elif not guide_parts:
            guide_parts.append(
                "请根据以上日志修改补丁。"
                "使用 patch_file 直接修改文件，然后输出 CandidatePatch JSON。"
            )
        lines.append("\n[指导]\n" + " ".join(guide_parts))
        return "\n".join(lines)

    # ---- Prompt 构建 ----

    def _emit_skill_hint_trace(self, agent: str, render) -> None:
        from src.skills.skill_block import skill_hint_rendered_trace

        tracer = self._repair_ctx.repair_tracer if self._repair_ctx else None
        if tracer is None or not render.text:
            return
        tracer.emit(agent, "skill_hint_rendered", skill_hint_rendered_trace(render))

    def _patcher_prompt(
        self,
        suspects: list[SuspectLocation],
        context: RetrievedContext | None,
        feedback: str = "",
        plan: RepairPlan | None = None,
        issue: str = "",
        runtime_contract_block: str = "",
    ) -> tuple[str, dict]:
        ctx = self._repair_ctx
        blackboard = ctx.blackboard if ctx is not None else None
        evidence_block = ""
        if self.patcher is not None:
            try:
                from agent_runtime.features.memory import render_evidence_ledger

                memory = self.patcher.session.get("memory", {}) if getattr(
                    self.patcher, "session", None
                ) else {}
                evidence_block = render_evidence_ledger(memory)
            except Exception:
                evidence_block = ""
        variables, render, subscribe_meta = assemble_patcher_variables(
            suspects=suspects,
            context=context,
            feedback=feedback,
            evidence_block=evidence_block,
            plan=plan,
            issue=issue,
            read_snippet=self._read_code_snippet,
            read_test_context=self._read_test_context,
            fallback_suspects=self._fallback_suspects_from_plan,
            blackboard=blackboard,
            diff_only=True,
            read_line_range=self._read_line_range,
            runtime_contract_block=runtime_contract_block,
        )
        tracer = ctx.repair_tracer if ctx is not None else None
        if subscribe_meta and tracer is not None:
            tracer.emit(
                "orchestrator",
                "blackboard_prefix_subscribed",
                subscribe_meta,
            )
        self._emit_skill_hint_trace("patcher", render)
        return render_repair_task("patcher", variables)

    def _read_code_snippet(self, file_path: str, start_line: int, end_line: int) -> str:
        """预读嫌疑文件上下文。"""
        path = Path(file_path)
        if not path.is_absolute():
            path = Path(self._repo_root) / path
        if not path.is_file():
            return ""
        lines = path.read_text(encoding="utf-8").split("\n")
        ctx_start = max(0, start_line - 3)
        ctx_end = min(len(lines), end_line + 3)
        block = ["    ```python"]
        for i in range(ctx_start, ctx_end):
            marker = ">>>" if start_line - 1 <= i < end_line else "   "
            block.append(f"    {marker} {lines[i]}")
        block.append("    ```")
        return "\n".join(block)

    def _read_line_range(self, file_path: str, start_line: int, end_line: int) -> str:
        """读取文件的原始行（无 markdown 包装），供 diff-only 上下文使用。"""
        path = Path(file_path)
        if not path.is_absolute():
            path = Path(self._repo_root) / path
        if not path.is_file():
            return ""
        lines = path.read_text(encoding="utf-8").split("\n")
        ctx_start = max(0, start_line - 1)
        ctx_end = min(len(lines), end_line)
        return "\n".join(lines[ctx_start:ctx_end])

    def _read_test_context(
        self,
        context: RetrievedContext | None,
        suspects: list[SuspectLocation],
        plan: RepairPlan | None = None,
    ) -> list[str]:
        """预读相关测试文件全文（同文件内所有用例一并提供）。"""
        test_paths: list[Path] = []

        if context and context.related_tests:
            for item in context.related_tests:
                path = self._resolve_test_path(str(item))
                if path and path not in test_paths:
                    test_paths.append(path)

        for s in suspects:
            guessed = self._guess_test_file(s.file_path, s.function_name or "")
            if guessed and guessed not in test_paths:
                test_paths.append(guessed)

        if not test_paths:
            test_paths = self._discover_repo_test_files()

        blocks: list[str] = []
        for path in test_paths:
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            rel = path.name
            blocks.append(f"  {rel}:")
            blocks.append("  ```python")
            blocks.append(content.rstrip())
            blocks.append("  ```")
        return blocks

    def _resolve_test_path(self, ref: str) -> Path | None:
        """从 pytest nodeid 或路径解析测试文件。"""
        file_part = ref.split("::", 1)[0].strip()
        if not file_part:
            return None
        candidates = [
            Path(self._repo_root) / file_part,
            Path(self._repo_root) / "tests" / file_part,
        ]
        for path in candidates:
            if path.is_file():
                return path
        return None

    def _guess_test_file(self, source_file: str, function_name: str) -> Path | None:
        """按约定猜测 test_<module>.py。"""
        stem = Path(source_file).stem
        names = [f"test_{stem}.py", f"{stem}_test.py", "test_app.py"]
        for name in names:
            for base in (Path(self._repo_root), Path(self._repo_root) / "tests"):
                path = base / name
                if not path.is_file():
                    continue
                if not function_name:
                    return path
                text = path.read_text(encoding="utf-8")
                if function_name in text:
                    return path
        return None

    def _discover_repo_test_files(self) -> list[Path]:
        """扫描 repo 根目录下的 test_*.py。"""
        root = Path(self._repo_root)
        found: list[Path] = []
        for pattern in ("test_*.py",):
            found.extend(root.glob(pattern))
            tests_dir = root / "tests"
            if tests_dir.is_dir():
                found.extend(tests_dir.glob(pattern))
        return sorted({p.resolve() for p in found})

    # ---- 解析 Agent 输出 ----

    def _verifier_prompt(self, patches: list[CandidatePatch], plan: RepairPlan | None) -> str:
        variables, render = build_verifier_variables(patches, self._repo_root, plan=plan)
        self._emit_skill_hint_trace("verifier", render)
        text, _ = render_repair_task("verifier", variables)
        return text

    def _parse_verification(self, answer: str) -> "VerificationResult":
        return parse_verification(answer)
