# Nano Code 0.6.0 终端使用指南

Nano Code 是参考 Claude Code 交互设计的本地编程助手。它从项目目录启动，用你自己的模型服务处理任务。

## 安装和配置

在 Nano Code 仓库中执行以下命令。需要 Python 3.11 或更新版本，推荐安装 Git。

```powershell
python -m pip install -e .\coding_agent
python -m nano_code --version
nano-code setup
$env:API_KEY = '<你的模型 API Key>'
nano-code doctor
```

安装后 `nano` 与 `nano-code` 等价，可直接用 `nano` 启动；如果系统中已有 GNU nano 等同名命令，优先使用 `nano-code`。`setup` 保存 API 类型、地址和模型 ID。默认使用 DeepSeek 的 OpenAI 兼容接口。接入其他兼容服务时设置对应的 Base URL 和模型 ID。使用 Anthropic Messages API 时选择 `anthropic`，并设置 `ANTHROPIC_API_KEY` 或 `API_KEY`。如果 `nano-code` 不在 PATH，可以用安装时的同一个 `python` 运行 `python -m nano_code`。

如果 Windows 上的 `nano-code.exe` 仍从 `agent` 导入，或报找不到 `cancellation`，通常是旧的 0.3 可编辑安装仍然生效。用 `(Get-Command nano-code).Source` 检查启动器所在目录，在对应环境中执行 `python -m pip install -e .\coding_agent`，再执行 `nano-code --version`。例如启动器在 `D:\miniconda3\Scripts` 时，从 Conda `base` 环境安装，或明确使用 `& 'D:\miniconda3\python.exe' -m pip install -e .\coding_agent`。可编辑安装会继续读取仓库里的 `coding_agent/.env`。

模型配置按命令行参数、环境变量、显式 `--env-file`、用户配置文件和内置默认值选取。`~/.nano-code/settings.json` 保存用户设置，`~/.nano-code/.env` 可保存仅供本机使用的凭据。源码安装时继续读取原有 `coding_agent/.env`。受信任的项目可用 `.nano/settings.json` 和 `.nano/settings.local.json` 覆盖用户 JSON 配置。`NANO_CODE_HOME` 可指定其他配置目录。

## 日常使用

切换到自己的项目目录后运行 `nano-code`，或用 `nano-code --cwd <目录>` 指定项目。输入“修复这个失败测试，完成后运行测试并说明改了什么”开始一轮任务。

