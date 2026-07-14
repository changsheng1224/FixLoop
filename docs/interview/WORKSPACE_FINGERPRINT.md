# Workspace Fingerprint & Prompt Cache 绑定

> 面试叙事：FixLoop 如何通过 content-hash 实现 prompt cache 的确定性失效——换一个文件就换一个 cache key，避免陈旧的缓存污染模型输出。

## 概述

FixLoop 的 prompt cache 依赖两个核心机制：
1. **Workspace fingerprint** — 工作区语义快照的 SHA256 hash
2. **Prefix hashes** — system prompt + tools + workspace 的联合 hash

当 workspace 内容变化时，fingerprint 自动变更 → prefix hash 变更 → prompt cache key 变更 → 旧缓存自动失效。

## Workspace Fingerprint

**代码位置**: `agent_runtime/workspace.py:WorkspaceContext.fingerprint()`

```python
def fingerprint(self) -> str:
    payload = self._fingerprint_payload()
    canonical = json.dumps(payload, sort_keys=True, ...)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

**输入因子**:
| 因子 | 来源 | 说明 |
|---|---|---|
| `head` | `git rev-parse HEAD` | 当前 commit SHA |
| `dirty_file_hashes` | `git diff --name-only` + 文件内容 SHA256 | 未提交的文件变更 |
| `doc_contents` | 白名单文档（README.md, pyproject.toml, CLAUDE.md） | 项目说明文件全文 |

**噪声排除**: `git_status` 文本和 `recent_commits` 摘要不参与 fingerprint（两者随 git 操作频繁变化但不影响 workspace 语义）。

## Prefix Hashes

**代码位置**: `agent_runtime/prompt_prefix.py:build_prefix_hashes()`

```
prefix_hashes = {
    "cache_key": SHA256(stable_system + stable_tools + workspace_fingerprint),
    "system_hash": SHA256(stable_system_text),
    "tools_hash": SHA256(stable_tools_text),
    "workspace_hash": workspace_fingerprint,
}
```

- `stable_system_text` — persona + rules（只在代码更新时变化）
- `stable_tools_text` — 工具签名（只在工具注册变更时变化）
- `workspace_fingerprint` — 上述工作区指纹

## Cache Key 传播

```
WorkspaceContext.fingerprint()
  → build_prefix_hashes() → prefix_hashes["cache_key"]
    → ContextManager._base_metadata() → metadata["prompt_cache_key"]
      → AgentLoop._xml_call_model() → model_client.complete(prompt_cache_key=...)
        → DeepSeek/OpenAI API → prompt_cache_key 透传
```

## Workspace 切换检测

**代码位置**: `agent_runtime/runtime.py:Agent._detect_workspace_switch()`

每次 `Agent.ask()` 调用前检测 cwd 和 `workspace.fingerprint()` 是否变更：
- 变更 → 重建 WorkspaceContext + 重建 prefix → cache key 自动更新
- 不变 → 复用已有 prefix → cache key 不变

```python
current_hash = self.workspace.fingerprint()
if current == last_cwd and current_hash == last_hash:
    return  # 无变更
# 变更 → 重建
self.workspace = WorkspaceContext.build(current)
self._prefix = self._build_prefix(system_prompt)
```

## 面试要点

- **为什么不用 git status 文本做 fingerprint？** → git status 的文本顺序依赖 locale、文件系统排序，不稳定。而 HEAD + dirty file hashes 是确定性（deterministic）的。
- **为什么排除 recent_commits？** → 新增 commit 不改变 workspace 内容（只是 git 历史），不应让 cache 失效。
- **为什么 prompt cache 绑定 workspace？** → 工具列表、文件路径都依赖 workspace 快照。workspace 变了，prompt context 就变了，必须换 cache key。
- **与 Anthropic prompt caching 的关系** → FixLoop 的 prefix_hash 直接作为 Anthropic API 的 `prompt_cache_key` 透传，后端复用缓存时不会读到过期的 workspace snapshot。
