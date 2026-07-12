"""Regenerate docs/bonus.md with ## and ### headings from DESIGN.md structure."""
import re
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BONUS = ROOT / "docs" / "bonus.md"
DESIGN = ROOT / "docs" / "bonus" / "DESIGN.md"

backlog_re = re.compile(r"^- \*\*\[P\d\]")


def parse_current_bonus(text: str) -> list[str]:
    return [line for line in text.splitlines() if backlog_re.match(line)]


ASSIGN: list[tuple[str, tuple[str, str | None]]] = [
    ("Agent 池化", ("1. Agent 运行时", None)),
    ("统一 token 会计", ("1. Agent 运行时", None)),
    ("REPL 热重载", ("1. Agent 运行时", None)),
    ("workspace 切换", ("1. Agent 运行时", None)),
    ("agent.register_tool", ("1. Agent 运行时", None)),
    ("多 Provider fallback", ("1. Agent 运行时", None)),
    ("统一 logger", ("1. Agent 运行时", None)),
    ("CancellationToken", ("2. Agent Loop / ReAct", "2.1 用户中断与取消")),
    ("协作式 cancel", ("2. Agent Loop / ReAct", "2.1 用户中断与取消")),
    ("TaskState.user_cancel", ("2. Agent Loop / ReAct", "2.1 用户中断与取消")),
    ("cancel 后 workspace", ("2. Agent Loop / ReAct", "2.1 用户中断与取消")),
    ("流式模型 cancel", ("2. Agent Loop / ReAct", "2.1 用户中断与取消")),
    ("REPL `/cancel`", ("2. Agent Loop / ReAct", "2.1 用户中断与取消")),
    ("补全 AgentLoop", ("2. Agent Loop / ReAct", None)),
    ("批量工具调用", ("2. Agent Loop / ReAct", None)),
    ("单步工具超时", ("2. Agent Loop / ReAct", None)),
    ("显式 ReAct", ("2. Agent Loop / ReAct", None)),
    ("单步 wall-clock", ("2. Agent Loop / ReAct", None)),
    ("stop_reason 枚举", ("2. Agent Loop / ReAct", None)),
    ("空转检测", ("2. Agent Loop / ReAct", None)),
    ("解析失败 recovery", ("2. Agent Loop / ReAct", None)),
    ("final_answer", ("2. Agent Loop / ReAct", None)),
    ("CoT 提取", ("2. Agent Loop / ReAct", None)),
    ("Plan 阶段", ("2. Agent Loop / ReAct", "2.2 执行前 Plan · TodoList")),
    ("Todo 状态机", ("2. Agent Loop / ReAct", "2.2 执行前 Plan · TodoList")),
    ("空转 / replan 读 todo", ("2. Agent Loop / ReAct", "2.2 执行前 Plan · TodoList")),
    ("REPL `/todos`", ("2. Agent Loop / ReAct", "2.2 执行前 Plan · TodoList")),
    ("八段 Context", ("3. Context 工程", "3.1 设计原则")),
    ("state section 注入", ("3. Context 工程", "3.1 设计原则")),
    ("knowledge 与 memory", ("3. Context 工程", "3.1 设计原则")),
    ("system/tools/skills 拆 prefix", ("3. Context 工程", "3.1 设计原则")),
    ("Skills 索引 + 全文按需", ("3. Context 工程", "3.2 五 Section 组装与 Token 预算")),
    ("SkillCatalog", ("13. Skill", "13.4 海量 Skill 加载")),
    ("多源 Skill 扫描", ("13. Skill", "13.4 海量 Skill 加载")),
    ("Skill 向量检索", ("13. Skill", "13.4 海量 Skill 加载")),
    ("L0 skill_mode 接线", ("13. Skill", "13.4 海量 Skill 加载")),
    ("Skill 索引 mtime", ("13. Skill", "13.4 海量 Skill 加载")),
    ("history canonical", ("3. Context 工程", "3.1 设计原则")),
    ("多模型 tokenizer", ("3. Context 工程", "3.2 五 Section 组装与 Token 预算")),
    ("cache 命中率进 report", ("3. Context 工程", "3.2 五 Section 组装与 Token 预算")),
    ("prefix 分段 hash", ("3. Context 工程", "3.2 五 Section 组装与 Token 预算")),
    ("build() metadata", ("3. Context 工程", "3.2 五 Section 组装与 Token 预算")),
    ("Tools 仅注入", ("3. Context 工程", "3.2 五 Section 组装与 Token 预算")),
    ("Skills 索引进", ("3. Context 工程", "3.2 五 Section 组装与 Token 预算")),
    ("User Message 模板", ("3. Context 工程", "3.2 五 Section 组装与 Token 预算")),
    ("摘要缓存持久化", ("3. Context 工程", "3.3 压缩管线 L0–L5")),
    ("增量摘要", ("3. Context 工程", "3.3 压缩管线 L0–L5")),
    ("KEEP_RECENT_HISTORY", ("3. Context 工程", "3.3 压缩管线 L0–L5")),
    ("native 路径接入", ("3. Context 工程", "3.3 压缩管线 L0–L5")),
    ("压缩阈值 yaml", ("3. Context 工程", "3.3 压缩管线 L0–L5")),
    ("issue/stack 钉扎", ("3. Context 工程", "3.4 L2 Repair 与 Memory 衔接")),
    ("共享 TokenBudget", ("3. Context 工程", "3.4 L2 Repair 与 Memory 衔接")),
    ("分 Agent 预算表", ("3. Context 工程", "3.4 L2 Repair 与 Memory 衔接")),
    ("钉扎区 registry", ("3. Context 工程", "3.4 L2 Repair 与 Memory 衔接")),
    ("recent_files 显式", ("4. 分层记忆", "4.2 四层模型与数据流")),
    ("episodic kind", ("4. 分层记忆", "4.2 四层模型与数据流")),
    ("episodic → durable", ("4. 分层记忆", "4.2 四层模型与数据流")),
    ("Candidate schema", ("4. 分层记忆", "4.3 写入管线")),
    ("冲突状态机", ("4. 分层记忆", "4.3 写入管线")),
    ("互斥 key", ("4. 分层记忆", "4.3 写入管线")),
    ("embed_query", ("4. 分层记忆", "4.4 召回与 Context 投影")),
    ("EMBED_MAX_TOKENS", ("4. 分层记忆", "4.4 召回与 Context 投影")),
    ("语料 chunk", ("4. 分层记忆", "4.4 召回与 Context 投影")),
    ("embedding 模型可插拔", ("4. 分层记忆", "4.4 召回与 Context 投影")),
    ("embedding 磁盘缓存", ("4. 分层记忆", "4.4 召回与 Context 投影")),
    ("记忆 GC", ("4. 分层记忆", "4.5 质量 · 衰减 · 隔离")),
    ("置信度时间衰减", ("4. 分层记忆", "4.5 质量 · 衰减 · 隔离")),
    ("topic 级 TTL", ("4. 分层记忆", "4.5 质量 · 衰减 · 隔离")),
    ("Memory Dream", ("4. 分层记忆", "4.5 质量 · 衰减 · 隔离")),
    ("健康 metric", ("4. 分层记忆", "4.5 质量 · 衰减 · 隔离")),
    ("本地路径隔离", ("4. 分层记忆", "4.5 质量 · 衰减 · 隔离")),
    ("repair precedent", ("4. 分层记忆", "4.6 L2 Repair 记忆桥接")),
    ("similar_fixes 置信度", ("4. 分层记忆", "4.6 L2 Repair 记忆桥接")),
    ("不信任记忆覆盖", ("4. 分层记忆", "4.6 L2 Repair 记忆桥接")),
    ("`/memory` 真实", ("4. 分层记忆", "4.7 运维与面试要点")),
    ("`/memory forget`", ("4. 分层记忆", "4.7 运维与面试要点")),
    ("Cache 命中率 REPL", ("5. Prompt", "5.1 Prefix · Cache · Rules")),
    ("few-shot", ("5. Prompt", "5.1 Prefix · Cache · Rules")),
    ("分 issue 类型 prompt", ("5. Prompt", "5.2 L2 角色 Prompt")),
    ("Skill 注入 Prompt", ("13. Skill", "13.3 注入与 Eval")),
    ("priority + 最长 pattern", ("13. Skill", "13.2 匹配算法")),
    ("Skill 命中率", ("13. Skill", "13.3 注入与 Eval")),
    ("Skill 包目录", ("13. Skill", "13.3 注入与 Eval")),
    ("search 正则", ("6. Agent Tool", "6.1 L1 通用工具")),
    ("write_file 原子", ("6. Agent Tool", "6.1 L1 通用工具")),
    ("search 结果上限", ("6. Agent Tool", "6.1 L1 通用工具")),
    ("list_files glob", ("6. Agent Tool", "6.1 L1 通用工具")),
    ("Retriever 规则快路径", ("6. Agent Tool", "6.2 注册与 L2 领域工具")),
    ("工具组合 ToolGroup", ("6. Agent Tool", "6.2 注册与 L2 领域工具")),
    ("Localizer 工具顺序", ("6. Agent Tool", "6.2 注册与 L2 领域工具")),
    ("ast_parse 局部", ("6. Agent Tool", "6.2 注册与 L2 领域工具")),
    ("ToolGateway 越权", ("7. ToolGateway", "7.2 调度 · 审计")),
    ("双层拒绝语义", ("7. ToolGateway", "7.2 调度 · 审计")),
    ("审批时 diff", ("8. 工具安全闸口", None)),
    ("Gate 5", ("8. 工具安全闸口", None)),
    ("Gate 7", ("8. 工具安全闸口", None)),
    ("符号链接逃逸", ("8. 工具安全闸口", None)),
    ("闸口拒绝统计", ("8. 工具安全闸口", None)),
    ("分 Agent 配额", ("9. 硬上限与工具配额", None)),
    ("context token 硬顶", ("9. 硬上限与工具配额", None)),
    ("Ollama / OpenAI streaming", ("10. 限流 · 熔断 · 降级", "10.1 模型 API")),
    ("熔断事件进 trace", ("10. 限流 · 熔断 · 降级", "10.1 模型 API")),
    ("分 Provider", ("10. 限流 · 熔断 · 降级", "10.1 模型 API")),
    ("Retry-After", ("10. 限流 · 熔断 · 降级", "10.1 模型 API")),
    ("半开成功阈值", ("10. 限流 · 熔断 · 降级", "10.1 模型 API")),
    ("HTTP keep-alive", ("10. 限流 · 熔断 · 降级", "10.1 模型 API")),
    ("Retriever 降级规则", ("10. 限流 · 熔断 · 降级", "10.2 修复流水线降级")),
    ("Multi-Agent 降级 Single-Agent", ("10. 限流 · 熔断 · 降级", "10.2 修复流水线降级")),
    ("每 tool 步 checkpoint", ("11. Checkpoint 断点恢复与续跑", None)),
    ("L2 阶段 checkpoint", ("11. Checkpoint 断点恢复与续跑", None)),
    ("blocker / next_step", ("11. Checkpoint 断点恢复与续跑", None)),
    ("SessionStore 损坏", ("11. Checkpoint 断点恢复与续跑", None)),
    ("cancel 时写 checkpoint", ("11. Checkpoint 断点恢复与续跑", None)),
    ("动态 Agent 裁剪", ("12. Multi-Agent 编排", "12.1 角色与机制")),
    ("Planner Agent", ("12. Multi-Agent 编排", "12.1 角色与机制")),
    ("分阶段超时", ("12. Multi-Agent 编排", "12.2 流水线编排")),
    ("asyncio 流水线", ("12. Multi-Agent 编排", "12.2 流水线编排")),
    ("状态机显式枚举", ("12. Multi-Agent 编排", "12.3 State 三层模型")),
    ("repair 落盘", ("12. Multi-Agent 编排", "12.3 State 三层模型")),
    ("L1/L2 State 关联", ("12. Multi-Agent 编排", "12.3 State 三层模型")),
    ("Agent 产物 JSON Schema", ("12. Multi-Agent 编排", "12.4 RepairState 状态机与产物")),
    ("前缀订阅", ("12. Multi-Agent 编排", "12.5 Blackboard 与 Agent 通信")),
    ("阶段级读写锁", ("12. Multi-Agent 编排", "12.6 并发与一致性")),
    ("workspace 写窗口单飞", ("12. Multi-Agent 编排", "12.6 并发与一致性")),
    ("分 Agent 独立 session", ("12. Multi-Agent 编排", "12.6 并发与一致性")),
    ("concurrent tool 硬顶", ("12. Multi-Agent 编排", "12.6 并发与一致性")),
    ("Orchestrator 冲突仲裁", ("12. Multi-Agent 编排", "12.7 冲突 · 终止 · 恢复")),
    ("Localizer∥Retriever 去重", ("12. Multi-Agent 编排", "12.7 冲突 · 终止 · 恢复")),
    ("冲突进 trace", ("12. Multi-Agent 编排", "12.7 冲突 · 终止 · 恢复")),
    ("降级 Single-Agent 最后一搏", ("12. Multi-Agent 编排", "12.7 冲突 · 终止 · 恢复")),
    ("RepairPlan.subtasks", ("12. Multi-Agent 编排", "12.8 子问题拆分")),
    ("composite Case 驱动", ("12. Multi-Agent 编排", "12.8 子问题拆分")),
    ("子问题失败隔离", ("12. Multi-Agent 编排", "12.8 子问题拆分")),
    ("多级 parse 降级", ("14. JSON 格式输出保证", None)),
    ("schema 校验层", ("14. JSON 格式输出保证", None)),
    ("解析失败自动重试 prompt", ("14. JSON 格式输出保证", None)),
    ("反馈环增强", ("15. 自愈闭环", None)),
    ("feedback 滑动窗口", ("15. 自愈闭环", None)),
    ("终止条件枚举", ("15. 自愈闭环", None)),
    ("仅暴露 `/code`", ("16. Docker 沙箱", "16.1 文件系统隔离")),
    ("tar 排除", ("16. Docker 沙箱", "16.1 文件系统隔离")),
    ("verify 后不留持久层", ("16. Docker 沙箱", "16.1 文件系统隔离")),
    ("宿主机零挂载", ("16. Docker 沙箱", "16.1 文件系统隔离")),
    ("网络策略文档", ("16. Docker 沙箱", "16.2 网络隔离")),
    ("sandbox 健康探针", ("16. Docker 沙箱", "16.3 资源隔离")),
    ("全局并发沙箱上限", ("16. Docker 沙箱", "16.3 资源隔离")),
    ("pytest 超时兜底", ("16. Docker 沙箱", "16.3 资源隔离")),
    ("禁止特权与 Docker-in-Docker", ("16. Docker 沙箱", "16.4 权限降级")),
    ("最小镜像 attack", ("16. Docker 沙箱", "16.4 权限降级")),
    ("逃逸回归 Case", ("16. Docker 沙箱", "16.5 开销与逃逸回归")),
    ("cancel/timeout 统一 kill", ("16. Docker 沙箱", "16.6 单 Turn 生命周期")),
    ("AST 语义等价", ("17. Patch 与 Verify", None)),
    ("prompt 注入 对抗", ("18. 敏感信息处理", None)),
    ("trace 保留策略", ("18. 敏感信息处理", None)),
    ("敏感产物加密", ("18. 敏感信息处理", None)),
    ("统一 run_id", ("19. 链路可观测", "19.1 Run · Trace · Report")),
    ("结构化 JSON 日志", ("19. 链路可观测", "19.1 Run · Trace · Report")),
    ("trace.jsonl gzip", ("19. 链路可观测", "19.1 Run · Trace · Report")),
    ("Prometheus", ("19. 链路可观测", "19.2 Repair · Agent 指标")),
    ("Grafana dashboard", ("19. 链路可观测", "19.2 Repair · Agent 指标")),
    ("Case 011", ("20. 消融实验与评测", "20.1 Case 库")),
    ("难度重标定", ("20. 消融实验与评测", "20.1 Case 库")),
    ("负样本 Case", ("20. 消融实验与评测", "20.1 Case 库")),
    ("多语言 Case", ("20. 消融实验与评测", "20.1 Case 库")),
    ("patch_equivalence_score", ("20. 消融实验与评测", "20.2 Runner 与指标")),
    ("并行跑 Case", ("20. 消融实验与评测", "20.2 Runner 与指标")),
    ("`--resume` 断点续跑", ("20. 消融实验与评测", "20.2 Runner 与指标")),
    ("Pass@k", ("20. 消融实验与评测", "20.2 Runner 与指标")),
    ("五 Section 独立硬顶", ("3. Context 工程", "3.2 五 Section 组装与 Token 预算")),
    ("tier_pins.yaml 接线", ("3. Context 工程", "3.2 五 Section 组装与 Token 预算")),
    ("fit 保护优先级", ("3. Context 工程", "3.2 五 Section 组装与 Token 预算")),
    ("prefix 稳定性单测", ("5. Prompt", "5.1 Prefix · Cache · Rules")),
    ("L2 prompt 文件 Jinja", ("5. Prompt", "5.2 L2 角色 Prompt")),
    ("角色 prompt schema_version", ("5. Prompt", "5.2 L2 角色 Prompt")),
    ("Skill 块注入 Prompt 统一", ("5. Prompt", "5.3 模板 · 调试 · Skill 块")),
    ("`/prompt` 导出", ("5. Prompt", "5.3 模板 · 调试 · Skill 块")),
    ("patch_file 多 hunk", ("6. Agent Tool", "6.1 L1 通用工具")),
    ("run_shell 环境变量", ("6. Agent Tool", "6.1 L1 通用工具")),
    ("`.agent/tools.yaml`", ("6. Agent Tool", "6.3 Schema 工程 · Manifest")),
    ("新工具 checklist", ("6. Agent Tool", "6.3 Schema 工程 · Manifest")),
    ("L2 registry 与 auto_schema", ("6. Agent Tool", "6.3 Schema 工程 · Manifest")),
    ("REPAIR_PERMISSION_TABLE yaml", ("7. ToolGateway", "7.1 权限表")),
    ("grant/revoke 审计", ("7. ToolGateway", "7.1 权限表")),
    ("Gateway 矩阵单测", ("7. ToolGateway", "7.3 测试 · 文档")),
    ("ADR 权限表变更", ("7. ToolGateway", "7.3 测试 · 文档")),
    ("Blackboard 接入 Orchestrator", ("12. Multi-Agent 编排", "12.5 Blackboard 与 Agent 通信")),
    ("merge 阶段读 Blackboard", ("12. Multi-Agent 编排", "12.5 Blackboard 与 Agent 通信")),
    ("Blackboard snapshot", ("12. Multi-Agent 编排", "12.5 Blackboard 与 Agent 通信")),
    ("Key 命名空间 lint", ("12. Multi-Agent 编排", "12.5 Blackboard 与 Agent 通信")),
    ("Skill YAML schema", ("13. Skill", "13.1 YAML Schema")),
    ("Skill 文件 CI lint", ("13. Skill", "13.1 YAML Schema")),
    ("node_timings 标准化", ("19. 链路可观测", "19.2 Repair · Agent 指标")),
    ("Context budget 进 report", ("19. 链路可观测", "19.3 工具 · Gateway · Context 指标")),
    ("Gateway 拒绝计数", ("19. 链路可观测", "19.3 工具 · Gateway · Context 指标")),
    ("意图 + Skill 快照", ("19. 链路可观测", "19.3 工具 · Gateway · Context 指标")),
    ("patch_precision 进 eval", ("20. 消融实验与评测", "20.2 Runner 与指标")),
    ("消融变体矩阵文档化", ("20. 消融实验与评测", "20.2 Runner 与指标")),
    ("regression_check CI", ("20. 消融实验与评测", "20.3 CI 门禁与基线")),
    ("eval `--fake` 冒烟", ("20. 消融实验与评测", "20.3 CI 门禁与基线")),
    ("ci_baseline_report", ("20. 消融实验与评测", "20.3 CI 门禁与基线")),
    ("Context token 标准化", ("19. 链路可观测", "19.4 核心运行时指标监控")),
    ("Prompt cache 命中率", ("19. 链路可观测", "19.4 核心运行时指标监控")),
    ("TTFT", ("19. 链路可观测", "19.4 核心运行时指标监控")),
    ("Retry 指标统一", ("19. 链路可观测", "19.4 核心运行时指标监控")),
    ("Tool 步数 + 配额", ("19. 链路可观测", "19.4 核心运行时指标监控")),
    ("REPL `/session` 指标面板", ("19. 链路可观测", "19.4 核心运行时指标监控")),
    ("fixloop_context_tokens", ("19. 链路可观测", "19.4 核心运行时指标监控")),
    ("Eval 聚合运行时指标", ("20. 消融实验与评测", "20.2 Runner 与指标")),
    ("运行时指标 CI 阈值", ("20. 消融实验与评测", "20.3 CI 门禁与基线")),
    ("分 Agent token / latency", ("19. 链路可观测", "19.2 Repair · Agent 指标")),
    ("命令历史", ("21. CLI · REPL（本地）", None)),
    ("`/memory` / `/memory forget`", ("21. CLI · REPL（本地）", None)),
    ("多行输入", ("21. CLI · REPL（本地）", None)),
    ("/save /load", ("21. CLI · REPL（本地）", None)),
    ("repair 退出码", ("21. CLI · REPL（本地）", None)),
    ("增量 repo snapshot", ("22. 配置 · 插件 · 可靠性", None)),
    ("IntentResult", ("23. 意图识别与路由", "23.1 L2 Issue 意图（Repair 入口）")),
    ("`_parse_issue` 规则补全", ("23. 意图识别与路由", "23.1 L2 Issue 意图（Repair 入口）")),
    ("歧义 issue LLM", ("23. 意图识别与路由", "23.1 L2 Issue 意图（Repair 入口）")),
    ("语言检测", ("23. 意图识别与路由", "23.1 L2 Issue 意图（Repair 入口）")),
    ("Skill 匹配置信度", ("23. 意图识别与路由", "23.2 Skill 策略匹配")),
    ("issue_type 路由表", ("23. 意图识别与路由", "23.3 意图 → 编排路由")),
    ("issue_type → prompt 变体自动", ("23. 意图识别与路由", "23.3 意图 → 编排路由")),
    ("REPL intent router", ("23. 意图识别与路由", "23.4 L1 会话意图（REPL / Memory）")),
    ("save_intent 多语言", ("23. 意图识别与路由", "23.4 L1 会话意图（REPL / Memory）")),
    ("repair 启动写 task_summary", ("23. 意图识别与路由", "23.4 L1 会话意图（REPL / Memory）")),
    ("意图识别进 trace", ("23. 意图识别与路由", "23.6 可观测与评测")),
    ("意图对抗 eval", ("23. 意图识别与路由", "23.6 可观测与评测")),
    ("压测场景库", ("24. 压测与容量", None)),
    ("CLI 退出码单测", ("25. 演示 · 文档 · 测试", None)),
    ("Skill 匹配", ("25. 演示 · 文档 · 测试", None)),
]


