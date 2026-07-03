"""Orchestrator：纯 Python 编排器（不调 LLM）。

工作流：
1. _parse_issue() → 正则提取语言/异常类型/文件名 → RepairPlan
2. _match_skill() → 匹配 YAML Skill
3. _run_localizer() + _run_retriever() → 并行执行
4. _run_patcher() → 串行执行
"""

import re
import sys as _sys
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path

import yaml

from src.state import (
    CandidatePatch,
    RepairPlan,
    RepairState,
    RetrievedContext,
    SuspectLocation,
    VerificationResult,
)

DEFAULT_REPAIR_TIMEOUT_S = 180


class Orchestrator:
    """纯 Python 修复编排器。

    不调 LLM，只做调度和状态管理。
    """

    def __init__(self, localizer, retriever, patcher, verifier=None, *, use_pytest_verify: bool = False):
        self.localizer = localizer
        self.retriever = retriever
        self.patcher = patcher
        self.verifier = verifier
        self.use_pytest_verify = use_pytest_verify
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
                print(
                    f"[{_ts()}] ⚠ 修复超时 ({repair_timeout_s}s)\n",
                    end="",
                    file=_sys.stderr,
                    flush=True,
                )
                return state

    def _repair_impl(self, state: RepairState) -> RepairState:
        """修复流水线主体（可被 repair() 超时包装）。"""
        max_retries = state.max_retries
        issue = state.issue_input
        timings = {}

        t_start = time.time()
        self._repair_started_at = t_start
        self._reset_token_tracking()
        print(f"[{_ts()}] Orchestrator 开始\n", end="", file=_sys.stderr, flush=True)

        # Step 1: 解析 Issue → RepairPlan + 匹配 Skill
        t0 = time.time()
        state.repair_plan = self._parse_issue(issue)
        skill = self._match_skill(issue)
        if skill and state.repair_plan:
            state.repair_plan.estimated_impact = skill.get("suggested_tools", [])
        ms = int((time.time() - t0) * 1000)
        timings["parse_issue_ms"] = ms
        print(f"[{_ts()}] parse_issue: {ms}ms\n", end="", file=_sys.stderr, flush=True)

        # Step 2+3: Localizer + Retriever 并行（Retriever 用 parse_issue 的粗定位）
        print(
            f"[{_ts()}] Localizer + Retriever 并行开始...\n", end="", file=_sys.stderr, flush=True
        )
        t0 = time.time()
        suspects, context, loc_timing, ret_timing = self._run_localize_and_retrieve(state)
        wall_ms = int((time.time() - t0) * 1000)
        timings["localize_retrieve_ms"] = wall_ms
        timings["localizer_ms"] = loc_timing["total_ms"]
        timings["retriever_ms"] = ret_timing["total_ms"]
        state.suspect_locations = suspects
        state.retrieved_context = context
        state.node_timings["localizer_ms"] = loc_timing["total_ms"]
        state.node_timings["localizer_internal"] = loc_timing["internal"]
        state.node_timings["retriever_ms"] = ret_timing["total_ms"]
        state.node_timings["retriever_internal"] = ret_timing["internal"]
        n = len(suspects)
        n_tests = len(context.related_tests) if context else 0
        print(
            f"[{_ts()}] Localizer+Retriever 完成: 墙钟{wall_ms}ms "
            f"(L={loc_timing['total_ms']}ms, R={ret_timing['total_ms']}ms), "
            f"{n} suspect, {n_tests} tests\n",
            end="",
            file=_sys.stderr,
            flush=True,
        )

        # Step 4: Patcher → Verifier → 自愈
        while state.retry_count < max_retries:
            repo_snapshot = self._snapshot_repo() if self._verification_enabled() else None
            print(
                f"[{_ts()}] Patcher 开始 (retry={state.retry_count})...\n",
                end="",
                file=_sys.stderr,
                flush=True,
            )
            t0 = time.time()
            state.candidate_patches = self._run_patcher(state)
            ms = int((time.time() - t0) * 1000)
            timings["patcher_ms"] = ms
            n = len(state.candidate_patches)
            print(
                f"[{_ts()}] Patcher 完成: {ms}ms, {n}个补丁\n", end="", file=_sys.stderr, flush=True
            )

            if not self._verification_enabled():
                state.status = "patched" if state.candidate_patches else "failed"
                break

            if not state.candidate_patches:
                if state.agent_errors.pop("patcher_apply", None):
                    state.feedback = (
                        "补丁 JSON 解析成功但未能写入文件。"
                        "original_lines 必须与预读代码完全一致；优先使用 diff 字段。"
                    )
                else:
                    state.feedback = "补丁生成失败（模型返回无法解析的 JSON）。请重新生成。"
                state.retry_count += 1
                continue

            print(f"[{_ts()}] Verifier 开始...\n", end="", file=_sys.stderr, flush=True)
            t0 = time.time()
            state.verification_result = self._run_verifier(state)
            ms = int((time.time() - t0) * 1000)
            timings["verifier_ms"] = ms
            print(f"[{_ts()}] Verifier 完成: {ms}ms\n", end="", file=_sys.stderr, flush=True)

            if state.verification_result.all_passed:
                state.status = "fixed"
                break

            if repo_snapshot is not None:
                self._restore_repo_snapshot(repo_snapshot)
            else:
                self._revert_changes(state)
            state.feedback = self._build_feedback(state.verification_result)
            state.retry_count += 1

        if state.status not in ("fixed", "patched"):
            state.status = "exhausted" if state.retry_count >= max_retries else "failed"

        state.node_timings = timings
        self._attach_token_usage(state)
        total_ms = int((time.time() - t_start) * 1000)
        print(
            f"[{_ts()}] 总耗时: {total_ms}ms, status={state.status}\n",
            end="",
            file=_sys.stderr,
            flush=True,
        )
        return state

    def _parse_issue(self, issue: str) -> RepairPlan:
        """正则解析 Issue 文本，提取语言/异常类型/文件名。"""
        plan = RepairPlan(language="python")

        has_import_err = bool(
            re.search(r"ModuleNotFoundError|ImportError", issue, re.IGNORECASE)
        )
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

    def _resolve_repo_file(self, file_path: str) -> Path | None:
        """解析并校验路径在 --repo 内且文件存在。"""
        if not file_path:
            return None
        path = Path(file_path)
        root = Path(self._repo_root).resolve()
        if path.is_absolute():
            try:
                path.resolve().relative_to(root)
            except ValueError:
                return None
        else:
            path = (root / path).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            return None
        if not path.is_file():
            return None
        return path

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

    _SNAPSHOT_SKIP_DIRS = frozenset({".agent", ".pytest_cache", "__pycache__", ".git"})

    def _snapshot_repo(self) -> dict[str, str]:
        root = Path(self._repo_root)
        snap: dict[str, str] = {}
        if not root.is_dir():
            return snap
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if any(part in self._SNAPSHOT_SKIP_DIRS for part in Path(rel).parts):
                continue
            try:
                snap[rel] = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
        return snap

    def _restore_repo_snapshot(self, snapshot: dict[str, str]) -> None:
        root = Path(self._repo_root)
        for rel, content in snapshot.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def _call_model(self, agent, prompt: str) -> tuple[str, int]:
        """直接调用模型（绕过 Agent loop），返回 (answer, elapsed_ms)。"""
        # 拼接 system prompt
        prompt_file = None
        if agent is self.localizer:
            prompt_file = Path(__file__).parent / "prompts" / "localizer.txt"
        elif agent is self.retriever:
            prompt_file = Path(__file__).parent / "prompts" / "retriever.txt"
        elif agent is self.patcher:
            prompt_file = Path(__file__).parent / "prompts" / "patcher.txt"

        if prompt_file and prompt_file.exists():
            system_prompt = prompt_file.read_text(encoding="utf-8")
        else:
            system_prompt = ""
        full_prompt = system_prompt + "\n\n" + prompt if system_prompt else prompt

        t0 = time.time()
        raw = agent.model_client.complete(
            full_prompt,
            max_new_tokens=agent.config.max_new_tokens or 4096,
        )
        elapsed_ms = int((time.time() - t0) * 1000)
        return raw, elapsed_ms

    def _run_agent(
        self,
        agent,
        prompt: str,
        agent_name: str,
        state: RepairState | None = None,
    ) -> tuple[str, dict]:
        """执行 Agent 调用（Verifier 用，保留 Agent loop）。"""
        t0 = time.time()
        try:
            answer = agent.ask(prompt)
        except Exception as e:
            if state is not None:
                state.agent_errors[agent_name] = str(e)
            print(
                f"  [{agent_name}] ⚠ Agent 失败: {e}\n",
                end="",
                file=_sys.stderr,
                flush=True,
            )
            elapsed_ms = int((time.time() - t0) * 1000)
            return "", {"total_ms": elapsed_ms, "internal": {}}
        elapsed_ms = int((time.time() - t0) * 1000)
        internal = self._read_agent_timings(agent)
        return answer, {"total_ms": elapsed_ms, "internal": internal}

    def _read_agent_timings(self, agent) -> dict:
        """从 Agent 的最新 run 目录读取 node_timings。"""
        import json as _json
        from pathlib import Path as _Path

        try:
            runs_dir = _Path(agent.workspace.repo_root) / ".agent" / "runs"
            if not runs_dir.exists():
                return {}
            latest = max(runs_dir.iterdir(), key=lambda p: p.stat().st_mtime)
            data = _json.loads((latest / "task_state.json").read_text())
            return data.get("node_timings", {})
        except Exception:
            return {}

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
        )
        state.node_timings["total_tokens"] = summary["total_tokens"]
        state.node_timings["token_usage"] = summary

    def _run_localize_and_retrieve(
        self,
        state: RepairState,
    ) -> tuple[list[SuspectLocation], RetrievedContext, dict, dict]:
        """并行运行 Localizer 与 Retriever，返回结果与各自耗时。"""
        plan = state.repair_plan
        issue = state.issue_input

        def run_localizer():
            prompt = self._localizer_prompt(plan, issue)
            answer, timing = self._run_agent(
                self.localizer,
                prompt,
                "localizer",
                state,
            )
            suspects = self._parse_suspect_list(answer)
            if not suspects:
                print(
                    f"  [localizer] ⚠ 0 suspects, raw[:500]={answer.strip()[:500]!r}",
                    file=_sys.stderr,
                    flush=True,
                )
            return suspects, timing

        def run_retriever():
            prompt = self._retriever_prompt([], plan=plan, issue=issue)
            answer, timing = self._run_agent(
                self.retriever,
                prompt,
                "retriever",
                state,
            )
            return self._parse_retrieved_context(answer), timing

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_loc = pool.submit(run_localizer)
            fut_ret = pool.submit(run_retriever)
            suspects, loc_timing = fut_loc.result()
            context, ret_timing = fut_ret.result()

        if not suspects:
            suspects = self._fallback_suspects_from_plan(plan, issue)
            if suspects:
                print(
                    f"  [localizer] 降级: RepairPlan → {len(suspects)} suspect\n",
                    end="",
                    file=_sys.stderr,
                    flush=True,
                )

        return suspects, context, loc_timing, ret_timing

    def _run_localizer(self, state: RepairState) -> list[SuspectLocation]:
        prompt = self._localizer_prompt(state.repair_plan, state.issue_input)
        answer, timing = self._run_agent(self.localizer, prompt, "localizer")
        state.node_timings["localizer_ms"] = timing["total_ms"]
        state.node_timings["localizer_internal"] = timing["internal"]
        suspects = self._parse_suspect_list(answer)
        if not suspects:
            print(
                f"  [localizer] ⚠ 0 suspects, raw[:500]={answer.strip()[:500]!r}",
                file=_sys.stderr,
                flush=True,
            )
        return suspects

    def _run_retriever(self, state: RepairState) -> RetrievedContext:
        prompt = self._retriever_prompt(
            state.suspect_locations,
            plan=state.repair_plan,
            issue=state.issue_input,
        )
        answer, timing = self._run_agent(self.retriever, prompt, "retriever")
        state.node_timings["retriever_ms"] = timing["total_ms"]
        state.node_timings["retriever_internal"] = timing["internal"]
        return self._parse_retrieved_context(answer)

    def _run_patcher(self, state: RepairState) -> list[CandidatePatch]:
        """Patcher：直接调模型生成 JSON，Orchestrator 自己应用补丁。

        不走 Agent loop，因为 DeepSeek 不遵守工具调用格式。
        1 次 API 调用 → 解析 JSON → 直接 patch 文件。
        """
        prompt = self._patcher_prompt(
            state.suspect_locations,
            state.retrieved_context,
            state.feedback,
            plan=state.repair_plan,
            issue=state.issue_input,
        )

        # 构建完整 prompt：system prompt（patcher.txt）+ user prompt
        prompt_file = Path(__file__).parent / "prompts" / "patcher.txt"
        system_prompt = prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else ""
        full_prompt = system_prompt + "\n\n" + prompt if system_prompt else prompt

        t0 = time.time()
        try:
            raw = self.patcher.model_client.complete(
                full_prompt,
                max_new_tokens=self.patcher.config.max_new_tokens or 4096,
            )
        except Exception as e:
            state.agent_errors["patcher"] = str(e)
            elapsed_ms = int((time.time() - t0) * 1000)
            state.node_timings["patcher_ms"] = elapsed_ms
            print(
                f"  [patcher] ⚠ 模型调用失败: {e}\n",
                end="",
                file=_sys.stderr,
                flush=True,
            )
            return []
        elapsed_ms = int((time.time() - t0) * 1000)
        state.node_timings["patcher_ms"] = elapsed_ms
        state.node_timings["patcher_internal"] = {"model_call_ms": elapsed_ms}

        # 解析模型输出的 JSON → CandidatePatch 列表
        patches = self._parse_patches(raw)
        if not patches:
            print(
                f"  [patcher] ⚠ 0 patches parsed, raw[:300]={raw.strip()[:300]!r}",
                file=_sys.stderr,
                flush=True,
            )

        applied_patches = self._apply_patches_on_disk(patches)
        if patches and not applied_patches:
            print("  [patcher] ⚠ 补丁解析成功但未写入任何文件", file=_sys.stderr, flush=True)
            state.agent_errors["patcher_apply"] = "apply_failed"

        return applied_patches

    def _apply_patches_on_disk(self, patches: list[CandidatePatch]) -> list[CandidatePatch]:
        """将补丁写入宿主机仓库，返回成功应用的补丁列表。"""
        applied: list[CandidatePatch] = []
        for p in patches:
            file_path = self._resolve_repo_file(p.file_path)
            if file_path is None:
                print(
                    f"  [patcher] ⚠ 拒绝补丁（路径不在 repo 或文件不存在）: {p.file_path!r}",
                    file=_sys.stderr,
                    flush=True,
                )
                continue

            text = file_path.read_text(encoding="utf-8")
            new_text = apply_patch_to_text(text, p)
            if new_text is None:
                print(
                    f"  [patcher] ⚠ 无法应用补丁: {p.file_path}",
                    file=_sys.stderr,
                    flush=True,
                )
                continue

            new_text = _sync_import_symbol_usages(text, new_text, p)
            file_path.write_text(new_text, encoding="utf-8")
            applied.append(p)
        return applied

    def _run_verifier(self, state: RepairState) -> "VerificationResult":
        """Docker 沙箱或本地 pytest 验证（不走 LLM Agent loop）。"""
        if self.verifier is not None:
            return self._run_docker_verifier(state)
        if self.use_pytest_verify:
            return self._run_pytest_verifier(state)
        return VerificationResult(all_passed=False, failure_logs=["verifier 未配置"])

    def _run_docker_verifier(self, state: RepairState) -> "VerificationResult":
        from src.tools.sandbox_tools import run_sandbox_verification

        test_path = self._pick_test_path(state)
        t0 = time.time()
        try:
            result, internal = run_sandbox_verification(self._repo_root, test_path=test_path)
        except Exception as e:
            state.agent_errors["verifier"] = str(e)
            elapsed_ms = int((time.time() - t0) * 1000)
            state.node_timings["verifier_ms"] = elapsed_ms
            print(
                f"  [verifier] ⚠ 沙箱验证失败: {e}\n",
                end="",
                file=_sys.stderr,
                flush=True,
            )
            return VerificationResult(all_passed=False, failure_logs=[str(e)])
        elapsed_ms = int((time.time() - t0) * 1000)
        state.node_timings["verifier_ms"] = elapsed_ms
        state.node_timings["verifier_internal"] = internal
        if internal:
            print(
                f"  [verifier] sandbox: create={internal.get('container_create_ms', '?')}ms "
                f"tar={internal.get('tar_copy_ms', '?')}ms "
                f"pip={internal.get('pip_ms', '?')}ms "
                f"pytest={internal.get('pytest_ms', '?')}ms",
                file=_sys.stderr,
                flush=True,
            )
        return result

    def _run_pytest_verifier(self, state: RepairState) -> VerificationResult:
        from src.eval.runner import run_pytest

        t0 = time.time()
        code, out = run_pytest(Path(self._repo_root))
        elapsed_ms = int((time.time() - t0) * 1000)
        state.node_timings["verifier_internal"] = {"pytest_ms": elapsed_ms}
        passed = code == 0
        if not passed:
            print(
                f"  [verifier] pytest 失败 (exit={code})\n",
                end="",
                file=_sys.stderr,
                flush=True,
            )
        return VerificationResult(
            all_passed=passed,
            failure_logs=[out[-2000:]] if out and not passed else [],
        )

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

        if plan and plan.issue_type == "type_error":
            parts.append(
                "修复提示: 这是类型错误。若测试断言期望数字结果，"
                "请用 int()/float() 做数值转换，禁止 str() 拼接。"
            )
        if plan and plan.issue_type in ("import_error", "composite"):
            parts.append(
                "修复提示: import 错误通常修正 import 路径或模块名（如 helper → helpers）。"
                "只修改下方已提供的源文件，不要引用其他项目文件名。"
            )
            if re.search(r"cannot import name", issue, re.IGNORECASE):
                parts.append(
                    "修复提示: 除 import 行外，须同步修改本文件内对错误符号名的所有调用。"
                )
        if plan and plan.issue_type == "composite":
            parts.append(
                "修复提示: 复合错误可能需修改多个文件（import + 类型转换）。"
                "输出 JSON 数组，每项对应一个 file_path；"
                f"至少修改 {len(plan.suspect_files or [])} 个相关文件中的每一处错误。"
            )
        if plan and plan.issue_type == "config_error":
            parts.append(
                "修复提示: 配置错误通常需修改 pyproject.toml。"
                "使用 diff 字段追加 TOML 段（如 [tool.eval]），"
                "不要改 unrelated 字段；JSON 中 diff 用 \\n 表示换行，避免 multiline original_lines。"
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
        import json

        try:
            json_str = _extract_json_block(answer)
            data = json.loads(json_str)
            if isinstance(data, list):
                return [SuspectLocation.from_dict(item) for item in data]
        except (json.JSONDecodeError, KeyError):
            pass
        return []

    def _parse_retrieved_context(self, answer: str) -> RetrievedContext:
        import json

        try:
            json_str = _extract_json_block(answer)
            data = json.loads(json_str)
            if isinstance(data, dict):
                # 规范化 related_tests：模型可能返回 dict 列表，转为 str 列表
                if "related_tests" in data:
                    normalized = []
                    for t in data["related_tests"]:
                        if isinstance(t, str):
                            normalized.append(t)
                        elif isinstance(t, dict):
                            normalized.append(t.get("name", t.get("path", json.dumps(t))))
                        else:
                            normalized.append(str(t))
                    data["related_tests"] = normalized
                return RetrievedContext.from_dict(data)
        except (json.JSONDecodeError, KeyError):
            pass
        return RetrievedContext()

    def _verifier_prompt(self, patches: list[CandidatePatch], plan: RepairPlan | None) -> str:
        parts = ["验证以下补丁："]
        for p in patches:
            parts.append(f"  - {p.file_path}: {p.explanation or p.diff[:80]}")
        repo = self._repo_root
        parts.append(f"请用 sandbox_build({repo!r}) 构建，再用 sandbox_test({repo!r}) 测试。")
        return "\n".join(parts)

    def _parse_verification(self, answer: str) -> "VerificationResult":
        import json

        try:
            json_str = _extract_json_block(answer)
            data = json.loads(json_str)
            if isinstance(data, dict):
                return VerificationResult.from_dict(data)
        except (json.JSONDecodeError, KeyError):
            pass
        return VerificationResult()

    def _parse_patches(self, answer: str) -> list[CandidatePatch]:
        import json

        text = answer.strip()

        # 1. 从 markdown 代码块提取
        json_str = _extract_json_block(text)
        if json_str:
            try:
                data = json.loads(json_str)
                if isinstance(data, list):
                    return [CandidatePatch.from_dict(item) for item in data]
                if isinstance(data, dict) and "patches" in data:
                    return [CandidatePatch.from_dict(item) for item in data["patches"]]
            except (json.JSONDecodeError, KeyError):
                pass

        # 2. 尝试找文本中任意 JSON 数组
        for m in re.finditer(r"\[[\s\S]*?\{[\s\S]*?\}[\s\S]*?\]", text):
            try:
                data = json.loads(m.group())
                if isinstance(data, list) and len(data) > 0:
                    return [CandidatePatch.from_dict(item) for item in data]
            except (json.JSONDecodeError, KeyError):
                continue

        return []


def apply_patch_to_text(text: str, patch: CandidatePatch) -> str | None:
    """将 CandidatePatch 应用到文件文本，支持 original_lines 或 unified diff。"""
    if patch.original_lines and patch.patched_lines:
        if patch.original_lines in text:
            return text.replace(patch.original_lines, patch.patched_lines, 1)
        replaced = _replace_line_by_strip(text, patch.original_lines, patch.patched_lines)
        if replaced is not None:
            return replaced

    if patch.diff:
        result = _apply_unified_diff(text, patch.diff)
        if result is not None:
            return result
        return _apply_import_line_fallback(text, patch.diff)

    return None


def _sync_import_symbol_usages(old_text: str, new_text: str, patch: CandidatePatch) -> str:
    """import 符号重命名后，同步替换文件内对旧符号的调用。"""
    rename = _infer_import_symbol_rename(old_text, new_text, patch)
    if not rename:
        return new_text
    old_sym, new_sym = rename
    return re.sub(rf"\b{re.escape(old_sym)}\s*\(", f"{new_sym}(", new_text)


def _infer_import_symbol_rename(
    old_text: str, new_text: str, patch: CandidatePatch
) -> tuple[str, str] | None:
    """从 import 行变更推断符号重命名（hello → greet）。"""
    candidates: list[tuple[str, str]] = []
    if patch.original_lines and patch.patched_lines:
        pair = _extract_import_symbol_pair(patch.original_lines, patch.patched_lines)
        if pair:
            candidates.append(pair)
    minus, plus = _extract_diff_line_pairs(patch.diff or "")
    if minus and plus:
        pair = _extract_import_symbol_pair(minus[0], plus[0])
        if pair:
            candidates.append(pair)
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    for old_sym, new_sym in candidates:
        if old_sym == new_sym:
            continue
        for old_line, new_line in zip(old_lines, new_lines):
            if old_line == new_line:
                continue
            if not _is_import_line(old_line) or not _is_import_line(new_line):
                continue
            if old_sym in old_line and new_sym in new_line:
                return old_sym, new_sym
    return None


def _extract_import_symbol_pair(old_line: str, new_line: str) -> tuple[str, str] | None:
    old_m = re.search(r"import\s+(\w+)\s*(?:#|$)", old_line)
    new_m = re.search(r"import\s+(\w+)\s*(?:#|$)", new_line)
    if old_m and new_m and old_m.group(1) != new_m.group(1):
        return old_m.group(1), new_m.group(1)
    return None


def _is_import_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(("from ", "import "))


def _extract_diff_line_pairs(diff: str) -> tuple[list[str], list[str]]:
    minus: list[str] = []
    plus: list[str] = []
    for line in diff.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            minus.append(line[1:])
        elif line.startswith("+") and not line.startswith("+++"):
            plus.append(line[1:])
    return minus, plus


def _apply_import_line_fallback(text: str, diff: str) -> str | None:
    """import 行补丁匹配失败时，按 diff 中的模块路径替换对应 import 行。"""
    minus, plus = _extract_diff_line_pairs(diff)
    if len(minus) != 1 or len(plus) != 1:
        return None
    old_line, new_line = minus[0], plus[0]
    if not (_is_import_line(old_line) or _is_import_line(plus[0])):
        return None

    old_key = _line_match_key(old_line)
    if old_key:
        replaced = _replace_line_by_strip(text, old_line, new_line)
        if replaced is not None:
            return replaced

    old_module = _extract_import_module(old_line)
    new_module = _extract_import_module(new_line)
    if not new_module:
        return None

    lines = text.splitlines(keepends=True)
    for i, file_line in enumerate(lines):
        content = file_line.rstrip("\n\r")
        if not _is_import_line(content):
            continue
        file_module = _extract_import_module(content)
        should_replace = False
        if old_module and (old_module in content or _import_modules_related(file_module, old_module)):
            should_replace = True
        elif file_module and file_module != new_module and _import_modules_related(file_module, new_module):
            should_replace = True
        if not should_replace:
            continue
        indent = content[: len(content) - len(content.lstrip())]
        if file_module and file_module != new_module and file_module in content:
            replacement = content.replace(file_module, new_module, 1)
        else:
            replacement = new_line.strip()
            if indent and not replacement.startswith((" ", "\t")):
                replacement = indent + replacement
        ending = file_line[len(content) :] if file_line.endswith(("\n", "\r")) else "\n"
        lines[i] = replacement + ending
        return "".join(lines)
    return None


def _import_modules_related(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _extract_import_module(line: str) -> str:
    stripped = _line_match_key(line)
    m = re.match(r"from\s+([\w.]+)\s+import", stripped)
    if m:
        return m.group(1)
    m = re.match(r"import\s+([\w.]+)", stripped)
    if m:
        return m.group(1)
    return ""


def _line_match_key(line: str) -> str:
    """比较行内容时忽略注释与首尾空白。"""
    return line.split("#", 1)[0].strip()


def _replace_line_by_strip(text: str, old_line: str, new_line: str) -> str | None:
    """按 strip 后的内容匹配单行并替换，保留原缩进。"""
    old_key = _line_match_key(old_line)
    if not old_key:
        return None

    lines = text.splitlines(keepends=True)
    for i, file_line in enumerate(lines):
        content = file_line.rstrip("\n\r")
        if _line_match_key(content) != old_key:
            continue
        indent = content[: len(content) - len(content.lstrip())]
        replacement = new_line.strip()
        if indent and not replacement.startswith((" ", "\t")):
            replacement = indent + replacement
        ending = file_line[len(content) :] if file_line.endswith(("\n", "\r")) else "\n"
        lines[i] = replacement + ending
        return "".join(lines)
    return None


def _apply_unified_diff(text: str, diff: str) -> str | None:
    """应用简化的 unified diff（-/+ 行）。"""
    minus: list[str] = []
    plus: list[str] = []
    for line in diff.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            minus.append(line[1:])
        elif line.startswith("+") and not line.startswith("+++"):
            plus.append(line[1:])

    if not minus:
        return None

    old_block = "\n".join(minus)
    new_block = "\n".join(plus)
    if old_block in text:
        return text.replace(old_block, new_block, 1)

    if len(minus) == 1 and plus:
        replaced = _replace_line_by_strip(text, minus[0], plus[0])
        if replaced is not None:
            return replaced

    return None


def _ts() -> str:
    """返回当前时间戳字符串 HH:MM:SS。"""
    return time.strftime("%H:%M:%S")


def _extract_json_block(text: str) -> str:
    """从文本中提取 JSON 块（优先处理 markdown 代码块）。"""
    text = text.strip()

    # 1. 从 markdown ```json...``` 代码块提取（支持嵌套括号）
    md_start = re.search(r"```(?:json)?\s*", text)
    if md_start:
        content = text[md_start.end() :]
        md_end = content.rfind("```")
        if md_end >= 0:
            inner = content[:md_end].strip()
            if inner.startswith("{") or inner.startswith("["):
                return inner

    # 2. 尝试直接作为 JSON 解析
    if text.startswith("[") or text.startswith("{"):
        return text

    # 3. 搜索最近邻的完整 JSON 块（从最后一个 [ 或 { 开始）
    for start_char in ("[", "{"):
        end_char = "]" if start_char == "[" else "}"
        last_start = text.rfind(start_char)
        if last_start >= 0:
            # 从该位置找到配对的闭合
            depth = 0
            for i in range(last_start, len(text)):
                if text[i] == start_char:
                    depth += 1
                elif text[i] == end_char:
                    depth -= 1
                    if depth == 0:
                        return text[last_start : i + 1]

    return text
