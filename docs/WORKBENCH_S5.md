# 工作台 S5：本地服务与交互界面

S5 为 S1–S4 增加可直接使用的浏览器工作台。页面和 JSON/SSE API 由同一个 Python
进程提供，前端使用本地 HTML、CSS、JavaScript 模块，无 CDN、Node 构建或独立前端服务。
安装包包含页面资源，开发和安装后的启动方式相同。

## 启动与登录

```powershell
python -m src.cli workbench --open-browser
# 等价：fixloop workbench --open-browser
# 无远端模型的演示：
python -m src.cli workbench --provider fake --open-browser
```

默认打开 `http://127.0.0.1:8765/`。可以省略 `--open-browser` 手动访问；`--port 0`
时使用终端输出的实际地址。Provider、模型、状态目录及修复验证层配置沿用
[S2](WORKBENCH_S2.md) 和 [S4](WORKBENCH_S4.md)。

初次打开，在登录页粘贴终端 `Bearer token file` 指向的 `api-token` 文件内容。
这是本地服务的访问令牌，与模型 API key 分开，不要粘贴模型凭据。
登录后无需在每次请求中重新输入，刷新页面也能继续使用；服务重启后重新登录。

页面通过 `POST /api/session` 使用 Bearer 令牌交换浏览器会话。服务签发随机的
HttpOnly、SameSite=Strict、Path=/api cookie，最长 12 小时有效，最多保留 64 个浏览器会话，
超过上限淘汰最早会话。cookie 只在当前服务实例有效；退出会撤销该浏览器会话。
令牌不放在 URL、localStorage 或页面源码中。localStorage 仅保存上次选择的会话 ID。

现有 Bearer API 继续可用；浏览器 cookie 的写请求额外要求 Origin 与请求 Host 完全一致。
静态页面可以在登录前加载，但仍检查本地 Host 和 Origin；项目数据与 API 需认证。
页面资源采用固定白名单、CSP、禁止内联脚本与禁止被嵌入其他页面，不提供目录浏览。

## 工作区布局

| 区域 | 操作 |
| --- | --- |
| 左侧会话列表 | 新建、搜索、选择会话，显示绑定项目，退出本地会话 |
| 中间对话 | 发送、停止、编辑重新执行、重新生成、从历史继续，复制回答与代码 |
| 右侧会话树 | 查看任意历史节点、切换执行分支、识别文件检查点和执行位置 |
| 右侧运行详情 | 模式、状态、验证计数、实际变化的文件路径 |
| 项目文件区域 | 查看本地修改状态，手动保存检查点，处理未完成的恢复事务 |

新建会话时，项目目录留空用于日常问答与写作；填写本机绝对目录后可选择项目问答或修复。
浏览器不能直接使用原生文件夹选择器，MVP 通过目录文本输入绑定项目。
一个项目可关联多个会话，任务在独立工作区并行运行；注册目录的应用与恢复串行。

普通聊天不向模型提供项目工具；项目问答只读；修复任务执行 Layer 2 流程并展示验证结果。
Enter 发送，Shift + Enter 换行，中文输入法组合输入期间不会误发送。
窄屏通过顶部按钮打开会话列表和会话树，支持键盘焦点及减少动画偏好。

## 历史、分支与文件

点击会话树节点只改变浏览位置。若浏览位置不是当前执行分支末端，输入框上方明确提示：
直接发送仍接在执行分支末端；“从这里继续”则创建新分支。

普通会话的编辑、重新生成和从节点继续直接创建分支，不覆盖原未来。
项目会话的这些操作，以及切换分支，必须经过恢复预览：

1. 编辑请求后先展示目标文件状态、路径变化、类型、大小、权限与摘要。
2. 检测到手动修改时，提示先保存当前状态到原分支。
3. 用户确认后才恢复文件并执行或切换。取消预览不改变文件或历史。
4. 预览后发生外部编辑或 revision 变化会拒绝应用，需重新生成预览。

编辑与重新生成恢复的是该轮执行前状态，“从节点继续”恢复该节点执行后状态。
原分支后续对话与文件状态保留，不自动重放。浏览与发送的详细语义见
[S3](WORKBENCH_S3.md) 和 [S4](WORKBENCH_S4.md)。

