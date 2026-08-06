# Patcher Primary 面试演示脚本（带进度输出）

> 配合 `docs/PATCHER_PRIMARY_SWE_PLAN.md` §8。边跑边讲，避免黑屏。

## 环境

```powershell
$env:FIXLOOP_REPAIR_MODE = "patcher_primary"
$env:FIXLOOP_CRITIC_MODE = "rules_first"
$env:FIXLOOP_PROGRESS = "1"
$env:FIXLOOP_PROGRESS_STDOUT = "1"          # 默认：进度走 stdout（避免 Tee-Object 假错误）
$env:FIXLOOP_PROGRESS_JSONL = "artifacts/progress.jsonl"   # 可选
$env:FIXLOOP_PROGRESS_HEARTBEAT = "1"       # 心跳默认只写 jsonl；要刷屏设 HEARTBEAT_TEXT=1
$env:FIXLOOP_PROGRESS_HEARTBEAT_S = "60"
$env:FIXLOOP_SEMANTIC = "0"                 # primary 跑批建议关 embedding
$env:FIXLOOP_PATCHER_COMPACT = "1"
```

## 口述轨迹（对照 CLI `[progress]` 行）

1. **`repair_started` / `seed_ready`**
   规则种子（test_patch / F2P）→ `allowed_edit`；**无 Loc/Ret LLM**。

2. **`patcher_turn` / `tool_progress`**
   Patcher 同环：grep/read → **`apply_patch`**（须已读）→ 写后窗口 / lint。

3. **`quick_test`（工具）**
   优先 F2P nodeid；失败摘要进下一 turn（读结果再决策）。

4. **`critic_progress` / `critic_finished`**
   空/越锁/纯测试 → reject 回灌；accept → Verifier。

5. **`verify_progress`**
   沙箱权威判定；失败结构化回灌再修。

6. **`heartbeat` / `repair_finished`**
   长跑可见心跳；结束看 `status` / salvage 计数。

## 一句话架构

> 规则种子定位 → Patcher 搜读改测（`apply_patch` + ACI）→ Critic 廉价审 → Verifier 实测；运行中有阶段进度行，Trace / `progress.jsonl` 可复查。

## 非目标（勿演示）

- 完整 TUI / SSE Dashboard
- Localizer∥Retriever 双 LLM 交接
- 刷屏模型思维链
