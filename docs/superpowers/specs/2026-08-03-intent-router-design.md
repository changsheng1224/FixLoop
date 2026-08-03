# Intent Router（规则 + Embedding + LLM + 多意图 DAG）设计与实现说明

> **Bonus / DESIGN ref:** docs/bonus.md §22 · docs/bonus/DESIGN.md §23  
> **Layer:** L1 核心（`agent_runtime/intent/`）+ L2 adapter（`src/orchestrator.py` / REPL）  
> **Status:** **已实现（P0+P1 + 后续增强）** — 抽象产品说明另见 `docs/INTENT_USER_RECOGNITION.md`  
> **选型（已确认 2026-08-03）：**
> - 三层：规则 → Embedding 原型匹配 →（弱时）light LLM  
> - 原型粒度：**仅动作级（A）**，不做 issue_type 级原型（B 留后续）  
> - 模块根：`agent_runtime/intent/`  
> - 首期范围：**P0 + P1**（L2 接线 + REPL 分流）  
> - **多意图 / DAG：方案 A — 规划就绪**（产出 `IntentGraph`；REPL **拓扑串行**执行；不做并行调度）  
>
> **后续已落地（相对原稿增补，见 §14–§18）：** 显式置信度与 clarify-only 门控、history-first 多轮指代、  
> 候选意图发现（规则/金标/LLM 旁路）、LLM 同次返回 Top-k 候选、分层评测与 held-out 压力集、堆栈优先槽位。

---

## FixLoop Context

- **问题：** 意图散落在 `_parse_issue`、`_has_save_intent`、`prompt_router`；无统一 schema；REPL 无通用路由；多句输入无法区分「多意图」vs「单意图+约束」，也无法表达执行依赖。
- **目标：** 一处分类 → 可表达多意图 DAG；规则优先；动作级原型 + embedding；弱信号 LLM；L2 兼容单节点；REPL 按图串行执行；可观测进 trace。
- **Primary modules:**
  - `agent_runtime/intent/`：models · segmenter · rules · embed · llm_fallback · planner · graph · router · confidence · clarify · dialogue · candidates · stack_parse · observability · eval_* · executor · adapters
  - `agent_runtime/features/memory/semantic.py`（复用 embed 模型 / cache）
  - `src/orchestrator.py`：`_parse_issue` 委托 adapter（消费图中 repair 节点）
  - REPL 入口：`route` → 拓扑串行执行 + 指代投影 + `/candidates`
  - `src/repair/prompt_router.py`：**消费** `RepairPlan.issue_type`，语义不变
- **Acceptance（首期）：**
  - `pytest tests/test_intent_router.py tests/test_intent_embed.py tests/test_intent_graph.py -v`
  - `pytest tests/test_orchestrator.py -v -k "ParseIssue or Inject or issue"`（回归）
  - 多句：多意图 vs 约束附着金样例；REPL 串行 remember→repair 等
- **Branch:** `bonus/intent-router` 或 `M9/D1/intent-router`
- **预估：** P0 ~450–550 行 + P1 ~200–280 行（含 YAML/图/测）；不替代 Skill 匹配；**不做并行 DAG executor**

---

## 1. 目标与非目标

### 1.1 目标

1. 统一 `IntentResult` + **`IntentGraph`**（节点动作意图 + 边依赖/顺序/约束）。
2. **多意图**：用户一句/多句可产出 ≥2 个可执行节点。
3. **消歧**：区分「多句 = 多意图」vs「多句 = 单一意图 + 约束附着」。
4. 规则 + Embedding +（弱时）LLM 三层分类（可对整段与分段双重信号）。
5. P0：L2 `repair --issue` 经 Router；图折叠为单 repair 计划 + slots。
6. P1：REPL 按 DAG **拓扑序串行**执行各节点 action。
7. Trace：`intent_routed`（含 graph 摘要）。

### 1.2 非目标（本期不做）

