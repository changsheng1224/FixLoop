# S1：统一预算与模型调用边界

本阶段覆盖 Plan 驱动 repair 主路径，保留 Layer 1 / Layer 2 分层。缓存策略、缓存键、缓存存储及缓存指标不在本次改动范围。

## 配置与口径

```json
{
  "prompt_budget": 8000,
  "hard_cap": 8000,
  "model_context_window": 16384,
  "max_new_tokens": 2048,
  "context_safety_margin": 256,
  "budget": {
    "prompt_tokens": 100000,
    "output_tokens": 20000,
    "total_tokens": 120000,
    "max_llm_calls": 30
  }
}
```

- 单次输入容量取 `prompt_budget`、`hard_cap`、模型窗口减本次输出预约和安全余量的最小值；工具 schema 与消息编码也计入最终输入检查。Plan 必需状态完整保留，装不下时明确阻断。
- `budget.prompt_tokens` 仅统计累计输入；输出、总 token 和调用次数独立限制。新增输出/总 token 限制默认 `0`，表示不限；模型窗口 `0` 表示未配置，不根据模型名称猜测容量。
- 新字段支持配置文件和 `FIXLOOP_MODEL_CONTEXT_WINDOW`、`FIXLOOP_CONTEXT_SAFETY_MARGIN`、`FIXLOOP_BUDGET_OUTPUT_TOKENS`、`FIXLOOP_BUDGET_TOTAL_TOKENS` 环境变量。

## 调用与结算

`agent_runtime/model_call_budget.py` 是统一派发门禁。一次派发原子预约调用次数、输入、输出及总 token。正常返回优先使用本次结构化 usage；文本客户端使用受锁保护的 session usage 增量，避免读到上一次请求的 `last_usage`。

有用量时按实际结算，包括超过预约或上限的实际用量；后续请求由账本阻断。正常返回却没有用量时保留预约并标记 `estimated`；异常、取消或超时保留 `unknown`。预约 scope 与调用目的进入预算快照，迟到的后台结果不能退款或改写恢复后的账本。

调用目的支持 `repair`、`planning`、`exploration`、`extraction`、`summary`。已接入 XML/native 主循环、XML 空响应重试、complete_once、任务规划/摘要、历史 summarizer、Plan 规划/重规划，以及 repair 前置意图分类。`extraction` 提供后续阶段的入口，本阶段没有增加自然语言提取器。

repair 入口在意图分类之前建立账本和 deadline，patcher/verifier 共用；checkpoint 保存全局快照。角色的旧 checkpoint 不能覆盖更新的全局预算或延长期限。普通独立 ask 使用独立运行预算；显式传入的父级预算保留。

探索子任务在父账本预留整个 scope，子账本限制内部派发，父级依据持久收据幂等结算。收据分开记录输入和输出；未知用量保留分项上界和总额度。取消后，未退出的后台调用继续占有原 read permit，避免提前释放并发槽。

## 验证与解释边界

相关测试覆盖原子拒绝且零派发、并发争抢、实际超预约、未知用量恢复、取消/超时后的迟到结果、辅助调用门禁、逐次重试计费、父子分项结算、模型窗口与必需状态阻断、配置加载、全局/角色 checkpoint 的优先级。已有上下文、压缩、探索、Plan 重规划、主循环、repair 恢复、工厂及 verifier 的相关回归已执行；未执行全量测试或真实 Provider 实验。

调用次数表示运行时逻辑派发；Provider 内部 HTTP 重试仍由原客户端负责，未暴露的物理请求次数和未知实际用量不能精确核算。输入使用已有 tokenizer/估算器，fallback 情况仍是估算；显式窗口和安全余量不代替 Provider 的最终容量校验。预算快照依赖现有 checkpoint 安全边界，不新增每次 HTTP 派发前的独立持久事务。

后续阶段继续处理可信消息、版本化约束/决策、自然语言提取、统一压缩结构；本阶段保留现有摘要格式与输出截断规则。
