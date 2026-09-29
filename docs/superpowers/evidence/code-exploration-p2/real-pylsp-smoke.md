# P2 Python LSP 本机验收记录

日期：2026-09-30。环境：Windows，Python 3.13.9（Anaconda），`pylsp` 1.13.1，Jedi 0.19.2。服务器来自已安装的 Anaconda 环境；本阶段没有安装新依赖。

运行 `python -m pytest -q tests/test_code_lsp_integration.py`：2 passed。测试实际启动 `pylsp`，查询后断言子进程退出。

实际查询结果（仓库相对路径、1 起始行列）：

| 查询 | 服务器结果 | 降级 |
| --- | --- | --- |
| `alias_reference/runner.py:5:12` 的定义 | `operations.py:1:5–10` | 无 |
| `alias_reference/operations.py:1:5` 的引用 | `runner.py:1:24–29` | 无 |
| `same_name/caller.py:5:12` 的定义 | 仅 `beta.py`，没有混入同名的 `alpha.py` | 无 |

本机冷启动实测约 6.6 秒，超过设计中的 5 秒初始化预算；当前初始化上限设为 8 秒，单请求保持 3 秒。这个结果只证明上述固定夹具中的语义查询与进程清理，尚不是模型修复效果对照。

运行普通 repair CLI 时可显式启用：

```powershell
python -m src.cli repair --repo <repo> --issue <issue> --code-exploration-mode lsp --pylsp-path <absolute-pylsp-exe>
```

默认模式为 `text`。服务器 argv 只能由受信任的用户配置或进程装配提供；仓库内配置不能开启 LSP。
