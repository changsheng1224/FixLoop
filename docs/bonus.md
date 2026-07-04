# FixLoop 项目级可改进与可额外实现功能探索

> 探索**仓库/工程/交付/运维**层面能力，与运行时、修复流水线、评测等专项 bonus **互补、不重复**。  
> 基线：`master` @ PR #83 · 双包结构 `agent_runtime` + `src` · `484 tests`。  
> 专项清单见：`bonus_layer1_plan.md` · `bonus_layer2_plan.md` · `bonus_m5-m6.md` · `bonus_m7-m8.md`（请自行筛选、去重）。

---

## 1. 包结构与发布 — `pyproject.toml` / 安装体验

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 统一 CLI 入口**：新增 `[project.scripts] fixloop = fixloop.cli:main`，一条命令切换 `agent`（L1）与 `repair`/`eval`（L2）子命令。
- **[P1] [C:⭐ I:⭐⭐⭐⭐] optional-deps 分层**：`[docker]`（docker-py）、`[eval]`（无额外依赖或 heavy 可选）、`[dev]` 保持；README 写清最小安装 vs 全功能安装。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 双 namespace 包声明**：`setuptools` 显式列出 `agent_runtime` + `src`（或重命名 `src`→`fixloop`），避免 editable install 漏模块。
- **[P2] [C:⭐ I:⭐⭐⭐] 版本与兼容性矩阵**：`fixloop.compat` 文档 + CI 跑 3.11/3.12；`requires-python` 与 ruff `target-version` 对齐说明。
- **[P3] [C:⭐⭐ I:⭐⭐] PyPI 发布流水线**：tag 触发 build wheel + GitHub Release 附件，仍可选「仅 git 安装」。

---

## 2. 仓库治理与贡献 — 根目录 / `.github/`

- **[P1] [C:⭐ I:⭐⭐⭐⭐] CONTRIBUTING.md**：分支命名（M/D/task）、PR 模板、本地 `pytest`/`ruff` 最小集、代理与 `gh` 说明（对齐 CLAUDE.md）。
- **[P1] [C:⭐ I:⭐⭐⭐] CODEOWNERS**：`agent_runtime/` 与 `src/` 分 owner，refactor PR 必审边界文件。
- **[P2] [C:⭐ I:⭐⭐⭐] CHANGELOG.md 或 Release 自动生成**：从 squash merge 标题聚合版本说明，与 ADR 编号互链。
- **[P2] [C:⭐ I:⭐⭐] RFC 轻量流程**：架构变更先开 `docs/rfc/` 草案再 PR，与 ADR 并存（ADR=已决策，RFC=讨论中）。
- **[P3] [C:⭐ I:⭐⭐] Issue 模板**：bug / feature / eval-regression 三类表单，减少复现信息缺失。

---

## 3. 开发者体验 — 本地一键环境

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Makefile 或 justfile**：`make test` / `lint` / `eval-smoke` / `demo-repair` 统一入口，替代记忆多条 pytest 路径。
- **[P1] [C:⭐⭐ I:⭐⭐⭐] devcontainer / docker-compose**：VS Code Dev Container 含 Python 3.11 + Docker-in-Docker + 预 build 沙箱镜像。
- **[P2] [C:⭐ I:⭐⭐⭐] `.env.example` 全量注释**：L1 provider key、L2 repair、eval fake 开关、代理变量一份模板。
- **[P2] [C:⭐ I:⭐⭐] pre-commit 可选配置**：ruff format/check + 禁止 `.env` 入库，不强制绑定 CI 以外开发者。
- **[P2] [C:⭐ I:⭐⭐⭐] 新贡献者引导脚本**：`scripts/onboard.sh` 检查 Python、Docker、rg、git、代理，输出下一步命令。

---

## 4. 跨平台与 Shell — Windows / POSIX

