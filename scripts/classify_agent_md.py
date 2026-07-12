"""Classify docs/agent.md questions into <=10 categories and rewrite the file."""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_MD = ROOT / "docs" / "agent.md"

CATEGORIES: list[tuple[str, str]] = [
    (
        "基础概念与 Agent 范式",
        "Agent 定义、ReAct/Plan-Execute 等范式、框架选型、工作流边界、趋势判断",
    ),
    (
        "Context、Prompt 与结构化输出",
        "上下文组装与压缩、Prompt 工程、JSON/Schema 输出、temperature 与 token 预算",
    ),
    (
        "工具、Skill 与协议生态",
        "Tool/Skill 设计、Function Calling、MCP/CLI/A2A、Middleware 与 Hook",
    ),
    (
        "记忆工程",
        "分层记忆、写入/召回/隔离/遗忘、用户画像、Memory 与 Context 边界",
    ),
    (
        "RAG 与知识检索",
        "检索链路、Hybrid/Rerank、HyDE/GraphRAG、Embedding、查询改写与知识卡片",
    ),
    (
        "多 Agent 编排与意图路由",
        "Orchestrator、分工协作、意图识别、SubAgent、状态机与 Blackboard",
    ),
    (
        "安全、沙箱与 Human-in-the-Loop",
        "沙箱隔离、权限与越界防护、审批合规、Prompt Injection、脱敏",
    ),
    (
        "运行时控制、Cancel 与 Checkpoint",
        "重试/熔断/降级、取消语义、断点续跑、超时与停止条件、幂等",
    ),
    (
        "评测、幻觉与质量保障",
        "指标与 RAGAS、Pass@k、Bad Case 闭环、幻觉度量、LLM-as-Judge",
    ),
    (
        "工程化、性能与系统设计",
        "Trace/SSE/缓存/延迟、分布式部署、代码修复 Harness、系统设计题、面试综合",
    ),
]

# Higher priority first: (category_index, unique substring in question body)
BODY_RULES: list[tuple[int, str]] = [
    # 多 Agent（优先于基础概念里的「多 Agent」字样）
    (5, "Orchestrator"),
    (5, "多 Agent（定位"),
    (5, "真 Multi-Agent"),
    (5, "SubAgent"),
    (5, "意图识别"),
    (5, "消息总线还是共享状态板"),
    (5, "Supervisor 模式"),
    (5, "Planner-Executor"),
    (5, "Critic-Reflector"),
    (5, "多 Agent 震荡"),
    (5, "Blackboard"),
    (5, "各子Agent的核心职责"),
    (5, "todolist"),
    (5, "Reviewer和Executor产生协同震荡"),
    (5, "子Agent能不能共享工具"),
    (5, "多智能体分工"),
    (5, "编排Orchestration"),
    (5, "多Agent设计"),
    # 安全沙箱
    (6, "沙箱"),
    (6, "sandbox"),
    (6, "Human-in-the-Loop"),
    (6, "Human in the loop"),
    (6, "审批"),
    (6, "Prompt Injection"),
    (6, "脱敏"),
    (6, "CSRF"),
    (6, "危险工具"),
    (6, "越界路径"),
    (6, "可靠性与安全"),
    (6, "保证Agent安全的沙箱"),
    # Cancel / checkpoint
    (7, "cancel"),
    (7, "Cancel"),
    (7, "checkpoint"),
    (7, "Checkpoint"),
    (7, "断点续跑"),
    (7, "幂等"),
    (7, "空转检测"),
    (7, "停止条件"),
    (7, "最大迭代次数"),
    (7, "死循环或无限调工具"),
    # 评测幻觉
    (8, "幻觉"),
    (8, "RAGAS"),
    (8, "Pass@"),
    (8, "Bad Case"),
    (8, "LLM-as-Judge"),
    (8, "LLM AS JUDGE"),
    (8, "回归评测"),
    (8, "评估与部署"),
    (8, "怎么度量幻觉"),
    # RAG
    (4, "Agentic RAG"),
    (4, "GraphRAG"),
    (4, "HyDE"),
    (4, "Hybrid Search"),
    (4, "Rerank"),
    (4, "召回为空"),
    (4, "查询改写"),
    (4, "知识卡片"),
    (4, "Embedding"),
    (4, "RAG还有价值"),
    (4, "Grep、Read"),
    (4, "Grep/Read"),
    (4, "Self-RAG"),
    (4, "Multi-Query"),
    # 记忆
    (3, "记忆"),
    (3, "Memory"),
    (3, "用户画像"),
    (3, "遗忘机制"),
    (3, "Mem0"),
    (3, "Episodic Memory"),
    (3, "memory多用户"),
    (3, "cc的记忆机制"),
    # Context/Prompt
    (1, "Context Engineering"),
    (1, "Context 组装"),
    (1, "上下文"),
    (1, "Prompt"),
    (1, "提示词"),
    (1, "压缩"),
    (1, "摘要"),
    (1, "token"),
    (1, "结构化输出"),
    (1, "temperature"),
    (1, "三角色"),
    (1, "Lost in the Middle"),
    (1, "钉扎区"),
    (1, "L1 截断"),
    (1, "L5 LLM"),
    # 工具/Skill/协议
    (2, "ToolGateway"),
    (2, "Function Calling"),
    (2, "function calling"),
    (2, "MCP"),
    (2, "Skill"),
    (2, "工具"),
    (2, "Tool Schema"),
    (2, "Middleware"),
    (2, "Hook"),
    (2, "A2A"),
    (2, "Observation"),
    (2, "tool是直接暴露"),
    # 工程化（系统设计 / 代码修复 / 性能）
    (9, "设计 ToolGateway"),
    (9, "设计 cancel"),
    (9, "设计四层 Memory"),
    (9, "设计 LLM 网关"),
    (9, "pytest"),
    (9, "patch apply"),
    (9, "Docker"),
    (9, "Harness"),
    (9, "trace"),
    (9, "SSE"),
    (9, "WebSocket"),
    (9, "KV Cache"),
    (9, "Prompt Cache"),
    (9, "首 token"),
    (9, "首 Token"),
    (9, "Langfuse"),
    (9, "用 2 分钟介绍"),
    (9, "为什么不用 LangChain"),
    (9, "为什么 Orchestrator"),
    (9, "为什么 ground truth"),
    (9, "为什么要有 ToolGateway"),
    (9, "为什么 cancel"),
    (9, "trade-off"),
    (9, "trade off"),
    (9, "系统变慢"),
    (9, "Ollama"),
    (9, "vLLM"),
    # 基础概念（放后面作兜底）
    (0, "ReAct"),
    (0, "Plan-and-Execute"),
    (0, "Reflexion"),
    (0, "CoT"),
    (0, "Tree of Thoughts"),
    (0, "Agent Loop"),
    (0, "Agentic Loop"),
    (0, "Loop Engineering"),
    (0, "框架选型"),
    (0, "LangGraph"),
    (0, "Coze"),
    (0, "Dify"),
    (0, "工作流"),
    (0, "什么是AI Agent"),
    (0, "Chatbot"),
    (0, "LoRA"),
    (0, "SFT"),
    (0, "MoE"),
    (0, "Scaling Law"),
    (0, "程序员在Agent"),
    (0, "垂直 Agent"),
]

