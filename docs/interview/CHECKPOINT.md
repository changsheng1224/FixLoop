# FixLoop Checkpoint 触发点规范

## CheckpointTrigger

```python
CheckpointTrigger = Literal["step_end", "user_cancel", "ask_end"]
```

| trigger | 触发时机 | payload | resume 行为 |
|---|---|---|---|
| `step_end` | 每个工具执行成功后 | `last_tool` 含工具名 | 可 mid-loop resume（从最后成功步继续） |
| `user_cancel` | Ctrl+C / `/cancel` | `in_flight_tool` 含执行中工具 | 可恢复完整 session |
| `ask_end` | ask() 正常结束 | — | 仅 full-session resume |

## Resume 规则

- `--resume`: 仅当最后一个 checkpoint 的 `trigger=step_end` 时允许 mid-loop resume
- 其他 trigger: 恢复 session 后重头开始 ask()
- `trigger=user_cancel`: 恢复时跳过 in_flight_tool（未完成的操作）

## create_checkpoint 校验

```python
create_checkpoint(agent, ts, msg, trigger="step_end",
                  last_tool="read_file", in_flight_tool="")
# trigger 不在 VALID_TRIGGERS → ValueError
```
