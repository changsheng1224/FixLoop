## Claude Code Platform Notes

### Skill Invocation

Skills are invoked via the `Skill` tool with the skill name (no `superpowers:` prefix). Example: `Skill({skill: "brainstorming"})`.

### Subagent Dispatch

Claude Code supports subagents via the `Agent` tool. Use `subagent_type` to select agent types. Skills like `dispatching-parallel-agents` and `subagent-driven-development` work with the Agent tool.

When using subagent-driven-development:
- Dispatch implementers as agents with clear, self-contained prompts
- Use the Agent tool's `description` field to label each agent
- Agents return their final message as the result

### Worktrees

Claude Code supports git worktrees via `EnterWorktree` and `ExitWorktree` tools. The `using-git-worktrees` skill's workflow is compatible.

### Environment Detection

```bash
GIT_DIR=$(cd "$(git rev-parse --git-dir)" 2>/dev/null && pwd -P)
GIT_COMMON=$(cd "$(git rev-parse --git-common-dir)" 2>/dev/null && pwd -P)
BRANCH=$(git branch --show-current)
```

- `GIT_DIR != GIT_COMMON` → already in a linked worktree
- `BRANCH` empty → detached HEAD
