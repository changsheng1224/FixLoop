"""Orchestrator：纯 Python 编排器（不调 LLM）。

工作流：
1. _parse_issue() → 正则提取语言/异常类型/文件名 → RepairPlan
2. _match_skill() → 匹配 YAML Skill
3. _run_localize_and_retrieve() → Localizer + Retriever 并行
4. _run_patcher() → 串行执行
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path

import yaml

from agent_runtime.logging_setup import get_logger
from src.repair.output_parsers import (
    parse_retrieved_context,
    parse_suspect_list,
    parse_verification,
)
from src.repair.patch_applier import PatchApplier, apply_patch_to_text, parse_patches
from src.repair.pipeline import RepairPipelineMixin
from src.repair.repo_snapshot import restore_repo_snapshot, snapshot_repo
from src.repair.verify import DockerVerifyStrategy, PytestVerifyStrategy, record_verify_timings
from src.prompts.loader import load_role_prompt
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


class Orchestrator(RepairPipelineMixin):
    """纯 Python 修复编排器。

    不调 LLM，只做调度和状态管理。
    """

    def __init__(
        self,
        localizer,
        retriever,
        patcher,
        verifier=None,
        *,
        use_pytest_verify: bool = False,
    ):
        self.localizer = localizer
        self.retriever = retriever
        self.patcher = patcher
        self.verifier = verifier
        self.use_pytest_verify = use_pytest_verify
        self._repair_tracer = None
        self._log_run_id_token = None
        # 修复目标目录：优先 --repo / Agent cwd，而非 git 顶层仓库
        self._repo_root = str(Path.cwd())
        if localizer is not None:
            self._repo_root = (
                localizer._cwd
                or getattr(localizer.workspace, "cwd", "")
                or localizer.workspace.repo_root
                or self._repo_root
            )

    def repair(
        self,
        issue: str,
        max_retries: int = 3,
        repair_timeout_s: int = DEFAULT_REPAIR_TIMEOUT_S,
    ) -> RepairState:
        """执行修复流水线。

        Args:
            issue: Issue 描述（含堆栈和错误信息）。
            max_retries: 最大重试次数。
            repair_timeout_s: 全流程超时秒数（≤0 表示不限制）。

        Returns:
            RepairState 实例。
        """
        state = RepairState(issue_input=issue, max_retries=max_retries)
        if repair_timeout_s <= 0:
            return self._repair_impl(state)

        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(self._repair_impl, state)
            try:
                return fut.result(timeout=repair_timeout_s)
            except FuturesTimeoutError:
                state.status = "failed"
                state.agent_errors["orchestrator"] = f"repair timeout ({repair_timeout_s}s)"
                state.node_timings["repair_timeout"] = repair_timeout_s
                log.warning("修复超时 (%ds)", repair_timeout_s)
                return state

    def _parse_issue(self, issue: str) -> RepairPlan:
        """正则解析 Issue 文本，提取语言/异常类型/文件名。"""
        plan = RepairPlan(language="python")

        has_import_err = bool(re.search(r"ModuleNotFoundError|ImportError", issue, re.IGNORECASE))
        has_type_err = bool(re.search(r"TypeError", issue, re.IGNORECASE))
        if re.search(r"composite", issue, re.IGNORECASE) or (has_import_err and has_type_err):
            plan.issue_type = "composite"
        else:
            exc_match = re.search(r"(\w+(?:Error|Exception|Warning))", issue)
            if exc_match:
                exc_type = exc_match.group(1)
                plan.issue_type = self._classify_error(exc_type)
            if re.search(r"pyproject\.toml|\[tool\.", issue, re.IGNORECASE):
                plan.issue_type = "config_error"

        for file_match in re.finditer(r'File\s+"([^"]+)"', issue):
            name = file_match.group(1).replace("\\", "/")
            if name not in plan.suspect_files:
                plan.suspect_files.append(name)

        candidate_match = re.search(r"Candidate source files:\s*(.+)", issue, re.IGNORECASE)
        if candidate_match:
            for raw in candidate_match.group(1).split(","):
                name = raw.strip().replace("\\", "/")
                if name and name not in plan.suspect_files:
                    plan.suspect_files.append(name)

        if not plan.suspect_files:
            file_match = re.search(r"at (\S+\.py)", issue)
            if file_match:
                plan.suspect_files.append(Path(file_match.group(1)).name)

        line_no = self._parse_file_line(issue, plan.suspect_files[0] if plan.suspect_files else "")
        if line_no and plan.suspect_files:
            plan.reasoning = f"{plan.suspect_files[0]}:{line_no}"
        else:
            plan.reasoning = issue[:200]

        return plan

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
            if plan.issue_type == "import_error":
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

    def _match_skill(self, issue: str) -> dict | None:
        """从 YAML Skill 文件中匹配 Issue 对应的修复策略。"""
        skills_dir = Path(__file__).parent / "skills"
        if not skills_dir.exists():
            return None
        for yaml_file in skills_dir.glob("*.yaml"):
            try:
                data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
                pattern = data.get("trigger_pattern", "")
                if pattern and re.search(pattern, issue):
                    return data
            except Exception:
                pass
        return None

    def _classify_error(self, exc_type: str) -> str:
        mapping = {
            "TypeError": "type_error",
            "ImportError": "import_error",
            "ModuleNotFoundError": "import_error",
            "KeyError": "config_error",
            "AttributeError": "attribute_error",
            "ValueError": "value_error",
            "SyntaxError": "syntax_error",
        }
        return mapping.get(exc_type, "unknown")

    def _verification_enabled(self) -> bool:
        return self.verifier is not None or self.use_pytest_verify

    def _snapshot_repo(self) -> dict[str, str]:
        return snapshot_repo(self._repo_root)

    def _restore_repo_snapshot(self, snapshot: dict[str, str]) -> None:
        restore_repo_snapshot(self._repo_root, snapshot)

    @staticmethod
    def _patcher_system_prompt(plan: RepairPlan | None) -> str:
        """Patcher ``complete_once`` 使用的 L2 system 文本（base + issue suffix）。

        故意不含 L1 repair prefix（rules/tools/examples）：Patcher 单次 JSON
        completion 不调工具，经 ``complete_once(system_prompt=...)`` 注入，
        而非 ContextManager 的 role 段。
        """
        issue_type = plan.issue_type if plan else ""
        return load_role_prompt("patcher", issue_type)

    def _run_agent(
        self,
        agent,
        prompt: str,
        agent_name: str,
        state: RepairState | None = None,
    ) -> tuple[str, dict]:
        """执行 Agent 调用（Verifier 用，保留 Agent loop）。"""
        from agent_runtime.log_context import log_context

        t0 = time.time()
        run_id = getattr(agent, "shared_run_id", None)
        try:
            with log_context(run_id=run_id, agent=agent_name):
                answer = agent.ask(prompt)
        except Exception as e:
            if state is not None:
                state.agent_errors[agent_name] = str(e)
            log.warning("[%s] Agent 失败: %s", agent_name, e)
            elapsed_ms = int((time.time() - t0) * 1000)
            return "", {"total_ms": elapsed_ms, "internal": {}}
        elapsed_ms = int((time.time() - t0) * 1000)
        internal = getattr(agent, "_last_run_node_timings", None) or {}
        return answer, {"total_ms": elapsed_ms, "internal": dict(internal)}

    def _begin_repair_trace(self, state: RepairState) -> None:
        from agent_runtime.log_context import bind_run_id
        from src.repair.run_trace import RepairRunTracer

        tracer = RepairRunTracer(self._repo_root)
        run_id = tracer.begin(state.issue_input)
        tracer.bind_agents(self.localizer, self.retriever, self.patcher)
        self._repair_tracer = tracer
        state.repair_run_id = run_id
        self._log_run_id_token = bind_run_id(run_id)

    def _end_repair_trace(self, state: RepairState) -> None:
        from agent_runtime.log_context import reset_run_id

        tracer = self._repair_tracer
        if tracer is None:
            return
        token_summary = state.node_timings.get("token_usage") or {}
        tracer.finalize(state, token_summary)
        tracer.unbind_agents(self.localizer, self.retriever, self.patcher)
        self._repair_tracer = None
        token = getattr(self, "_log_run_id_token", None)
        if token is not None:
            reset_run_id(token)
            self._log_run_id_token = None

    def _reset_token_tracking(self) -> None:
        from src.eval.token_usage import reset_clients_session_usage

        reset_clients_session_usage(self.localizer, self.retriever, self.patcher)

    def _attach_token_usage(self, state: RepairState) -> None:
        from src.eval.token_usage import build_repair_token_usage, resolve_model_clients

        clients = resolve_model_clients(self.localizer, self.retriever, self.patcher)
        if not clients:
            return
        summary = build_repair_token_usage(
            clients,
            Path(self._repo_root),
            since_ts=getattr(self, "_repair_started_at", None),
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

    def _attach_rejection_stats(self, state: RepairState) -> None:
        from src.repair.rejection_aggregate import summarize_repair_rejections

        run_id = state.repair_run_id or ""
        if not run_id:
            return
        run_dir = Path(self._repo_root) / ".agent" / "runs" / run_id
        summary = summarize_repair_rejections(run_dir)
        for key, value in summary.items():
            state.node_timings[key] = value

    def _run_patcher(self, state: RepairState) -> tuple[list[CandidatePatch], dict]:
        """Patcher：直接调模型生成 JSON，Orchestrator 自己应用补丁。

        不走 Agent loop，因为 DeepSeek 不遵守工具调用格式。
        1 次 API 调用 → 解析 JSON → 直接 patch 文件。

        Returns:
            (applied_patches, timing_meta) where timing_meta has
            ``model_call_ms``, ``parse_apply_ms``, ``total_ms``.
        """
        plan = state.repair_plan
        prompt = self._patcher_prompt(
            state.suspect_locations,
            state.retrieved_context,
            state.feedback,
            plan=plan,
            issue=state.issue_input,
        )

        issue_type = plan.issue_type if plan else ""
        patcher_system = self._patcher_system_prompt(plan)

        t_start = time.time()
        t_model = time.time()
        tracer = self._repair_tracer
        if tracer:
            tracer.emit(
                "patcher",
                "complete_once_started",
                {
                    "issue_type": issue_type,
                    "prompt_variant": issue_type or "default",
                },
            )
            tracer.emit("patcher", "model_request_start", {"step": 1, "attempt": 1})
        usage_before = {}
        if self.patcher is not None:
            from src.eval.token_usage import get_client_session_usage

            usage_before = get_client_session_usage(self.patcher.model_client)
        try:
            raw = self.patcher.complete_once(prompt, system_prompt=patcher_system)
        except Exception as e:
            state.agent_errors["patcher"] = str(e)
            total_ms = int((time.time() - t_start) * 1000)
            model_call_ms = int((time.time() - t_model) * 1000)
            log.warning("[patcher] 模型调用失败: %s", e)
            return [], {
                "model_call_ms": model_call_ms,
                "parse_apply_ms": max(0, total_ms - model_call_ms),
                "total_ms": total_ms,
            }

        model_call_ms = int((time.time() - t_model) * 1000)
        if tracer and self.patcher is not None:
            from agent_runtime.model_timing import (
                build_report_latency_fields,
                collect_client_timings,
                emit_model_timing_events,
            )
            from src.eval.token_usage import diff_client_usage, get_client_session_usage

            timings = collect_client_timings(self.patcher.model_client)
            emit_model_timing_events(
                lambda event, payload: tracer.emit("patcher", event, payload),
                timings,
                default_attempt=1,
            )
            usage_after = get_client_session_usage(self.patcher.model_client)
            delta = diff_client_usage(usage_before, usage_after)
            latency_fields = build_report_latency_fields(timings)
            tracer.write_agent_token(
                "patcher",
                delta,
                extra={
                    "tool_steps": 0,
                    "node_timings": {"model_call_ms": model_call_ms},
                    "prompt_budget": getattr(self.patcher.config, "prompt_budget", None),
                    "budget_cuts": getattr(self.patcher, "_last_budget_meta", {}).get("cuts", []),
                    **latency_fields,
                },
            )
            tracer.emit("patcher", "complete_once_finished", {"token_usage": delta})

        patches = self._parse_patches(raw)
        if not patches:
            log.debug("[patcher] 0 patches parsed, raw[:300]=%r", raw.strip()[:300])

        applied_patches = self._apply_patches_on_disk(patches)
        if patches and not applied_patches:
            log.warning("[patcher] 补丁解析成功但未写入任何文件")
            state.agent_errors["patcher_apply"] = "apply_failed"

        total_ms = int((time.time() - t_start) * 1000)
        return applied_patches, {
            "model_call_ms": model_call_ms,
            "parse_apply_ms": max(0, total_ms - model_call_ms),
            "total_ms": total_ms,
        }

    def _apply_patches_on_disk(self, patches: list[CandidatePatch]) -> list[CandidatePatch]:
        return self._patch_applier().apply_patches(patches)

    def _run_verifier(self, state: RepairState) -> "VerificationResult":
        """Docker 沙箱或本地 pytest 验证（不走 LLM Agent loop）。"""
        if self.verifier is not None:
            run = DockerVerifyStrategy().run(
                self._repo_root,
                test_path=self._pick_test_path(state),
            )
            if run.error:
                log.warning("[verifier] 沙箱验证失败: %s", run.error)
            record_verify_timings(state, run, log_sandbox=True)
            return run.result
        if self.use_pytest_verify:
            run = PytestVerifyStrategy().run(self._repo_root)
            record_verify_timings(state, run)
            return run.result
        return VerificationResult(all_passed=False, failure_logs=["verifier 未配置"])

    def _pick_test_path(self, state: RepairState) -> str:
        """从 Retriever 结果中提取 pytest nodeid，避免跑全量 tests/。"""
        ctx = state.retrieved_context
        if not ctx or not ctx.related_tests:
            return ""
        for item in ctx.related_tests:
            if isinstance(item, str) and item.strip():
                return item.strip()
            if isinstance(item, dict):
                for key in ("nodeid", "name", "path"):
                    value = item.get(key, "")
                    if value:
                        return str(value).strip()
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

    def _build_feedback(self, result: "VerificationResult") -> str:
        """构建失败反馈文本。"""
        lines = ["补丁验证失败。"]
        if result.build_log:
            lines.append(f"构建日志: {result.build_log[:300]}")
        if result.failure_logs:
            lines.append("失败测试:")
            for log in result.failure_logs[:5]:
                lines.append(f"  - {log[:300]}")
        lines.append(
            "请根据失败日志修改补丁。使用 patch_file 直接修改文件，然后输出 CandidatePatch JSON。"
        )
        return "\n".join(lines)

    # ---- Prompt 构建 ----

    def _localizer_prompt(self, plan: RepairPlan, issue: str = "") -> str:
        parts = [f"定位以下问题：\n{issue or plan.reasoning}"]
        if plan.suspect_files:
            parts.append(f"嫌疑文件: {', '.join(plan.suspect_files)}")
        if plan.issue_type == "import_error":
            parts.append(
                "这是 import 错误（ModuleNotFoundError/ImportError）。"
                "优先 read_file 读取嫌疑文件的 import 行；无完整 traceback 时可跳过 stack_parse。"
                "最后输出 SuspectList JSON，指向错误的 import 语句行。"
            )
        else:
            parts.append(
                "请用 stack_parse 解析堆栈，再用 ast_parse 分析文件结构，"
                "最后输出 SuspectList JSON。"
            )
        return "\n".join(parts)

    def _retriever_prompt(
        self,
        suspects: list[SuspectLocation],
        plan: RepairPlan | None = None,
        issue: str = "",
    ) -> str:
        if suspects:
            parts = ["根据以下嫌疑位置搜索相关代码："]
            for s in suspects:
                parts.append(f"  - {s.file_path}:{s.start_line} {s.function_name or ''}")
            parts.append("请用 find_test 和 search 收集上下文，输出 RetrievedContext JSON。")
            return "\n".join(parts)

        if plan and plan.suspect_files:
            parts = [f"根据 Issue 和嫌疑文件搜索相关代码：\n{issue or plan.reasoning}"]
            parts.append(f"嫌疑文件: {', '.join(plan.suspect_files)}")
            parts.append("请用 find_test 和 search 收集上下文，输出 RetrievedContext JSON。")
            return "\n".join(parts)

        return (
            "搜索与该 Issue 相关的代码上下文。"
            "请用 search 和 find_test 搜索后输出 RetrievedContext JSON。"
        )

    def _patcher_prompt(
        self,
        suspects: list[SuspectLocation],
        context: RetrievedContext | None,
        feedback: str = "",
        plan: RepairPlan | None = None,
        issue: str = "",
    ) -> str:
        parts = []
        if feedback:
            parts.append(f"[上一轮验证反馈]\n{feedback}\n")
        parts.append("基于以下信息生成修复补丁：")

        if plan and re.search(r"cannot import name", issue, re.IGNORECASE):
            parts.append("修复提示: 除 import 行外，须同步修改本文件内对错误符号名的所有调用。")
        if plan and plan.issue_type == "composite":
            parts.append(
                f"至少修改 {len(plan.suspect_files or [])} 个相关文件中的每一处错误。"
            )
        if issue and "concatenate str" in issue.lower():
            parts.append(
                "Issue 表明 str 与 int 不能直接相加；修复后混合类型输入应得到数字运算结果。"
            )

        effective_suspects = suspects or (
            self._fallback_suspects_from_plan(plan, issue) if plan else []
        )

        if plan and plan.suspect_files:
            parts.append(f"只允许修改以下文件: {', '.join(plan.suspect_files)}")

        if effective_suspects:
            parts.append("嫌疑位置（代码已预读，无需再调用 read_file）:")
            for s in effective_suspects:
                if not s.file_path:
                    continue
                parts.append(f"  - {s.file_path}:{s.start_line} ({s.reason})")
                snippet = self._read_code_snippet(s.file_path, s.start_line, s.end_line)
                if snippet:
                    parts.append(snippet)
                else:
                    parts.append(f"    ⚠ 文件不存在: {s.file_path}")

        if plan and plan.issue_type == "composite" and plan.suspect_files:
            seen_paths = {s.file_path for s in effective_suspects if s.file_path}
            extra = [fp for fp in plan.suspect_files if fp not in seen_paths]
            if extra:
                parts.append("其他相关源文件（代码已预读）:")
                for fp in extra:
                    snippet = self._read_code_snippet(fp, 1, 80)
                    if snippet:
                        parts.append(f"  - {fp}")
                        parts.append(snippet)

        test_blocks = self._read_test_context(context, effective_suspects, plan)
        if test_blocks:
            parts.append("相关测试文件（补丁必须通过这些 assert）:")
            parts.extend(test_blocks)

        parts.append("直接输出 CandidatePatch JSON 列表，不要调用任何工具。")
        return "\n".join(parts)

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

    def _parse_suspect_list(self, answer: str) -> list[SuspectLocation]:
        return parse_suspect_list(answer)

    def _parse_retrieved_context(self, answer: str) -> RetrievedContext:
        return parse_retrieved_context(answer)

    def _verifier_prompt(self, patches: list[CandidatePatch], plan: RepairPlan | None) -> str:
        parts = ["验证以下补丁："]
        for p in patches:
            parts.append(f"  - {p.file_path}: {p.explanation or p.diff[:80]}")
        repo = self._repo_root
        parts.append(f"请用 sandbox_build({repo!r}) 构建，再用 sandbox_test({repo!r}) 测试。")
        return "\n".join(parts)

    def _parse_verification(self, answer: str) -> "VerificationResult":
        return parse_verification(answer)

    def _parse_patches(self, answer: str) -> list[CandidatePatch]:
        return parse_patches(answer)