def assign_item(line: str) -> tuple[str, str | None]:
    for needle, key in ASSIGN:
        if needle in line:
            return key
    raise ValueError(f"unassigned backlog item: {line[:100]}")


def parse_design_structure(text: str) -> OrderedDict[str, list[str]]:
    groups: OrderedDict[str, list[str]] = OrderedDict()
    cur_h2: str | None = None
    for line in text.splitlines():
        if line.startswith("## ") and not line.startswith("## 目录"):
            cur_h2 = line[3:].strip()
            groups[cur_h2] = []
        elif line.startswith("### ") and cur_h2:
            groups[cur_h2].append(line[4:].strip())
    return groups


def main() -> None:
    items = parse_current_bonus(BONUS.read_text(encoding="utf-8"))
    bucket: dict[tuple[str, str | None], list[str]] = {}
    for it in items:
        key = assign_item(it)
        bucket.setdefault(key, []).append(it)

    structure = parse_design_structure(DESIGN.read_text(encoding="utf-8"))

    header = """# FixLoop Bonus 待实现条目

> **仅 backlog**；设计思路见 [docs/bonus/DESIGN.md](bonus/DESIGN.md) · 产品边界与 Web 归档见 [OUT_OF_SCOPE.md](bonus/OUT_OF_SCOPE.md)。  
> **产品边界**：本地 CLI / REPL + `src.cli repair`；不实现 Web / HTTP / 多租户。  
> 基线：`master` @ PR #87 · **558 tests**。格式：**[P?] [C:复杂度 I:面试价值] 标题**：方案摘要。

---

## 目录

| 章 | 设计 | 待办 |
|----|------|------|
| 1–25 | [DESIGN.md](bonus/DESIGN.md) | 见下方 § 与 ### 子节 |
| — | [Out of Scope](bonus/OUT_OF_SCOPE.md) | 🚫 不实现 |

---

"""

    out = header
    chapter_count = 0
    for h2, h3_list in structure.items():
        h2_items = bucket.get((h2, None), [])
        h3_blocks = [(h3, bucket.get((h2, h3), [])) for h3 in h3_list]
        if not h2_items and not any(items for _, items in h3_blocks):
            continue
        chapter_count += 1
        out += f"## {h2}\n\n"
        for h3, h3_items in h3_blocks:
            if not h3_items:
                continue
            out += f"### {h3}\n\n"
            for it in h3_items:
                out += it + "\n"
            out += "\n"
        if h2_items:
            for it in h2_items:
                out += it + "\n"
            out += "\n"
        out += "---\n\n"

    out += "*待办清单 · 不含已标 ✅ 的已完成项 · 设计见 [bonus/DESIGN.md](bonus/DESIGN.md)*\n"
    BONUS.write_text(out, encoding="utf-8")
    print(f"Wrote {len(items)} items across {chapter_count} chapters with ### subsections")


if __name__ == "__main__":
    main()
