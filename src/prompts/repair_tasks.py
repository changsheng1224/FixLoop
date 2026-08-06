"""L2 repair user message 模板渲染。"""

from __future__ import annotations

from pathlib import Path

from agent_runtime.template_render import render_template, template_metadata
from src.skills.skill_block import SkillBlockRender, SkillHintRole, render_skill_hint_for_plan

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


def _resolve_skill_render(
    plan,
    role: SkillHintRole,
    *,
    skill_render: SkillBlockRender | None = None,
) -> SkillBlockRender:
    if skill_render is not None:
        return skill_render
    return render_skill_hint_for_plan(plan, role)


def build_patcher_variables(
    *,
    feedback: str = "",
    evidence_block: str = "",
    runtime_contract_block: str = "",
    issue_hints_block: str = "",
    skill_hint_block: str = "",
    allowed_files_line: str = "",
    suspects_block: str = "",
    extra_files_block: str = "",
    test_blocks: str = "",
    disk_grounding_block: str = "",
) -> dict[str, str]:
    feedback_block = ""
    if feedback:
        feedback_block = f"[上一轮验证反馈]\n{feedback}\n"
    return {
        "feedback_block": feedback_block,
        "evidence_block": evidence_block,
        "runtime_contract_block": runtime_contract_block,
        "issue_hints_block": issue_hints_block,
        "skill_hint_block": skill_hint_block,
        "allowed_files_line": allowed_files_line,
        "suspects_block": suspects_block,
        "extra_files_block": extra_files_block,
        "test_blocks": test_blocks,
        "disk_grounding_block": disk_grounding_block,
    }


def build_verifier_variables(
    patches,
    repo_root: str,
    plan=None,
    *,
    skill_render: SkillBlockRender | None = None,
) -> tuple[dict[str, str], SkillBlockRender]:
    lines = []
    for p in patches:
        lines.append(f"  - {p.file_path}: {p.explanation or p.diff[:80]}")
    repo = repr(repo_root)
    render = (
        _resolve_skill_render(plan, "verifier", skill_render=skill_render)
        if plan
        else SkillBlockRender(text="", role="verifier", source="none")
    )
    variables = {
        "patches_list": "\n".join(lines),
        "repo": repo,
        "skill_hint_block": render.text,
    }
    return variables, render
