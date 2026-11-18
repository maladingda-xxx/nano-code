# 版本记录

本文件记录 Nano Code 的源码包版本、主要变更和已知限制。数据格式版本描述会话数据库与文件快照的布局。

## 数据格式

- 当前会话格式版本：`1`
- 兼容 v0.3 存档布局；本轮不提供自动迁移
- 可通过 `nano-code doctor` 的 `session_format` 字段或 `nano-code doctor --export` 查看

## 未发布

### 改进

- 终端配色统一为 midnight blue，补全菜单、底部模式行和模型、推理强度对话框同步调整
- 编辑文件后直接显示 diff：按文件显示 `Update`/`Create` 标题、`+N -M` 统计，以及整行背景、带行号和语法高亮的增删行
- `write_file` 返回改动 diff，与 `edit_file` 一致，便于模型和用户审阅
- `/diff` 使用同一套 diff 渲染

## 0.6.0 · 2026-11-18

### 新增

- 终端界面：多行输入、历史、补全、`@文件`、任务队列、取消、diff 与工具输出审阅
- 独立会话 UID、`/sessions`、`/resume`、`/rename`、`/fork`、`/export`
- 文件检查点：`/checkpoints`、`/restore`、`/undo`、`/recover`
- 权限模式：`default`、`acceptEdits`、`plan`、`auto`、`dontAsk`；`auto` 使用独立模型请求审查编辑和命令
- 项目约定：按目录加载 `CLAUDE.md` 和 `AGENTS.md`
- 扩展：Skills、只读子代理、Hooks、MCP（stdio/HTTP）
- 模型控制：`/model`、`/effort`、`/context`、`/compact`、`/cost`
- 模型适配：OpenAI 兼容 Chat Completions、Anthropic Messages
- 多入口：交互终端、`-p`、JSON/stream-json、JSON-RPC、Python SDK
- 配置与诊断：`setup`、`doctor`、`doctor --export`、`/init`、`/memory`
- Windows、Linux、macOS 的 Python 3.11/3.13 持续集成

### 改进

- 工具结果带状态、退出码、耗时和截断标记；验证结果区分通过、失败、未运行和过期
- 命令支持实时输出、可配置超时、进程树取消和异常退出恢复
- 上下文预算接入所有模型请求；压缩保留原始历史归档
- 模型回复、工具参数和结束原因有明确错误语义

### 修复

- 工具轮数用尽后要求模型直接作答，并识别无工具标记
- 上下文压缩不再把运行时提示当作新用户轮次
- CRLF 文件的编辑匹配和文件末尾换行处理

### 已知限制

- I06 的固定判定通过，但 ItsDangerous 2.2.0 旧压缩载荷兼容性复测失败；详见 `specs/001-preview-release-validation/results.md`
- Anthropic 真实服务验证和外部用户试用尚未完成
- 未覆盖 MCP OAuth、完整 hooks 事件集、Claude Code 设置格式、IDE/桌面/云端和系统沙箱
- 并行会话的独立文件修改需要 Git worktree

## 0.3.1 · 2026-11

- 结构化运行与验证状态，事件携带会话、轮次和工具调用标识
- 工具执行日志与异常退出恢复
- 命令实时输出、超时和进程树取消
- 上下文预算和工具结果裁剪
- 固定 10 项真实任务评测集

## 0.3.0 · 2026-11

- 独立执行循环，支持文本、工具和结束事件
- OpenAI 兼容 Chat Completions、流式响应、重试与降级
- 仓库搜索、文件读取、精确编辑、写入和命令执行
- SQLite 会话保存、恢复、撤销和检查点
- `nano-code -p` 和 `--json` 脚本入口

## 0.2.0 · 2026-11

- 流式输出、异常体系、会话持久化
- API 重试和基础工具调用

## 升级

```powershell
python -m pip install -e .\coding_agent
python -m nano_code --version
nano-code doctor
```

升级前关闭其他 Nano Code 进程，并备份项目根目录的 `.coding_agent_sessions.db`。该文件可能包含对话、工具输出和文件快照。清理和恢复步骤见 [终端指南](docs/terminal.md#升级和排查)。