| 操作 | 用法 |
|---|---|
| 输入和补全 | Enter 发送，Alt+Enter 或行尾 `\` 后按 Enter 换行。上下键查看历史，`/` 补全命令，`@路径` 附带文件。含空格路径用 `@"目录/文件名"`。 |
| 运行中的控制 | 可以继续输入，消息按顺序执行。`/queue` 查看队列，`/clear-queue` 清空。Ctrl+C 或 `/cancel` 请求取消当前轮。模型请求等待服务商响应时，取消可能要等到响应或超时。 |
| 实时状态 | 小号 `composing···` 和药水瓶 `brewing···` 按轮交替，每轮固定一种动画。耗时从提交时开始。`↓` 只显示服务商已报告的输出 token，未报告时显示 `—`。等待授权时显示授权提示。 |
| 审阅 | `/status` 查看 Git 状态，`/diff` 查看工作区差异，`/diff --staged` 查看暂存差异。`/output` 展开最近一轮工具输出，Ctrl+O 切换展开模式。 |
| 会话 | `/sessions` 列表，`/resume <名称或 ID>` 恢复，`/rename` 重命名，`/fork` 复制对话。命令行支持 `--continue` 和 `--resume <ID>`。 |
| 恢复 | `/undo` 回退当前会话的最近一次内置文件编辑。`/checkpoints <轮次 ID>` 预览，`/restore <轮次 ID>` 恢复该轮次。`/recover` 列出异常中断的轮次。 |
| 模型 | `/model` 查看当前模型和候选项，`/model <ID>` 在当前服务内切换。输入 `/model ` 后可补全候选。用户设置中的 `models` 是可补全的模型 ID 列表。 |
| 上下文 | `/context` 分别显示消息、工具与协议开销、回复预留和剩余输入空间的 token 估算。`/compact [重点]` 生成摘要，报告压缩前后用量，并给出可用 `/resume` 打开的原对话归档 ID。`/cost` 查看服务端用量，`/export` 导出对话。 |
| 推理强度 | 直连官方 DeepSeek 且模型为 `deepseek-flash` 或 `deepseek-v4-pro` 时，`/effort` 显示级别，`/effort low`、`/effort high`、`/effort max` 切换。`/effort auto` 恢复适配器默认。启动时也可传 `--effort` 或设置 `REASONING_EFFORT`。 |
| 权限模式 | `/plan` 进入只读计划，再输入一次会返回先前模式。`/auto` 开关自动模式，`/permissions` 查看或指定模式。Shift+Tab 在默认、接受编辑、计划和自动模式之间循环。 |
| 项目约定 | 启动时读取 `CLAUDE.md` 和 `AGENTS.md`，访问不同子目录的文件时读取相应约定。`/init` 生成项目模板，`/memory` 维护项目记忆。 |

恢复时会检查文件是否仍与 Nano Code 当时写入的内容一致，发现冲突会保留当前文件。可恢复范围包括通过 `edit_file` 和 `write_file` 完成的修改。测试命令、MCP 工具和 hooks 造成的外部变更需要用 Git 或对应系统的记录审阅。对话分支共用项目文件。需要独立修改文件时，请从两个 Git worktree 分别启动。

## 权限和扩展

- `default` 允许仓库内读取。编辑、外部命令和 MCP 调用需要确认。可按 `y` 允许一次，或按 `s` 在当前会话允许完全相同的操作。
- `acceptEdits` 接受仓库范围内的内置文件编辑。外部命令和 MCP 调用仍有各自的授权检查。
- `plan` 允许只读调查，关闭编辑、外部命令、MCP 工具和可执行 hooks。工作区写入类命令如 `/undo`、`/restore <ID>` 和 `/export` 也需先退出计划模式。
- `auto` 自动允许工作区内置编辑和 `permissions.autoAllow` 中完全匹配工作目录和参数的命令。其他操作按现有授权流程处理。该模式使用本地规则。
- `dontAsk` 禁用交互授权。外部操作必须有显式允许规则。`deny` 优先于 `allow`。权限控制在工具层，已授权外部程序仍以当前用户权限运行。

可在用户或受信任的项目设置中配置确切的参数、目录和文件规则。例如 `{"permissions":{"allow":[{"tool":"execute_command","cwd":".","argv":["python","-m","pytest","-q"]}],"deny":[{"tool":"write_file","path":"deploy/*"}]}}`。项目中的可执行扩展和允许规则需要先使用 `/trust` 绑定当前配置，然后重启。内容修改后需要重新信任。

如需只在自动模式预先批准一条测试命令，可在 `permissions.autoAllow` 中加入 `{"tool":"execute_command","cwd":".","argv":["python","-m","pytest","-q"]}`。也可以用 `nano-code --permission-mode auto` 启动。

项目 `.nano/skills/<名称>/SKILL.md`、`.claude/skills/<名称>/SKILL.md` 或用户 `~/.nano-code/skills/<名称>/SKILL.md` 可以放可复用流程。`/skills` 查看，`/<名称> 参数` 手动调用。工具可按需加载允许模型调用的 Skill。子代理支持同样位置下的 `agents/<名称>.md`，它们用独立上下文做前台只读调查并返回结果。

MCP 配在 `mcpServers`。stdio 服务器用 `command`、`args` 和可选 `env`，HTTP 服务器用 `url` 和可选 `headers_env`（请求头名称映射到环境变量名）。使用 `/mcp connect <名称>` 连接，`/mcp disconnect <名称>` 断开。支持工具发现、调用、超时及失败提示。

Hooks 支持 `UserPromptSubmit`、`PreToolUse`、`PostToolUse` 和 `Stop`。每个事件配置一组对象，使用 `argv` 指定可执行程序和参数，可选 `matcher` 匹配工具名。Nano Code 向 stdin 写入事件 JSON。`PreToolUse` 非零退出会阻止该工具调用。

## 脚本和 SDK

```powershell
nano-code -p '解释项目入口' --output-format json
nano-code -p '修复失败测试并验证' --allow-edits --allow-command '["python","-m","pytest","-q"]' --json
nano-code --rpc
```

`--json` 输出逐行事件，`--output-format json` 输出一条最终结果。进程 exit code 表示模型轮次状态，测试状态读 `verification.status`。默认情况下脚本模式没有交互授权。

```python
from nano_code import Session

with Session(".", emit=print) as session:
    result = session.run("定位项目入口并解释启动流程")
    print(result["status"], result["verification"])
```

`--rpc` 每行接受一条 JSON-RPC 2.0 消息。先发送 `initialize`，再发 `run`，参数为 `{"prompt":"..."}`。可并行查询 `status` 或发送 `cancel`。`resume` 和 `fork` 使用 `session_id`，`shutdown` 结束服务端。事件作为 `event` 通知推送，协议版本为 `1`。

## 升级和排查

`nano-code doctor` 检查配置、解释器和工具链。原 SQLite 会话可继续通过 `/sessions` 或 `--continue` 使用。旧入口仍可用 `nano-code-legacy` 启动。升级前关闭其他 Nano Code 进程，并备份项目根目录的 `.coding_agent_sessions.db`。它可能包含原始对话、工具输出和文件快照，导出或共享前应审阅。

本版本覆盖本地 CLI 常用工作流。Claude Code 的 IDE、桌面、云端、Remote Control、账号体系、托管集成、插件市场、Agent Teams、系统沙箱和自动授权分类器仍有差异。MCP OAuth、完整 hooks 事件集和 Claude Code 的设置格式也尚未覆盖。

参考 Anthropic 官方 [交互模式](https://code.claude.com/docs/en/interactive-mode)、[CLI 参数](https://code.claude.com/docs/en/cli-reference)、[检查点](https://code.claude.com/docs/en/checkpointing)、[权限](https://code.claude.com/docs/en/permissions)、[Skills](https://code.claude.com/docs/en/skills)、[MCP](https://code.claude.com/docs/en/mcp)、[hooks](https://code.claude.com/docs/en/hooks) 和 [子代理](https://code.claude.com/docs/en/sub-agents)。
