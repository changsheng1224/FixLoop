# CLAUDE.md — MultiRepo Agent 项目约定

## 项目概述

从 Python 标准库零 LLM 框架依赖构建 Multi-Agent 代码修复系统。分两层：
- **Layer 1**：Agent 运行时内核（~1900行，M1-M4）
- **Layer 2**：多 Agent 修复系统（~1000行，M5-M8）

详细设计见 `docs/DEVELOPMENT_PLAN_ALL.md`，每日计划见 `docs/M1-M2-DAILY.md` 等。

## 执行规范

执行每个任务前，必须先向用户说明：
1. **做什么**：该任务的目标和产出
2. **怎么实现**：涉及的模块、关键设计决策、预计代码行数

得到用户确认后，再真正执行。禁止直接动手。

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

**⚠ 禁止本地 merge + push master**：不得使用 `git checkout master && git merge --no-ff ... && git push origin master` 绕过 PR 流程。若 `gh` 代理暂时不可用，向用户报告错误并等待指导，不可退化为本地合并。

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