如果恢复中断，页面说明目录可能处于部分新文件、部分旧文件的混合状态，阻止继续执行并提供
“完成恢复”和“回到恢复前”入口。已持久化的恢复选择会显示，并禁用相反选择。
若执行资源停止状态未知，所有项目会话显示门禁及“重新检查并停止遗留执行”入口。
选择后仍由服务核对文件状态；未知外部修改不能强制覆盖。若操作返回错误，页面重新读取状态，
不自动重发写请求，避免把“响应失败”误判为“操作没有发生”。

## 运行与连接

普通聊天增量展示回答，项目模式展示阶段与工具进度，结束后显示完整答案。
断线只断开观察，不停止后台任务；页面带事件游标重连并去重，终态以 Run 快照为准。
用户切换会话也不会停止先前任务。停止必须点击“停止”，随后等待工作线程退出与文件保存。

收到终态后，界面等待最新会话树和 revision 同步完成，才解锁后续操作。
项目执行期间禁用文件恢复和相关历史变更；仍可读取进度与结果。
浏览器刷新或重新打开会恢复选择的会话、历史和进行中的任务。

内容使用有限 Markdown 渲染：标题、列表、引用、粗体、行内代码、代码块与 HTTP(S) 链接。
代码块和回答可复制。原始 HTML 作为文本显示，禁止脚本链接，不执行模型输出中的 HTML。
暂不提供语法高亮、Markdown 表格、图片附件、文件内容 diff 或嵌入式代码编辑器。
候选修复提供内容差异预览，用户点击“应用到项目”后才写入注册目录，也可以放弃候选。
其他历史恢复展示文件元数据差异，文件内容恢复由快照与步骤日志执行。

## 验证

本次在 Windows Python 与本机 Edge 上验证，没有使用真实远端模型或网络站点：

- `tests/test_workbench_ui.py`：页面与资源白名单、认证隔离、cookie 来源校验、退出、
  过期与重启失效、会话容量、恢复元数据不误报手动修改。
- `tests/test_workbench_http.py`、`tests/test_workbench_restore.py`：相关 API/SSE 与恢复回归。
- `tests/workbench_ui_units.mjs`：任意字节分块的 UTF-8/SSE、CRLF、心跳及分支祖先投影。
- `tests/workbench_ui_browser.mjs`：实际服务上的普通历史编辑、重新生成、分支继续、
  项目预览取消、手动修改保留、过期预览拒绝、失败文件状态、恢复中断入口、
  重连游标、停止、终态同步门禁、复制、重命名、登录失效和移动端安全渲染。
- 本地 wheel 构建与资源检查，确认安装包内含全部前端文件及启动入口。

```powershell
python -m pytest tests/test_workbench_ui.py tests/test_workbench_http.py tests/test_workbench_restore.py -q
node tests/workbench_ui_units.mjs
# 浏览器测试需要测试环境中的 playwright 包及已安装的浏览器：
$env:FIXLOOP_BROWSER_CHANNEL = 'msedge'
node tests/workbench_ui_browser.mjs
# 只复验某组：
$env:FIXLOOP_UI_CASES = 'project_restore'
node tests/workbench_ui_browser.mjs
```

浏览器测试可用 `FIXLOOP_PLAYWRIGHT_MODULE` 指向已有 Playwright 模块，
用 `FIXLOOP_TEST_PYTHON` 指定与项目依赖一致的解释器。未指定浏览器 channel 时使用
Playwright Chromium。Node 与 Playwright 仅用于开发验收，运行工作台不需要它们。
截图和测试状态保存在 `.runtime-tools/s5-browser-<时间戳>`。

按 CLAUDE.md 仅运行相关测试与 Ruff，未运行仓库全量测试。S5 是本地 Web 界面，
未提供 Electron/Tauri 安装器或原生窗口。修复使用已有 WSL/bwrap 后端；
当前执行、持久化及恢复边界见 [加固契约](WORKBENCH_HARDENING.md)。