- issue_type 级 embedding 原型库（粒度 B）
- **并行** DAG 调度 / 工作窃取 / 跨节点取消传播产品化（方案 B）
- 用 Intent 替代 Skill YAML `trigger_pattern`
- 常驻小模型、用户在线自定义意图无评审
- Web NLU / 闲聊 Agent
- 任意深度自然语言规划（首期边类型闭集，见 §5）

---

## 2. 架构

```text
用户输入 (REPL / --issue)
        │
        ├─ 会话历史（原文）+ 意图薄投影（待澄清 / 上轮摘要 / referents）
        ▼
┌─────────────────────────────────────────────────────────────┐
│ IntentRouter.route(text, context)                           │
│  0. 指代/澄清续写（history-first；失败 → unresolved clarify） │
│  1. preprocess / slash 短路                                 │
│  2. Segmenter → segments[]（含同句双意图弱切）                │
│  3. 每段 Rule + Embed → fuse（c_rule/c_embed/c_fuse）       │
│  4. MultiIntentPlanner → IntentGraph                        │
│  5. 弱图 → LLM 一次修正图，并返回 candidates[] 旁路          │
│  6. 置信门控：低置信 / 冲突 / 歧义 → clarify-only            │
│  7. 候选发现事件写入 raw_signals（及可选 JSONL）              │
│  8. IntentResult { primary, graph, breakdown, raw_signals } │
└─────────────────────────────────────────────────────────────┘
        │
        ├─ channel=repair
        │     graph → 折叠 repair 节点 + 约束 slots
        │     → IssueIntentAdapter → RepairPlan
        │
        └─ channel=repl
              allow_execute=false → 只澄清
              else IntentGraphExecutor.serial(graph)
```

**边界：**

- Router + Planner **只规划**（分类、填槽建议、建图），**不调度 Agent、不执行工具**。
- P1 的 `IntentGraphExecutor` 是 **薄串行 runner**（按 action 调现有 API），不是通用工作流引擎。
- 并行边可存在于图表示中，但执行器 **一律串行化**（按稳定拓扑序；同层按 `priority` 再 `span.start`）。
- **产品默认（REPL）：** 置信不足或冲突时 **只澄清、不自动执行**（无 `fallback=ask` 静默续跑）。
- **候选意图：** 仅发现与评审输入，**不自动扩展**闭集 taxonomy。

---

## 3. Schema

### 3.1 `IntentNode`

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | `str` | 稳定 id，如 `n0`、`n1` |
| `primary` | `str` | 动作级 intent id（闭集，§3.4） |
| `action` | `str` | 执行动作（同原表） |
| `role` | `Literal["executable","constraint","clarify"]` | 可执行 / 仅附着约束 / 澄清 |
| `span` | `{start, end}` 或 `segment_index` | 对应原文区间 |
| `text` | `str` | 该节点覆盖的原文片段 |
| `slots` | `dict` | 节点局部槽 |
| `confidence` | `float` | |
| `parser` | `str` | 该节点主要来源层 |
| `priority` | `int` | 默认同层排序，越大越先（可选） |

### 3.2 `IntentEdge`

| 字段 | 类型 | 说明 |
|------|------|------|
| `src` | `str` | 边起点 node id |
| `dst` | `str` | 边终点 node id |
| `kind` | `Literal["sequence","depends_on","constrains"]` | 见 §5 |
| `reason` | `str` | 短解释 |

**边语义（方案 A）：**

| kind | 含义 | 串行执行含义 |
|------|------|----------------|
| `sequence` | 用户叙述顺序 / 建议先后 | `src` 执行完再 `dst` |
| `depends_on` | 硬依赖（`dst` 依赖 `src` 完成） | 同左；环则降级 clarify |
| `constrains` | `src`(constraint) 约束 `dst`(executable) | **不单独执行 src**；合并 slots 到 dst |

### 3.3 `IntentGraph`

```text
nodes: list[IntentNode]
edges: list[IntentEdge]
mode: "single" | "multi" | "hybrid"   # hybrid = 1 可执行 + ≥1 约束
root_ids: list[str]                   # 入度为 0 的可执行节点
```