- **[P1] [C:⭐ I:⭐⭐⭐⭐] demo 脚本 POSIX 化**：`demo/*.sh` 提供 PowerShell 对等脚本或文档说明 Git Bash/WSL2 路径；避免 `\` 与 `$?` 语义差异。
- **[P1] [C:⭐ I:⭐⭐⭐] 路径规范审计**：全库 grep `Path.cwd()` vs `repo_root`；Windows 下 temp eval 目录与 Docker 卷挂载路径一致。
- **[P2] [C:⭐ I:⭐⭐⭐] Docker Desktop 文档**：Windows/macOS 安装、镜像 build、WSL2 后端注意事项单独一节。
- **[P3] [C:⭐⭐ I:⭐⭐] 非 UTF-8 控制台**：CLI 输出 encoding fallback，避免中文 status 乱码（尤其 Windows cp936）。

---

## 5. 配置分层 — 全局 `fixloop.yaml`

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 项目级配置文件**：仓库根或 `~/.fixloop/config.yaml` 合并 provider、timeout、eval 默认路径；优先级 env > 项目 > 用户 > 默认。
- **[P2] [C:⭐ I:⭐⭐⭐] 配置校验**：启动时用 pydantic 模型校验 yaml，错误一次性列出字段路径。
- **[P2] [C:⭐ I:⭐⭐] 配置打印**：`fixloop config show`  redact 后 dump 生效配置，便于排障。
- **[P3] [C:⭐⭐ I:⭐⭐] 多 profile**：`dev` / `ci` / `prod` 预设写入 yaml，CLI `--profile` 切换。

---

## 6. 插件与扩展点 — 第三方能力接入

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] entry_points 插件**：`fixloop.tools` / `fixloop.verify_strategies` / `fixloop.orchestrator_variants` 注册表，pip install 外挂包即可扩展。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Skill 包目录**：除 `src/skills/` 外支持 `~/.fixloop/skills/` 与用户 yaml 覆盖，不改仓库即可试 prompt 策略。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 自定义 Case 目录**：eval `--cases-dir` 默认不变，文档说明如何挂载外部 Case 库做私有评测。
- **[P3] [C:⭐⭐⭐ I:⭐⭐] Webhook 钩子**：repair 结束 POST 摘要到 Slack/飞书（opt-in），与 Layer 业务逻辑解耦。

---

## 7. 可观测性（项目级）— 日志 / 指标 / 追踪

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 统一 run_id**：L1 `ask` 与 L2 `repair` 共用 UUID，写入所有 log line 与 trace 文件名，便于 grep 一次会话。
- **[P1] [C:⭐⭐ I:⭐⭐⭐] 结构化日志**：可选 JSON log（`FIXLOOP_LOG=json`），字段含 layer/agent/status/duration_ms，供 Loki/ELK  ingestion。
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐] OpenTelemetry 可选集成**：span 覆盖 repair 各阶段与 docker exec，exporter 默认 off。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Prometheus metrics 端点**：`fixloop serve --metrics :9090` 暴露 repair 计数、latency histogram、token 累计（进程级）。
- **[P3] [C:⭐⭐⭐⭐ I:⭐⭐] 分布式追踪**：Multi-Agent 并行阶段 parent/child span 关联（仅在有 OTel 时启用）。

---

## 8. 产物与磁盘生命周期 — `.agent/` / `eval_results/`

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 统一 artifact 根目录策略**：文档定义 `.agent/runs`、`.agent/repairs`、`eval_results/` 保留策略与 gitignore 边界。
- **[P1] [C:⭐⭐ I:⭐⭐⭐] 全局清理命令**：`fixloop clean --runs --eval --older-than 7d`，避免磁盘被 trace 撑满。
- **[P2] [C:⭐ I:⭐⭐⭐] 磁盘配额软限制**：repair 前检查 repo 所在分区剩余空间，不足则 fail fast。
- **[P2] [C:⭐ I:⭐⭐] 敏感产物擦除**：clean 时可选 secure delete 含 patch 的 temp 目录（eval tmp）。

---

## 9. 安全与供应链 — 依赖与密钥

- **[P1] [C:⭐ I:⭐⭐⭐⭐] CI pip-audit / dependabot**：`test.yml` 增 dependency review 或 weekly audit job。
- **[P1] [C:⭐ I:⭐⭐⭐] gitleaks 或 trufflehog**：PR 扫描误提交 API key；与现有 `git ls-files` 敏感文件检查互补。
- **[P2] [C:⭐ I:⭐⭐⭐] THIRD_PARTY_NOTICES**：汇总 sentence-transformers 模型与 Docker 基础镜像许可证。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] SBOM 导出**：release 时 `cyclonedx-bom` 生成 SPDX/CycloneDX 附件。
- **[P3] [C:⭐⭐ I:⭐⭐] 签名发布**：wheel cosign 签名（仅当走 PyPI 发布时）。

---

## 10. 静态分析与类型 — 质量门禁之上

- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐] 渐进 mypy/pyright**：先对 `src/state.py`、`agent_runtime/config.py` 等核心 dataclass 开 strict，CI 非阻塞报告。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] import-linter 层规则**：自动化断言 `agent_runtime` 不 import `src`，`src/harness` 不 import `src/eval`。
- **[P2] [C:⭐ I:⭐⭐⭐] 复杂度预算**：ruff 或 xenon 对 `orchestrator.py`  cyclomatic 上限，防 God class 回潮。
- **[P3] [C:⭐⭐⭐ I:⭐⭐] mutation testing**：对 `patch_applier` / `output_parsers` 跑 mutmut 抽样，非全库。

---

## 11. 性能与 SLO — 非功能基准

- **[P1] [C:⭐⭐ I:⭐⭐⭐] 冷启动 benchmark**：测量 `import agent_runtime` + `import src.cli` 耗时，CI 记录回归（阈值告警非 fail）。
- **[P1] [C:⭐⭐ I:⭐⭐⭐] repair 微基准套件**：不含 LLM，纯 Python 测 `_parse_issue`、snapshot、apply_patch 的 P99，与 eval 解耦。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 端到端 SLO 文档**：定义 demo calculator repair P95 预算（含 Docker），README 写清硬件假设。
- **[P2] [C:⭐ I:⭐⭐] 内存峰值 profiling**：eval 并行 `workers=N` 前文档给出推荐 N 与 16GB 机器上限。

---

## 12. 测试策略（项目级）— 金字塔与契约

- **[P1] [C:⭐⭐ I:⭐⭐⭐] L1/L2 契约测试包**：`tests/contract/` 只测公开 API（bootstrap、repair 模块 export），refactor 内实现随意换。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 场景 fixture 库**：共享 `conftest` 工厂：临时 repo、FakeClient 预设序列、Docker skip marker 统一。
- **[P2] [C:⭐ I:⭐⭐⭐] 测试分层标记**：`@pytest.mark.unit` / `integration` / `docker` / `slow`，CI 默认只跑 unit+integration。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 混沌注入开关**：`FIXLOOP_CHAOS=1` 随机 API 超时/失败，测 Orchestrator 降级不崩溃。
- **[P3] [C:⭐⭐⭐ I:⭐⭐] 多进程 eval stress**：专门 job 测 report/jsonl 并发写无 corruption。

---

## 13. 文档与知识库 — 站点化

- **[P2] [C:⭐⭐ I:⭐⭐⭐] MkDocs 或 Sphinx 站点**：ARCHITECTURE、ADR、M* DAILY、bonus 系列纳入可搜索静态站。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] API 参考自动生成**：pydoc/markdown 从 docstring 导出 bootstrap、RepairState、EvalRunner 公开面。
- **[P2] [C:⭐ I:⭐⭐] 架构图即代码**：Mermaid 源文件进 repo，README 嵌入 generated PNG（CI 校验未漂移）。
- **[P3] [C:⭐⭐ I:⭐⭐] 交互式 Case 浏览器**：本地静态页浏览 `eval/cases` 矩阵与 expected_patch（不含自动跑 eval）。

---

## 14. 成本与配额治理 — 全项目 Token/API

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 全局 token 预算**：单次 repair / 单次 ablation 上限，超限 abort 并写 `agent_errors`（与 L1 quota 闸口互补）。
- **[P2] [C:⭐ I:⭐⭐⭐] eval 费用预估门禁**：ablation 启动前打印预估 USD，需 `--i-know-what-im-doing` 才跑 >N 次真实 API。
- **[P2] [C:⭐ I:⭐⭐] Provider 路由策略**：配置主/备 model fallback（如 DeepSeek 失败切备用），项目级而非单 Client 硬编码。
- **[P3] [C:⭐⭐ I:⭐⭐] 组织级 key 轮换文档**：多开发者共用 key 时的轮换与 audit 建议。

---

## 15. 国际化与可访问性 — CLI / 文档

- **[P2] [C:⭐⭐ I:⭐⭐⭐] CLI locale**：`FIXLOOP_LANG=en|zh` 控制 help 与错误信息；默认 zh 保持现状。
- **[P3] [C:⭐⭐ I:⭐⭐] Prompt locale 分离**：`prompts/en/*.txt` 与 `prompts/zh/*.txt`，factory 按 locale 加载。
- **[P3] [C:⭐ I:⭐⭐] 色盲友好 demo 输出**：demo_lib 不仅用颜色，失败/成功同时打印 `[OK]`/`[FAIL]` 前缀。

---

## 16. 多仓库与会话 — 产品化延伸

- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐] 多 repo  repair 队列**：CLI 接受多个 `--repo`，顺序或并行修复 monorepo 子包。
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 会话持久化**：`fixloop session` 列出未完成 repair，恢复 context 与 retry 计数（跨进程）。
- **[P3] [C:⭐⭐⭐⭐ I:⭐⭐] 远程 worker**：repair 步骤提交到队列（Redis），本地只做编排 — 仅当产品化时考虑。

---

## 17. 模板与脚手架 — 扩展评测/Skill 效率

- **[P2] [C:⭐⭐ I:⭐⭐⭐] cookiecutter Case 模板**：生成 `case_XXX` 目录结构与占位 pytest，减少手工 copy。
- **[P2] [C:⭐ I:⭐⭐] Skill 模板生成器**：`fixloop skill new type_error` 输出 yaml 骨架与 trigger_pattern 示例。
- **[P3] [C:⭐ I:⭐⭐] Agent 角色模板**：新 role 时生成 factory 分支 + composite tools 清单 checklist。

---

## 18. 合规与隐私 — 日志与 trace

- **[P2] [C:⭐ I:⭐⭐⭐] 默认 redact 策略表**：文档列出哪些字段进 trace、哪些永不落盘（issue 全文 vs 摘要）。
- **[P2] [C:⭐ I:⭐⭐] 离线模式**：`FIXLOOP_OFFLINE=1` 禁用一切外网 API，仅允许 FakeClient + 本地 pytest，适合 air-gap 演示。
- **[P3] [C:⭐⭐ I:⭐⭐] 用户数据保留声明**：README 说明 `.agent/` 可能含代码片段，提交 issue 前需自行清理。

---

*文档版本：项目级 Bonus · 独立探索 · base `master` @ PR #83*
