# CLAUDE.md — MultiRepo Agent 项目约定

## 项目概述

从 Python 标准库零 LLM 框架依赖构建 Multi-Agent 代码修复系统。分两层：
- **Layer 1**：Agent 运行时内核（~1900行，M1-M4）
- **Layer 2**：多 Agent 修复系统（~1000行，M5-M8）

详细设计见 `docs/DEVELOPMENT_PLAN_ALL.md`，每日计划见 `docs/M1-M2-DAILY.md` 等；Layer 2 导读见 `docs/M5_GUIDE.md`、`docs/M6_GUIDE.md`（含 PR #65 E2E 加固总结）。

## 执行规范

执行每个任务前，必须先向用户说明：
1. **做什么**：该任务的目标和产出
2. **怎么实现**：涉及的模块、关键设计决策、预计代码行数

得到用户确认后，再真正执行。禁止直接动手。

默认直接实现用户要求的目标，不采用兼容式增强、兼容层或额外迁移逻辑；只有用户明确要求兼容性，或现有契约明确要求保持兼容时，才引入兼容方案。

## 代码修复能力改进原则

FixLoop 的代码修复能力改进必须面向**通用 repair runtime 能力**，不得演化成针对某个 SWE-bench Case、特定仓库或特定测试名称的规则集合。

允许并鼓励改进：

- 上下文组织、证据账本、工具预算、验证反馈、终态契约、失败归因、超时与取消、权限一致性等通用运行时能力。
- 将重复读取、无进展、输出截断、验证环境失败、权限冲突等问题抽象成通用失败类型或通用控制流。
- 让模型基于公开 Issue、当前源码、工具结果和验证反馈自行形成定位假设与补丁方案。

禁止的改进方式：

- 为单个 SWE-bench 实例、特定项目、特定文件名、特定测试名或特定错误文本写硬编码修复策略。
- 使用 Gold Patch、Gold Test Patch、`FAIL_TO_PASS` / `PASS_TO_PASS` 等答案性信息影响 Agent 的定位、提示、工具选择或补丁生成。
- 用规则替代模型做具体修复决策，例如直接指定应修改的目标函数、补丁内容或 Case 专属分支。
- 将评测数据集中的偶然模式固化为主流程逻辑，导致系统脱离真实通用代码修复场景。

判断标准：系统只负责提升过程质量和可诊断性；模型负责理解问题、提出假设并生成代码修改。任何新增逻辑都应能解释为通用修复运行时能力，而不是某个 Case 的答案提示。

## 测试规范

- **commit 到分支前**：仅需执行与改动相关的测试（如改了 `tools.py` 就跑 `tests/test_tools.py`）
- **全量测试**：除非用户显式要求，否则不运行全量测试 `pytest tests/ -v`；未获授权时不得声称全量测试通过
- **推 PR / PR 合并**：仅在用户显式要求发布级验证并授权全量测试时，执行全量测试；满足项目发布门禁时还需全量测试通过 + lint 零 warning

## Git 分支规范

每个 M 的每个 D 的每个任务使用独立分支，完成后提交 PR 合并回 `master`。

### 分支命名
```
M{m}/D{d}/{task-slug}
```
示例：`M1/D1/config-system`、`M1/D2/fake-client`、`M2/D7/tool-executor`

### 工作流
```
1. git checkout master && git pull
2. git checkout -b M{m}/D{d}/{task-slug}
3. [开发 + 测试]
4. git add -A && git commit -m "feat(M{m}D{d}): <任务描述>"
5. git push -u origin M{m}/D{d}/{task-slug}
6. gh pr create --base master --title "M{m}D{d}: <任务描述>" --body "..."
7. gh pr merge --squash --delete-branch
8. git checkout master && git pull
```

**⚠ 禁止直接 push master**：
- 小修改可在当前分支本地 commit，但**推送到远端前必须经用户同意**。
- 所有远端合并**必须走 PR 流程**（`gh pr create` + `gh pr merge`），不得直接 `git push origin master`。
- 若 `gh` 代理暂时不可用，向用户报告错误并等待指导，不可退化为本地合并+推送。

## 远程仓库

```bash
git remote -v  # origin git@github.com:changsheng1224/FixLoop.git
```

## 网络环境

GitHub HTTPS (443) 被 GFW 阻断，需通过代理访问。SSH (22) 直连正常。

```bash
# git 已全局配置代理
git config --global http.proxy http://127.0.0.1:7897
git config --global https.proxy http://127.0.0.1:7897

# gh CLI 需要在每次调用前设置环境变量
export HTTPS_PROXY=http://127.0.0.1:7897
export HTTP_PROXY=http://127.0.0.1:7897
export PATH="$PATH:/c/Program Files/GitHub CLI"
```

> 注意：`gh` 命令（pr create / pr merge 等）需要 `HTTPS_PROXY` 环境变量，否则会连接超时。
