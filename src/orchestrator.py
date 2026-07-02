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


class Orchestrator:
    """纯 Python 修复编排器。

    不调 LLM，只做调度和状态管理。
    """

    def __init__(self, localizer, retriever, patcher, verifier=None):
        self.localizer = localizer
        self.retriever = retriever
        self.patcher = patcher
        self.verifier = verifier
        # 修复目标目录：优先 --repo / Agent cwd，而非 git 顶层仓库
        self._repo_root = str(Path.cwd())
        if localizer is not None:
            self._repo_root = (
                localizer._cwd
                or getattr(localizer.workspace, "cwd", "")
                or localizer.workspace.repo_root
                or self._repo_root
            )

    def repair(self, issue: str, max_retries: int = 3) -> RepairState:
        """执行修复流水线。

        Args:
            issue: Issue 描述（含堆栈和错误信息）。
            max_retries: 最大重试次数。

        Returns:
            RepairState 实例。
        """
        state = RepairState(issue_input=issue, max_retries=max_retries)
        timings = {}

        t_start = time.time()
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

        # Step 2: Localizer
        print(f"[{_ts()}] Localizer 开始...\n", end="", file=_sys.stderr, flush=True)
        t0 = time.time()
        state.suspect_locations = self._run_localizer(state)
        ms = int((time.time() - t0) * 1000)
        timings["localizer_ms"] = ms
        n = len(state.suspect_locations)
        print(f"[{_ts()}] Localizer 完成: {ms}ms, {n} suspect\n",
              end="", file=_sys.stderr, flush=True)

        # Step 3: Retriever
        print(f"[{_ts()}] Retriever 开始...\n", end="", file=_sys.stderr, flush=True)
        t0 = time.time()
        state.retrieved_context = self._run_retriever(state)
        ms = int((time.time() - t0) * 1000)
        timings["retriever_ms"] = ms
        print(f"[{_ts()}] Retriever 完成: {ms}ms\n", end="", file=_sys.stderr, flush=True)

        # Step 4: Patcher → Verifier → 自愈
        while state.retry_count < max_retries:
            print(f"[{_ts()}] Patcher 开始 (retry={state.retry_count})...\n",
                  end="", file=_sys.stderr, flush=True)
            t0 = time.time()
            state.candidate_patches = self._run_patcher(state)
            ms = int((time.time() - t0) * 1000)
            timings["patcher_ms"] = ms
            n = len(state.candidate_patches)
            print(f"[{_ts()}] Patcher 完成: {ms}ms, {n}个补丁\n",
                  end="", file=_sys.stderr, flush=True)

            if self.verifier is None:
                state.status = "patched"
                break

            if not state.candidate_patches:
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

            # 验证失败 → 回滚被修改的文件 → 反馈给 Patcher
            self._revert_changes(state)
            state.feedback = self._build_feedback(state.verification_result)
            state.retry_count += 1

        state.node_timings = timings
        total_ms = int((time.time() - t_start) * 1000)
        print(f"[{_ts()}] 总耗时: {total_ms}ms, status={state.status}\n",
              end="", file=_sys.stderr, flush=True)
        return state

    def _parse_issue(self, issue: str) -> RepairPlan:
        """正则解析 Issue 文本，提取语言/异常类型/文件名。"""
        plan = RepairPlan(language="python")

        # 异常类型
        exc_match = re.search(r"(\w+(?:Error|Exception|Warning))", issue)
        if exc_match:
            exc_type = exc_match.group(1)
            plan.issue_type = self._classify_error(exc_type)

        # 文件名和行号
        file_match = re.search(r'File\s+"([^"]+)"', issue)
        if not file_match:
            file_match = re.search(r"at (\S+\.py)", issue)
        if file_match:
            plan.suspect_files.append(file_match.group(1))

        # 行号
        line_match = re.search(r"line (\d+)", issue)
        if line_match and plan.suspect_files:
            plan.reasoning = f"{plan.suspect_files[0]}:{line_match.group(1)}"
        else:
            plan.reasoning = issue[:200]

        return plan

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
            "AttributeError": "attribute_error",
            "ValueError": "value_error",
            "SyntaxError": "syntax_error",
        }
        return mapping.get(exc_type, "unknown")

    def _call_model(self, agent, prompt: str) -> tuple[str, int]:
        """直接调用模型（绕过 Agent loop），返回 (answer, elapsed_ms)。"""
        # 拼接 system prompt
        prompt_file = None
        agent_type = ""
        if agent is self.localizer:
            prompt_file = Path(__file__).parent / "prompts" / "localizer.txt"
            agent_type = "localizer"
        elif agent is self.retriever:
            prompt_file = Path(__file__).parent / "prompts" / "retriever.txt"
            agent_type = "retriever"
        elif agent is self.patcher:
            prompt_file = Path(__file__).parent / "prompts" / "patcher.txt"
            agent_type = "patcher"

        system_prompt = prompt_file.read_text(encoding="utf-8") if prompt_file and prompt_file.exists() else ""
        full_prompt = system_prompt + "\n\n" + prompt if system_prompt else prompt

        t0 = time.time()
        raw = agent.model_client.complete(
            full_prompt, max_new_tokens=agent.config.max_new_tokens or 4096,
        )
        elapsed_ms = int((time.time() - t0) * 1000)
        return raw, elapsed_ms

    def _run_agent(self, agent, prompt: str, agent_name: str) -> tuple[str, dict]:
        """执行 Agent 调用（Verifier 用，保留 Agent loop）。"""
        t0 = time.time()
        answer = agent.ask(prompt)
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

    def _run_localizer(self, state: RepairState) -> list[SuspectLocation]:
        prompt = self._localizer_prompt(state.repair_plan, state.issue_input)
        answer, timing = self._run_agent(self.localizer, prompt, "localizer")
        state.node_timings["localizer_ms"] = timing["total_ms"]
        state.node_timings["localizer_internal"] = timing["internal"]
        suspects = self._parse_suspect_list(answer)
        if not suspects:
            print(f"  [localizer] ⚠ 0 suspects, raw[:500]={answer.strip()[:500]!r}",
                  file=_sys.stderr, flush=True)
        return suspects

    def _run_retriever(self, state: RepairState) -> RetrievedContext:
        prompt = self._retriever_prompt(state.suspect_locations)
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
            state.suspect_locations, state.retrieved_context, state.feedback
        )

        # 构建完整 prompt：system prompt（patcher.txt）+ user prompt
        prompt_file = Path(__file__).parent / "prompts" / "patcher.txt"
        system_prompt = prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else ""
        full_prompt = system_prompt + "\n\n" + prompt if system_prompt else prompt

        t0 = time.time()
        raw = self.patcher.model_client.complete(
            full_prompt, max_new_tokens=self.patcher.config.max_new_tokens or 4096,
        )
        elapsed_ms = int((time.time() - t0) * 1000)
        state.node_timings["patcher_ms"] = elapsed_ms
        state.node_timings["patcher_internal"] = {"model_call_ms": elapsed_ms}

        # 解析模型输出的 JSON → CandidatePatch 列表
        patches = self._parse_patches(raw)
        if not patches:
            print(f"  [patcher] ⚠ 0 patches parsed, raw[:300]={raw.strip()[:300]!r}",
                  file=_sys.stderr, flush=True)

        applied = self._apply_patches_on_disk(patches)
        if patches and not applied:
            print("  [patcher] ⚠ 补丁解析成功但未写入任何文件", file=_sys.stderr, flush=True)

        return patches

    def _apply_patches_on_disk(self, patches: list[CandidatePatch]) -> int:
        """将补丁写入宿主机仓库，返回成功应用的补丁数。"""
        applied = 0
        for p in patches:
            if not p.file_path:
                continue
            file_path = Path(p.file_path)
            if not file_path.is_absolute():
                file_path = Path(self._repo_root) / file_path
            if not file_path.is_file():
                print(f"  [patcher] ⚠ 文件不存在: {file_path}", file=_sys.stderr, flush=True)
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

            file_path.write_text(new_text, encoding="utf-8")
            applied += 1
        return applied

    def _run_verifier(self, state: RepairState) -> "VerificationResult":
        """直连 Docker harness 验证（与 Patcher 相同，不走 LLM Agent loop）。"""
        from src.tools.sandbox_tools import run_sandbox_verification

        test_path = self._pick_test_path(state)
        t0 = time.time()
        result, internal = run_sandbox_verification(self._repo_root, test_path=test_path)
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
                    capture_output=True, timeout=10,
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
            "请根据失败日志修改补丁。使用 patch_file 直接修改文件，"
            "然后输出 CandidatePatch JSON。"
        )
        return "\n".join(lines)

    # ---- Prompt 构建 ----

    def _localizer_prompt(self, plan: RepairPlan, issue: str = "") -> str:
        parts = [f"定位以下问题：\n{issue or plan.reasoning}"]
        if plan.suspect_files:
            parts.append(f"嫌疑文件: {', '.join(plan.suspect_files)}")
        parts.append("请用 stack_parse 解析堆栈，再用 ast_parse 分析文件结构，最后输出 SuspectList JSON。")
        return "\n".join(parts)

    def _retriever_prompt(self, suspects: list[SuspectLocation]) -> str:
        if not suspects:
            return "搜索与该 Issue 相关的代码上下文。请用 search 和 find_test 搜索后输出 RetrievedContext JSON。"
        parts = ["根据以下嫌疑位置搜索相关代码："]
        for s in suspects:
            parts.append(f"  - {s.file_path}:{s.start_line} {s.function_name or ''}")
        parts.append("请用 search/find_test/git_blame 搜索，输出 RetrievedContext JSON。")
        return "\n".join(parts)

    def _patcher_prompt(
        self,
        suspects: list[SuspectLocation],
        context: RetrievedContext | None,
        feedback: str = "",
    ) -> str:
        parts = []
        if feedback:
            parts.append(f"[上一轮验证反馈]\n{feedback}\n")
        parts.append("基于以下信息生成修复补丁：")
        if suspects:
            parts.append("嫌疑位置（代码已预读，无需再调用 read_file）:")
            for s in suspects:
                if not s.file_path:
                    continue
                parts.append(f"  - {s.file_path}:{s.start_line} ({s.reason})")
                # 预读嫌疑文件的实际代码，防止模型幻觉
                file_path = Path(s.file_path)
                if not file_path.is_absolute():
                    file_path = Path(self._repo_root) / file_path
                if file_path.is_file():
                    lines = file_path.read_text(encoding="utf-8").split("\n")
                    ctx_start = max(0, s.start_line - 3)
                    ctx_end = min(len(lines), s.end_line + 3)
                    parts.append(f"    ```python")
                    for i in range(ctx_start, ctx_end):
                        marker = ">>>" if s.start_line - 1 <= i < s.end_line else "   "
                        parts.append(f"    {marker} {lines[i]}")
                    parts.append("    ```")
                else:
                    parts.append(f"    ⚠ 文件不存在: {file_path}")
        if context and context.related_tests:
            test_names = []
            for t in context.related_tests:
                if isinstance(t, str):
                    test_names.append(t)
                elif isinstance(t, dict):
                    test_names.append(t.get("name", t.get("path", str(t))))
                else:
                    test_names.append(str(t))
            if test_names:
                parts.append(f"相关测试: {', '.join(test_names)}")
        parts.append(
            "请用 read_file 确认上下文后，用 patch_file 生成最小化 diff。"
            "输出 CandidatePatch JSON 列表。"
        )
        return "\n".join(parts)

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
        return _apply_unified_diff(text, patch.diff)

    return None


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
        ending = file_line[len(content):] if file_line.endswith(("\n", "\r")) else "\n"
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
        content = text[md_start.end():]
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
                        return text[last_start:i + 1]

    return text
