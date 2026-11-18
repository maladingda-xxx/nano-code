# 任务清单 · 日常终端开发工作流

## 权限模式

- [X] T001 [US3] 在 `coding_agent/nano_code/config.py` 和 `permissions.py` 接入 `auto` 与精确命令规则。
- [X] T002 [US3] 在 `runtime.py`、`commands.py` 和 `ui.py` 统一 `/plan`、`/auto`、`/permissions`、Shift+Tab，覆盖计划模式直接命令和运行中切换。

## 模型与上下文

- [X] T003 [US1] 在 `config.py`、`cli.py`、`runtime.py` 和 `ui.py` 实现候选模型、强度启动参数、参数补全，并保留请求计数。
- [X] T004 [US2] 在 `context_budget.py` 和 `commands.py` 显示共享的预算分项。
- [X] T005 [US2] 在 `runtime.py` 和 `sessions.py` 事务保存上下文压缩及全部原文，测试取消、保存失败和无收益摘要。

## 验证

- [X] T006 补齐 `coding_agent/tests/` 的命令和权限回归，更新 `docs/terminal.md` 并运行测试。

## Auto 分类审查

- [X] T007 [US3] 在 `coding_agent/nano_code/permissions.py` 和 `runtime.py` 接入独立审查，在 `cli.py` 和 `ui.py` 保留用户原始请求。验证明确允许、拒绝、异常和取消的路径。
- [X] T008 [US3] 从无预配规则的终端任务验证自动执行测试命令，记录模型审查、工具结果和提示行为，再补齐文档。

任务依序执行。结果见[验证步骤](quickstart.md#本轮验证记录)。