不变量：

- 无环（若检测到环 → 整次 `clarify` 或拆成单节点 ask）
- `constrains` 的 src.role 必须为 `constraint`
- 至少 0 个可执行节点；0 个则可整体 `clarify` / `help`

### 3.4 `IntentResult`（兼容层）

| 字段 | 类型 | 说明 |
|------|------|------|
| `primary` | `str` | **兼容**：主可执行节点的 primary；多意图时取 `root_ids[0]` 或最高 priority |
| `secondary` | `list[str]` | **派生**：其余可执行节点 primary 列表（由 graph 生成，不再手填） |
| `graph` | `IntentGraph` | **权威结构** |
| `confidence` | `float` | 图置信：可执行节点 conf 的加权平均（可执行优先） |
| `parser` | `str` | 全局参与层汇总 |
| `action` | `str` | 兼容：主节点 action；多意图时可为 `run_graph`（P1 REPL 识此标志走 executor） |
| `slots` | `dict` | 兼容：主节点 slots ∪ 附着约束合并结果 |
| `reason` | `str` | |
| `embed_*` / `confidence_breakdown` / `raw_signals` | | 保留；`raw_signals` 含 `mode`、`segment_count`、消歧理由 |

`action=run_graph`：仅当 `mode=multi` 且 ≥2 可执行节点时设置；`single`/`hybrid` 仍用主节点 action。

### 3.5 动作级 Taxonomy（闭集）

| `primary` | `action` | 说明 |
|-----------|----------|------|
| `ask` | `ask` | 默认问答 / ReAct |
| `explain` | `explain_code` | 解释代码 / diff / 行为 |
| `remember` | `promote_memory` | 对齐 `_has_save_intent` |
| `repair_request` | `run_repair` | REPL「帮我修」+ 附带文本 |
| `repair_issue` | `run_repair` | CLI `--issue` / 纯堆栈通道 |
| `review` | `review_code` | 代码审查 / 找隐患 |
| `refactor` | `run_refactor` | 无失败信号的重构 |
| `implement` | `run_implement` | 新功能 / 补实现 |
| `test` | `run_tests` | 写测 / 跑测 / 覆盖率 |
| `debug` | `run_debug` | 排查根因（可先不改代码） |
| `search` | `search_codebase` | 只检索定位 |
| `plan` | `make_plan` | 先出计划 / 方案 |
| `help` | `help` | `/help`、能力说明 |
| `clarify` | `clarify` | 过短/歧义/环/无法消歧 |
| `cancel` | `noop_cancel` | 文案识别；真取消仍靠 CancelToken |
| `out_of_scope` | `reject` | 一期可选 |

共 **16** 类动作级意图。`issue_type` **不是** `primary`，进 `slots["issue_type"]`。

**消歧（摘要）：** 堆栈/显式「修」→ `repair_*`；「解释」→ `explain`；「有没有问题」→ `review`；无失败信号的重构/新功能 → `refactor`/`implement`。

**约束角色常用槽（附着到 executable，不单独成可执行意图）：**

| 槽 / 约束类型 | 例 |
|---------------|----|
| `language` | 「只用 Python」 |
| `suspect_files` / `scope` | 「只改 utils.py」 |
| `issue_type` 提示 | 「这是 TypeError」附在堆栈后 |
| `prefer_tests` | 「先别跑全量，只跑 test_x」 |
| `note` | 软约束原文，进 slots 供下游 prompt |

### 3.6 `RouteContext`

```text
channel: "repair" | "repl"
light_client: optional
emit: optional Callable
max_executable_nodes: int = 4
tau_node / tau_llm / tau_clarify / tau_exec
embed_fn: optional
history: optional list[{role,content}]   # 会话历史（指代材料）
dialogue: optional DialogueProjection    # 薄投影（待澄清/上轮/referents）
candidate_root: optional path            # 若设置则追加候选事件 JSONL
```