KEYWORDS: list[tuple[int, list[str]]] = [
    (4, ["rag", "检索", "召回", "rerank", "chunk", "向量"]),
    (3, ["memory", "记忆", "画像"]),
    (6, ["沙箱", "审批", "脱敏", "injection", "csrf", "合规"]),
    (7, ["cancel", "checkpoint", "幂等", "熔断", "限流", "超时", "空转"]),
    (5, ["orchestrator", "subagent", "意图", "编排", "supervisor", "blackboard"]),
    (2, ["工具", "tool", "skill", "mcp", "schema", "middleware"]),
    (1, ["context", "prompt", "上下文", "压缩", "json", "token"]),
    (8, ["幻觉", "ragas", "pass@", "bad case", "评测", "指标"]),
    (0, ["react", "范式", "框架", "工作流", "langgraph"]),
    (9, ["trace", "sse", "cache", "延迟", "部署", "redis", "harness", "pytest"]),
]


def parse_questions(text: str) -> list[str]:
    bodies: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("|") or s.startswith(">") or s == "---":
            continue
        m = re.match(r"^(\*\*)?\d+\.\s*(.+?)(\*\*)?$", s)
        if not m:
            continue
        body = m.group(2).strip()
        if m.group(1):
            body = f"**{body}**"
        bodies.append(body)
    return bodies


def classify(body: str) -> int:
    plain = body.strip("*")
    for cat_idx, needle in BODY_RULES:
        if needle in plain:
            return cat_idx
    low = plain.lower()
    scores: dict[int, int] = defaultdict(int)
    for cat_idx, keys in KEYWORDS:
        for k in keys:
            if k in low:
                scores[cat_idx] += 1
    if scores:
        return max(scores.items(), key=lambda x: (x[1], -x[0]))[0]
    # 无关键词命中：偏工程实践/场景题，归入系统设计类
    if plain.startswith("什么是"):
        return 0
    return 9


def main() -> None:
    text = AGENT_MD.read_text(encoding="utf-8")
    questions = parse_questions(text)
    if len(questions) != 499:
        raise SystemExit(f"Expected 499 questions, got {len(questions)}")

    buckets: list[list[str]] = [[] for _ in CATEGORIES]
    for body in questions:
        buckets[classify(body)].append(body)

    lines: list[str] = [
        "# Agent 面试题库（按类型分类）",
        "",
        "> 共 **499** 题 · **10** 类 · 题号为分类后全局连续编号",
        "",
        "## 目录",
        "",
        "| # | 分类 | 题量 | 题号范围 |",
        "|---|------|------|----------|",
    ]

    global_n = 0
    for i, (title, _) in enumerate(CATEGORIES):
        n = len(buckets[i])
        if n:
            start, end = global_n + 1, global_n + n
            global_n = end
            lines.append(f"| {i + 1} | {title} | {n} | {start}–{end} |")
        else:
            lines.append(f"| {i + 1} | {title} | 0 | — |")

    lines.extend(["", "---", ""])

    global_n = 0
    for i, (title, desc) in enumerate(CATEGORIES):
        lines.append(f"## {i + 1}. {title}")
        lines.append(f"> {desc}")
        lines.append("")
        for body in buckets[i]:
            global_n += 1
            if body.startswith("**") and body.endswith("**"):
                inner = body.strip("*").strip()
                lines.append(f"**{global_n}. {inner}**")
            else:
                lines.append(f"{global_n}. {body}")
            lines.append("")
        lines.append("---")
        lines.append("")

    AGENT_MD.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote {AGENT_MD} ({global_n} questions)")
    for i, (title, _) in enumerate(CATEGORIES):
        print(f"  [{i + 1}] {title}: {len(buckets[i])}")


if __name__ == "__main__":
    main()
