# 验证步骤

在配置了两个模型 ID 的项目启动 `nano-code`，输入 `/model` 查看候选。对支持的模型使用 `/effort high` 后发起新任务，核对强度应用于请求。使用 `/context` 查看估算分项。对长会话执行 `/compact 保留未完成事项`，用归档 ID 恢复原对话。

从自动模式运行 `/plan`，尝试写入或执行命令，再运行 `/plan` 恢复自动模式。配置一条精确测试命令，在默认模式确认它仍会询问授权，切换到自动模式验证这条命令按规则自动执行。用不同参数复核审批仍有效。

```powershell
python -m pytest coding_agent/tests -q
```

## 本轮验证记录

- Windows Python 3.11 的 76 项回归通过，覆盖完整模式循环、自动规则、模型与预算命令，以及压缩成功、无收益、取消和事务回滚。
- `deepseek-flash` 使用 `--effort low` 完成一次真实请求，事件记录在 `.cache/nano-eval/daily-controls-3b2v6j4p/`。
- `/compact 保留未运行的测试` 使用 `deepseek-flash` 完成一次真实摘要。上下文估算从 6,147 降至 1,758 tokens，原始归档内容逐项与压缩前一致。报告位于 `.cache/nano-eval/daily-compact-lcdwq7qr/`。