- `channel=repair`：允许多段堆栈/说明，但 **可执行节点折叠为 1× `repair_issue`**；其余段倾向 `constraint` 附着。  
- `channel=repl`：允许 `mode=multi` 多可执行节点；低置信 clarify-only。

---

## 4. 分段与单段分类

### 4.1 Segmenter

启发式（规则为主，不依赖 NLP 句分割器）：

1. 按空行 / 换行拆大块  
2. 按中文句号/问号/感叹号、英文 `.?!`（保护 `file.py`、`v1.2`）  
3. 按连接词弱切：`然后` / `另外` / `同时` / `and then` / `also`（标记 `cue=sequential|additive`）  
4. 过短碎片（&lt; 2 字）并入前段  

输出：`list[Segment]`：`{index, text, cue}`。

单段输入：segments 长度为 1，走原单意图路径。

### 4.2 每段三层分类（复用原流水线）

对每个 segment（及可选「全文」一次）：

1. **RuleLayer** → `c_r`, 候选 primary / slots  
2. **EmbedLayer** → `c_e`, top1/margin  
3. **fuse** → 段级候选 `IntentNode`（暂 `role=executable`）  
4. 段级弱信号可暂不调 LLM，留给 Planner 图级一次 LLM

全文强 slash（`/help` `/cancel`）可短路：单节点图，忽略其余噪声。

---

## 5. 多意图 vs 单意图+约束（Planner）

### 5.1 判定 `mode`

对段级候选集合：

```text
若仅 1 个高置信动作类候选（ask/remember/repair_*/help/...）
  → mode=single（其余段尝试附着为 constraint）

若 ≥2 个「互斥可执行」动作且置信均 ≥ τ_node（默认 0.55）
  且不满足「约束附着」模式
  → mode=multi

若 1 个可执行 + ≥1 个被判为约束的段
  → mode=hybrid
```

**互斥可执行：** `remember`、`repair_*`、`ask`、`help`、`cancel` 两两不同即视为可独立执行（`help`+`ask` 可合并为 help 优先单节点，特例）。

### 5.2 约束附着启发式（优先于拆成第二意图）

段 B 判为 **constraint**（附着到最近/主 executable A）若命中任一：

1. **无独立动词意图**：无 remember/repair/ask 强规则，且 embed top1 为 clarify 或低分  
2. **约束句式**：`只/仅/不要/先/用…`、`only` / `don't` / `please use` / `scope:`  
3. **纯元数据**：文件路径列表、`language: py`、issue_type 名词、行号范围  
4. **接续说明**：紧跟在 repair 段后的堆栈续行 / 「报错如下」后的 traceback（并入同一 repair 节点 text+slots，而非新节点）

例：

| 输入 | mode | 图 |
|------|------|-----|
| `帮我修这个 TypeError。\n只用改 foo.py` | hybrid | n0=repair；n1=constraint --constrains→ n0 |
| `请记住用 pytest。然后帮我修这个失败。` | multi | n0=remember -sequence→ n1=repair |
| `Traceback... TypeError...`（CLI --issue） | single | n0=repair_issue（多行并一段或并节点） |
| `这个函数是干什么的？另外 AgentLoop 呢？` | multi 或 single | 两 ask：**首期合并为 1× ask（全文）** 或 2× ask 串行；默认 **合并单 ask** 降噪 |
| `修这个 bug 并且记住别再用 unittest` | multi | repair + remember；边 `sequence`（叙述序）或 remember 先 `depends_on` 可选；**默认叙述序 sequence** |

### 5.3 建边规则（确定性，可测）

1. `mode=hybrid`：每个 constraint → `constrains` → 主 executable（或 span 上最近的 executable）  
2. `mode=multi`：按 segment 顺序对可执行节点连 `sequence` 链（n0→n1→n2）  
3. 用户显式「先…再…」：同 `sequence`；「必须先记住再修」可升为 `depends_on`（规则命中「先/必须」）  
4. `cancel` / `help` 与其它共存：`cancel` 独占单节点图；`help` 优先单节点  

