# 终端接口

| 操作 | 合约 |
| --- | --- |
| `/model [模型ID]` | 无参数查看活动模型、设置中的 `models` 和当前端点的内置候选，有参数切到同一服务的指定模型。 |
| `/effort [级别]` | 无参数显示可选值；`auto` 恢复适配器默认。CLI 可用 `--effort`，环境可用 `REASONING_EFFORT`。 |
| `/context` | 展示当前请求的估算分项，不调用模型。 |
| `/compact [重点]` | 成功时报告前后 token 估算及归档 ID。失败时保留原会话。 |
| `/plan`、`/auto` | 切换对应权限模式。退出计划时回到先前模式。 |
| `/permissions [模式]`、Shift+Tab | 显示或切换模式。命令行可用 `--permission-mode auto`。 |

`permissions.autoAllow` 接受精确命令规则，例如 `{"tool":"execute_command","cwd":".","argv":["python","-m","pytest","-q"]}`。这些规则只在自动模式生效。
