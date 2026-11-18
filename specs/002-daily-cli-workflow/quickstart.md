# 验证步骤

在配置了两个模型 ID 的项目启动 `nano-code`，输入 `/model` 查看候选。对支持的模型使用 `/effort high` 后发起新任务，核对强度应用于请求。使用 `/context` 查看估算分项。对长会话执行 `/compact 保留未完成事项`，用归档 ID 恢复原对话。

从自动模式运行 `/plan`，尝试写入或执行命令，再运行 `/plan` 恢复自动模式。在隔离测试项目中，直接要求执行常规测试，终端应显示审查放行和实际测试结果。检查缺少有效结论时没有执行记录。切换到默认模式后，工具授权仍由交互提示处理。

```powershell
python -m pytest coding_agent/tests -q
```

## 本轮验证记录

- Windows Python 3.11 的 76 项回归通过，覆盖完整模式循环、自动规则、模型与预算命令，以及压缩成功、无收益、取消和事务回滚。
- `deepseek-flash` 使用 `--effort low` 完成一次真实请求，事件记录在 `.cache/nano-eval/daily-controls-3b2v6j4p/`。
- `/compact 保留未运行的测试` 使用 `deepseek-flash` 完成一次真实摘要。上下文估算从 6,147 降至 1,758 tokens，原始归档内容逐项与压缩前一致。报告位于 `.cache/nano-eval/daily-compact-lcdwq7qr/`。
- Auto 分类审查新增后，Windows Python 3.11 的 82 项回归通过。无预配规则的真实 `deepseek-flash` 运行共 3 次请求，其中 1 次为自动审查。`pytest` 自动获准并通过，报告位于 `.cache/nano-eval/classified-auto-4g2zaltb/`。
- 本机 PATH 上的 `nano-code` 已从原安装更新为仓库源码安装。新开交互会话运行 `python --version` 时，终端显示 `Auto 审查已放行 · execute_command`，随后退出码为 0，未弹人工审批。
- D 盘仓库复核通过 82 项测试。Conda 与系统 Python 的可编辑安装均指向 `D:\Projects\nano-code\nano-code\coding_agent`。使用 Conda 的 `nano.exe` 和 D 盘配置启动后，`cmake --version` 自动审查放行，验证通过。报告位于 `.cache/nano-eval/conda-d-auto-j5m9ghpf/`。
