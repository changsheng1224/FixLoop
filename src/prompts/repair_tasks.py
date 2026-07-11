"""L2 repair user message 模板渲染。"""

from __future__ import annotations

from pathlib import Path

from agent_runtime.template_render import render_template, template_metadata
from src.prompts.loader import load_localizer_hints
from src.repair.prompt_router import apply_prompt_routing, localizer_hints_key_for

_TASKS_DIR = Path(__file__).parent / "tasks"


def load_repair_task_template(name: str) -> tuple[str, str]:
    path = _TASKS_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"repair task template not found: {name}")
    text = path.read_text(encoding="utf-8").strip()
    return text, f"src/prompts/tasks/{name}.md"


def render_repair_task(name: str, variables: dict[str, str]) -> tuple[str, dict]:
    template, source = load_repair_task_template(name)
    return render_template(template, variables), template_metadata(template, source)


def build_localizer_variables(plan, issue: str = "") -> dict[str, str]:
    if not plan.prompt_variants:
        apply_prompt_routing(plan)
    issue_text = issue or plan.reasoning
    suspect_line = ""
    if plan.suspect_files:
        suspect_line = f"嫌疑文件: {', '.join(plan.suspect_files)}"
    hints = load_localizer_hints(localizer_hints_key_for(plan))
    return {
        "issue": issue_text,
        "suspect_files_line": suspect_line,
        "issue_type_hints": hints,
    }


def build_retriever_template_and_variables(
    suspects,
    plan=None,
    issue: str = "",
) -> tuple[str, dict[str, str]]:
    if suspects:
        lines = ["根据以下嫌疑位置搜索相关代码："]
        for s in suspects:
            lines.append(f"  - {s.file_path}:{s.start_line} {s.function_name or ''}")
        return "retriever_suspects", {
            "suspects_list": "\n".join(lines[1:]),
            "header": lines[0],
        }

    if plan and plan.suspect_files:
        return "retriever_plan", {
            "issue": issue or plan.reasoning,
            "suspect_files": ", ".join(plan.suspect_files),
        }

    return "retriever_fallback", {}


def build_patcher_variables(
    *,
    feedback: str = "",
    issue_hints_block: str = "",
    skill_hint_block: str = "",
    allowed_files_line: str = "",
    suspects_block: str = "",
    extra_files_block: str = "",
    test_blocks: str = "",
) -> dict[str, str]:
    feedback_block = ""
    if feedback:
        feedback_block = f"[上一轮验证反馈]\n{feedback}\n"
    return {
        "feedback_block": feedback_block,
        "issue_hints_block": issue_hints_block,
        "skill_hint_block": skill_hint_block,
        "allowed_files_line": allowed_files_line,
        "suspects_block": suspects_block,
        "extra_files_block": extra_files_block,
        "test_blocks": test_blocks,
    }


def build_verifier_variables(patches, repo_root: str) -> dict[str, str]:
    lines = []
    for p in patches:
        lines.append(f"  - {p.file_path}: {p.explanation or p.diff[:80]}")
    repo = repr(repo_root)
    return {
        "patches_list": "\n".join(lines),
        "repo": repo,
    }
