"""Planner Agent: 单次 JSON complete → RepairPlan（只规划不调 tool）。"""

from __future__ import annotations

PLANNER_PROMPT = """你是一个代码修复规划师。分析以下 issue 并输出 RepairPlan JSON。

只输出 JSON 对象，不要输出其他内容：
{
  "language": "python",
  "issue_type": "type_error|import_error|composite|test_failure|config_error|logic_error",
  "reasoning": "简短的一句话判断依据（<200 chars）",
  "suspect_files": ["文件1.py", "文件2.py"],
  "subtasks": [
    {"id": "fix_xxx", "goal": "修复 xxx 中的错误", "suspect_files": ["文件1.py"]}
  ]
}

规则：
- composite 类型时 subtasks 至少 2 个，每个 subtask 只含一个文件
- suspect_files 从 issue 中提取（堆栈 File "..."、错误消息中的路径）
- 不要调用任何工具，只输出 JSON
"""


def create_planner(client, workspace, cwd: str = ""):
    """创建 Planner agent（仅供单次 complete 调用，不走 Agent loop）。

    Returns: agent instance 供 orchestrator 调用 plan_with_llm()。
    """
    from agent_runtime.config import AgentConfig
    from agent_runtime.providers.clients import FakeModelClient
    from agent_runtime.runtime import Agent

    config = AgentConfig(
        provider="deepseek",
        max_steps=1,  # Planner 只做单次 complete，不走 Agent loop
        max_new_tokens=512,
        approval="auto",
        json_mode=True,
    )
    return Agent(
        config=config,
        model_client=client,
        workspace=workspace,
        cwd=cwd or workspace.repo_root,
        tools={},
        system_prompt=PLANNER_PROMPT,
        agent_name="planner",
    )


__all__ = ["PLANNER_PROMPT", "create_planner"]