### 5.4 图级 LLM（一次）

触发：`mode` 不确定、可执行数冲突、或图 conf &lt; `τ_llm`。

要求 JSON 示例：

```json
{
  "mode": "multi|single|hybrid",
  "nodes": [
    {"id": "n0", "primary": "remember", "role": "executable", "segment_index": 0},
    {"id": "n1", "primary": "repair_request", "role": "executable", "segment_index": 1}
  ],
  "edges": [
    {"src": "n0", "dst": "n1", "kind": "sequence"}
  ]
}
```

校验：primary/role/kind 闭集；非法则回退规则图或 `clarify`。

### 5.5 阈值

| 名 | 默认 | 含义 |
|----|------|------|
| `τ_node` | 0.55 | 段成为独立可执行节点 |
| `τ_llm` | 0.55 | 图级 LLM |
| `τ_clarify` | 0.45 | REPL 过弱 → clarify |
| `τ_exec` | 0.60 | 单节点自动执行门槛 |
| `max_executable_nodes` | 4 | 超出 → clarify |

---

## 6. 三层细节（单段 / 兼容）

与前一版相同，摘要如下：

- **规则：** slash → remember → 强 bug/stack（含 pytest FAILURES 粘贴）→ 企业动作关键词 → 过短 clarify → 默认 ask；repair 通道填 issue slots；同句多意图 lead 可切分；修+重构等同句冲突 → 澄清  
- **Embedding：** `prototypes.yaml` 动作级 3–8 例；max-pool cosine；模型缺失跳过  
- **融合：** 强规则优先；一致则加权；冲突记 breakdown / slots；产出 `c_rule` / `c_embed` / `c_fuse`，图级 `c_graph` / `min_node_conf`  
- **LLM（弱图）：** FakeClient / 无 client 跳过；调用时 **同一次**要求返回：
  - 意图图（mode/nodes/edges，primary **闭集优先**）
  - `candidates[{label,confidence,merge_into}]` Top-k（闭集优先；新短名仅作提名）
  - `need_clarify`（布尔，旁路信号，不替代门控）
  - 新标签 **不自动入典**，写入候选发现事件 `source=llm_nominate`

约束段可用轻量规则为主，**不强制**每段都跑 embed（省开销）；Planner 需要时再 embed。

堆栈槽位：**traceback 区域优先**（过滤 site-packages / `.venv` 等），避免大段粘贴污染 `suspect_files`。

---

## 7. 接线

### 7.1 P0 — L2

1. `route(issue, channel=repair)` → 期望 `mode ∈ {single, hybrid}`，恰好 **1** 可执行 `repair_*`  
2. 合并所有 `constrains` 入该节点 `slots`  
3. `IssueIntentAdapter.to_repair_plan(result)` → 现有 `RepairPlan` + `apply_prompt_routing`  
4. 若误产出 multi：取 conf 最高的 repair 节点，其余记 `raw_signals.dropped_nodes`（不 fail）  
5. Trace：`intent_routed` 含 `mode`、node 摘要、边摘要  

### 7.2 P1 — REPL 串行 Executor

```text
result = route(line, channel=repl)
if result.action == clarify/help/reject/cancel: 处理并 return
nodes = topological_sort(result.graph)  # 仅 executable；constraint 已合并
for node in nodes:
    dispatch(node.action, node.text, node.slots)
    # 失败策略（A）：fail-fast，打印错误，不继续后续节点
```

- `promote_memory` → 现有 remember/promote API  
- `run_repair` → `orch.repair`  
- `ask` → `Agent.ask`  
- 执行前可把 `constrains` 合并进节点（Planner 应已做；Executor 再 assert 一次）

### 7.3 不变

- Skill / prompt_router / Docker / Verifier / ToolGateway 入口语义不变  

---

## 8. 文件布局

