# S5：主路径集成与面试说明验收

日期：2026-10-13。分支：`M1/D5/context-integration-interview-s5`，基于 S4 `cec524c`。本记录是本地开发验收，不包含提交、推送、PR 或全量测试。

## 完成内容

S1–S4 已有预算、来源提交、required context、压缩、委派和恢复接线。S5 修正最终选取审计和宿主约束镜像，补跨模块测试与面试说明；复用现有 journal、EvidenceLedger 和运行预算。

| 模块 | S5 改动 |
| --- | --- |
| `agent_runtime/context_manager.py` | 最终请求 manifest 增加单次/累计容量说明、constraint_refs、task_context_revision 和压缩诊断；历史按单位索引关联 Observation，按最终选取写 selected/dropped；封印历史引用核对 section hash |
| `agent_runtime/context_runtime.py` | checkpoint manifest 保留 constraint_refs、model_limits、history_compression，防止安全边界保存时丢失新诊断字段 |
| `src/repair/plan_binding.py` | sync 与宿主用户消息入口同步 RepairState 的约束镜像；成功提交后，即使后续解析阻断，镜像仍反映 journal 当前状态 |
| `tests/test_context_main_path.py` | 新增 8 个参数化展开用例，覆盖主请求、压缩、真实工具/pytest、预算恢复、委派更新和审计边界 |
| `docs/CONTEXT_MAIN_PATH_INTERVIEW.md` | 主路径时序、来源/权威区分、预算示例、压缩/委派/恢复取舍、90 秒介绍和三个口述案例 |

历史组装阶段不再把全部投影 Observation 预先标记为 selected。完整单位省略后仅保留实际选中引用；相同文本的两个单位使用索引区分来源。最终协议装箱省略整个历史段时，manifest 不再保留历史段的 Observation 引用。同一引用若仍通过源码或反馈进入请求，维持 selected，并从顶层 dropped 中排除。

model_limits 是本次 `prepare_request()` 的容量说明；实际消耗仍由 BudgetManager 记录。constraint_refs 包含各条约束最新版及撤销状态；decision_refs 继续绑定当前节点投影，不能解释为全部节点决策清单。projection_hash 是审计投影标识，没有修改 Provider prefix hash 或缓存键。

## 完成条件与验证映射

| 完成条件 | 验证 |
| --- | --- |
| 原始 Issue → Plan → 方案提取 → 读改测 → 反馈边界 | XML/native 主路径用例执行真实 read/patch 与宿主 pytest；相关 L2 回归覆盖失败验证、回滚、有界重规划和公共 repair 入口 |
| 当前要求与宿主状态、委派一致 | 通过 accept_user_message 新增约束及节点选择；新委派携带新版；旧完成任务回收返回 task_context_changed，无 findings；再次撤销后第二批旧结果失效 |
| 压缩与重启后仍使用 journal 权威 | 强制 L5 触发的主路径保存/关闭/重开；故意污染 RepairState 约束镜像及缓存目标，恢复仍保留 journal 目标、约束、决策链与 context_revision |
| 已确认副作用不重放 | 恢复采用已有 patch 和 verify 结果；operation/attempt 数量未增加，模型及提取入口无新增调用；原测试文件未变 |
| XML/native 消费最终请求 | 派发门禁捕获 request 与 manifest，核对 request_hash、状态版本及输入容量；native 与实际客户端 requests 相等，XML 正文存在于实际客户端 prompts |
| 状态/预算/压缩诊断可持久化 | create_checkpoint 保留新增 manifest 字段；同一账本包含 extraction/planning/summary/repair，恢复 snapshot 相等，已记录消耗不退款 |
| 最终证据选取可解释 | 重复历史文本分别关联旧/新 ID；装箱选新项；最终省略历史时清除引用；源码复用引用保持 selected；封印 hash 错误不能继承旧引用 |
| 必需输入超限可阻断 | required context 回归包含 XML/native 超大当前请求，Provider 零调用；最终协议回归覆盖可选整段省略 |

新增主路径用例把 L5 触发阈值降为 1，以确定性覆盖压缩接线，未修改生产阈值。模型语义候选由受控客户端提供；文件读改和宿主 pytest 真实执行。关闭/重新打开模拟恢复边界；已有 L2 回归另外覆盖子进程崩溃窗口。

主路径测试显式设输入上限 8000、窗口 16384、输出预留 2048、余量 256、调用上限 30，给累计输出/总量留出宽裕测试额度。面试文档另提供累计输入 100000、输出 20000、总量 120000 的示例；二者都不是某型号规格证明。

## 实际验证结果

- 新增 S5 用例共 8 项通过。首轮 3 项失败来自测试客户端路由和节点/字段前置条件，修正后按失败 nodeid 复验；原先 4 项已通过结果保留，后续新增异常镜像用例单独通过。
- 相关回归批次 119 项通过：包括 S5 宿主委派用例，以及 `test_history_summary.py`、`test_context_projection.py`、`test_context_assembly.py`、`test_required_long_task_context.py`、`test_task_extraction_l2.py`、`test_plan_l2_binding.py`。
- 合并去重后的本阶段验证范围为 **126 项**。没有重复运行已通过且未受影响的用例，没有运行全量测试。
- 修改 Python 文件 Ruff check 与 format check 通过；`git diff --check` 通过。

首批运行出现既有 `.pytest_cache` 写入警告；后续使用 `-p no:cacheprovider`，不改仓库缓存配置。所有测试使用同一 `python -m pytest`，临时仓库位于 `.tmp/pytest-s5-*`。

## 支持边界

没有改 Provider cache_control、prefix hash、摘要缓存 key/存储/失效/指标，没有新增 UI、LLM 框架或第二套任务状态。普通 L1、独立外围入口与 Plan repair 的覆盖区别见面试说明。

本阶段验证最终请求及状态/来源/版本/预算/收据契约，不能据此推断模型遵循约束、提取语义准确率、摘要无遗漏、线上修复率或缓存成本收益。selected/dropped 是投影候选诊断，不是 canonical 历史逐条覆盖率。恢复保证依赖已确认收据和现有对账；unknown 用量保守保留，普通模型调用仍依赖 checkpoint 安全边界保存，不承诺所有物理请求精确计数或所有工具通用 exactly-once。