```text
agent_runtime/intent/
  __init__.py
  models.py           # IntentNode/Edge/Graph/Result/RouteContext
  segmenter.py
  rules.py
  embed_index.py
  prototypes.yaml
  llm_fallback.py     # maybe_refine → 图 + candidates 旁路
  planner.py
  graph.py
  router.py
  confidence.py       # fuse / graph confidence / intents snapshot
  clarify.py          # clarify-only 策略与 ClarifyPayload
  dialogue.py         # history-first 指代 + 薄投影
  candidates.py       # 候选事件 / 意图卡 / --from-eval
  stack_parse.py      # 堆栈优先槽位
  observability.py    # Prometheus
  eval_metrics.py     # 离线分层评测 CLI
  executor.py         # 拓扑串行
  eval_cases.yaml
  eval_cases_realistic_stacks.yaml
  eval_cases_enterprise.yaml
  eval_cases_realistic_users.yaml   # 流量分布模拟
  eval_cases_heldout_gaps.yaml      # 故意不过拟合压力集
  adapters/
    repair_plan.py
```

产品抽象说明：`docs/INTENT_USER_RECOGNITION.md`。

---

## 9. 可观测

| 事件/字段 | 内容 |
|-----------|------|
| `intent_routed` | mode, primary, action, confidence, parser, nodes[], edges[], breakdown, intents[], anaphora, llm_candidates, clarify_reason, allow_execute |
| Prometheus | routed / clarify{reason} / anaphora{outcome} / candidate{key,source} / latency / confidence buckets |
| CLI | `意图: …`；澄清问题；`/candidates` 聚合卡 |
| 候选 JSONL | `{cwd}/.agent/intent_candidates.jsonl`（可选 `candidate_root`） |
| Offline | `python -m agent_runtime.intent.eval_metrics`；`python -m agent_runtime.intent.candidates --from-eval` |

澄清 reason 稳定标签：`low_conf | no_hit | ambiguous | conflict | empty | below_tau_exec | unresolved_anaphora`。

---

## 10. 测试与评测

### 10.1 单测（相关）

| 主题 | 期望 |
|------|------|
| 单句 TypeError / hybrid / multi remember→repair | 与金样一致 |
| 同句双意图、修+重构冲突 | multi 或 clarify(conflict) |
| 指代「修一下/刚才那个」+ history | resolved → repair/explain |
| 无上下文指代 | unresolved_anaphora clarify |
| LLM refine + candidates | 图修正且 `llm_candidates` / `llm_nominate` 事件 |
| 候选 `--from-eval` | 产出意图卡 |
| mock embed 冲突 / embed 不可用 | 规则仍可用 |
| L2 adapter / REPL 串行 | 回归绿 |

CI：**强制 mock embedding**（无真实模型）。

### 10.2 离线评测分层

| 集合 | 作用 |
|------|------|
| 回归金集 | 防改坏（分布内 misroute 作门禁） |
| realistic_users | 近似流量加权（stratum + weight） |
| heldout_gaps | 故意不过拟合；允许整体 misroute 非零 |

报告字段：`in_distribution_misroute_rate`、`heldout_gap_misroute_rate`、`weighted_misroute_rate`、`by_stratum`。  
**勿将分布内 0% 误述为线上能力。**

---

## 11. 分期与风险

| 阶段 | 交付 | 风险缓解 |
|------|------|----------|
| P0 | schema+三层+segmenter+planner+graph 校验+L2 折叠 | 金样例；repair 通道强制折叠 |
| P1 | REPL 拓扑串行 executor | fail-fast；τ_clarify；ask 双句合并 |
| **P1+（已落地）** | clarify-only、置信 breakdown、多轮指代、候选发现、LLM candidates、分层评测 | held-out 保持难；候选不自动入典 |

**明确不做：** 并行执行同层节点、跨节点补偿、用户在线无审自定义意图、通用对话平台。

---

## 12. Spec 自检

- [x] 多意图 / 约束消歧 / DAG 方案 A  
- [x] 明确不做并行 executor  
- [x] `IntentResult.primary` 兼容策略明确  
- [x] 边类型闭集、环与上限处理明确  
- [x] 验收用例含 multi vs hybrid  
- [x] clarify-only / 多轮 / 候选发现 / LLM candidates 写入本文档（§14–§17）  

---

## 13. 文档关系

| 文档 | 角色 |
|------|------|
| 本文 | Intent Router **设计 + 实现边界**（含后续增强） |
| `docs/INTENT_USER_RECOGNITION.md` | **产品/面试向**抽象链路（少代码细节） |
| `docs/superpowers/plans/2026-08-03-intent-router.md` | 原 P0/P1 执行计划 |

---

## 14. 置信度与 Clarify-only（已实现）

- 段级：`c_rule` / `c_embed` / `c_fuse`；图级：`c_graph` / `min_node_conf`  
- `raw_signals.intents[]`、`split_strategy`  
- REPL 触发澄清：`low_conf`、`no_hit`、`ambiguous`、`conflict`、`below_tau_exec`、空输入、指代失败等  
- `ClarifyPayload`：`reason`、`question`、`candidates[]`、`allow_execute=false`  
- **不**在低置信时自动 `fallback=ask` 继续执行  

---

## 15. 多轮对话状态（已实现）

- **历史优先：** `RouteContext.history` / `Agent.read_history()` 提供指代原文  
- **薄投影：** `session["intent_dialogue"]` 仅存 pending_clarify、上轮 primary/slots、referents  
- 改写：「修一下 / 刚才那个 / 继续」；澄清短答「修」+ 原文合并  
- 无法消解 → `unresolved_anaphora`  
- REPL intent 路径 `record` 用户话，避免多轮断层；`/reset` 清历史与投影  

---

## 16. 候选意图发现（已实现）

**目的：** 为 taxonomy 扩展提供评审输入，而非在线发明新 primary。

| 来源 | 说明 |
|------|------|
| `route_topk` / `clarify_residual` / `conflict` / `no_hit` | 路由与澄清残留 |
| `user_cancel` / `user_rephrase` / `clarify_choice` | REPL 行为代理 |
| `llm_nominate` | LLM 同次 `candidates[]` |
| `gold_mismatch` | `--from-eval` 时预测≠金标 |

聚合为 `CandidateIntentCard`（频次、例句、closest、severity）。  
CLI：`python -m agent_runtime.intent.candidates --from-eval`；REPL：`/candidates`。

补意图流程：**发现 → 人审意图卡 → 闭集 + 规则/原型 + 金集 → 再接 action**。

---

## 17. LLM 弱信号 + 候选（已实现）

弱置信 / 含糊图时可选调用 light client：

1. **主路径：** 修正 `IntentGraph`（闭集 primary）  
2. **旁路：** `candidates[]`（闭集优先；可提名新短名 + `merge_into`）  
3. **门控独立：** `need_clarify` 不替代 τ 与冲突策略  
4. **不入典：** 新标签只进候选事件  

接口：`maybe_refine` → `LlmRefineResult`；兼容 `maybe_refine_graph` 仅返回图。

---

## 18. 实现状态小结

| 能力 | 状态 |
|------|------|
| P0 L2 repair 折叠 | ✅ |
| P1 REPL 串行图执行 | ✅（企业动作多为 stub） |
| 16 类动作级 taxonomy | ✅ |
| 堆栈优先槽位 | ✅ |
| clarify-only + 置信 breakdown | ✅ |
| history-first 指代 | ✅ |
| 候选发现 + from-eval | ✅ |
| LLM 图修正 + candidates | ✅ |
| 分层 / held-out 评测 | ✅ |
| 并行 DAG / 在线自动加意图 | ❌ 非目标 |

---

*方案 A：规划就绪 IntentGraph + 串行执行；权威产品边界仍以 DESIGN §23 为准。后续增强以本文 §14–§18 与 `INTENT_USER_RECOGNITION.md` 为准。*
