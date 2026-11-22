# Nano Code 0.6.0 终端使用指南

Nano Code 是参考 Claude Code 交互设计的本地编程助手。它从项目目录启动，用你自己的模型服务处理任务。

## 安装和配置

在 Nano Code 仓库中执行以下命令。需要 Python 3.11 或更新版本，推荐安装 Git。

```powershell
python -m pip install -e .\coding_agent
python -m nano_code --version
nano-code
```

安装后 `nano` 与 `nano-code` 等价。如果系统中已有 GNU nano 等同名命令，请使用 `nano-code`。如果 `nano-code` 不在 PATH，可以用安装时的同一个 `python` 运行 `python -m nano_code`。

首次启动后输入 `/login`。选择 API Key / Token 可配置 13 家供应商，选择浏览器账号登录可使用 OpenRouter。配置后输入 `/model`，选中对应模型再开始对话。登录按供应商保存，重启后可直接用 `/model` 重新选择。用 `/model legacy:deepseek-flash` 可回到原有 DeepSeek 配置。 [供应商与配置详情](../coding_agent/README.md#安装和配置)

如果 Windows 上的 `nano-code.exe` 仍从 `agent` 导入，或报找不到 `cancellation`，通常是旧的 0.3 可编辑安装仍然生效。用 `(Get-Command nano-code).Source` 检查启动器所在目录，在对应环境中执行 `python -m pip install -e .\coding_agent`，再执行 `nano-code --version`。例如启动器在 `D:\miniconda3\Scripts` 时，从 Conda `base` 环境安装，或明确使用 `& 'D:\miniconda3\python.exe' -m pip install -e .\coding_agent`。可编辑安装会继续读取仓库里的 `coding_agent/.env`。

旧版和脚本模式的模型配置按命令行参数、环境变量、显式 `--env-file`、用户配置文件和内置默认值选取。`nano-code setup` 可保存 API 类型、地址和模型 ID。`~/.nano-code/settings.json` 保存用户设置，`~/.nano-code/.env` 可保存仅供本机使用的凭据。源码安装时继续读取原有 `coding_agent/.env`。受信任的项目可用 `.nano/settings.json` 和 `.nano/settings.local.json` 覆盖用户 JSON 配置。`NANO_CODE_HOME` 可指定其他配置目录。

## 日常使用

切换到自己的项目目录后运行 `nano-code`，或用 `nano-code --cwd <目录>` 指定项目。输入“修复这个失败测试，完成后运行测试并说明改了什么”开始一轮任务。

| 操作 | 用法 |
|---|---|
| 输入和补全 | Enter 发送，Alt+Enter 或行尾 `\` 后按 Enter 换行。上下键查看历史，`/` 补全命令，`@路径` 附带文件。含空格路径用 `@"目录/文件名"`。 |
| 运行中的控制 | 可以继续输入，消息按顺序执行。`/queue` 查看队列，`/clear-queue` 清空。Ctrl+C 或 `/cancel` 请求取消当前轮。模型请求等待服务商响应时，取消可能要等到响应或超时。 |
| 实时状态 | 小号 `composing···` 和药水瓶 `brewing···` 按轮交替，每轮固定一种动画。耗时从提交时开始。`↓` 只显示服务商已报告的输出 token，未报告时显示 `—`。等待授权时显示授权提示。 |
| 审阅 | `/status` 查看 Git 状态，`/diff` 查看工作区差异，`/diff --staged` 查看暂存差异。编辑文件后直接显示 `Update(路径) +N -M` 和整行背景色的改动块，超过 12 行时提示展开。`/output` 展开最近一轮工具输出，Ctrl+O 切换展开模式。 |
| 会话 | `/sessions` 列表，`/resume <名称或 ID>` 恢复，`/rename` 重命名，`/fork` 复制对话。命令行支持 `--continue` 和 `--resume <ID>`。 |
| 恢复 | `/undo` 回退当前会话的最近一次内置文件编辑。`/checkpoints <轮次 ID>` 预览，`/restore <轮次 ID>` 恢复该轮次。`/recover` 列出异常中断的轮次。 |
| 供应商登录 | `/login` 先选登录方式，再搜索供应商。使用隐藏输入框填写 API Key 或 Token，或在浏览器批准 OpenRouter 授权。Esc 或 Ctrl+C 可取消。 |
| 模型 | `/model` 显示已配置供应商和模型。按 `/` 搜索，↑/↓ 或数字选择，←/→ 调整支持的推理强度，Enter 应用，Esc 取消。也可用 `/model 供应商:模型ID` 切换。 |
| 上下文 | `/context` 查看模型、窗口来源、输入预算、回复预留、压力和最近压缩。`/context stats` 查看实际用量和估算误差。`/compact [重点]` 生成工作摘要。`/resume` 沿用原会话 ID，`/export` 导出原始对话。 |
| 推理强度 | `/effort` 按所选模型显示可用档位。用 `←/→` 调整，Enter 应用，也可输入 `/effort high` 等命令。`/effort auto` 恢复当前连接的默认行为，直连 Kimi K3 系列使用服务端默认档 `max`。启动时配置的模型也可用 `--effort` 或 `REASONING_EFFORT` 设置。 |
| 权限模式 | `/plan` 进入只读计划，再输入一次会返回先前模式。`/auto` 开关自动模式，`/permissions` 查看或指定模式。Shift+Tab 在默认、接受编辑、计划和自动模式之间循环，运行中也可以切换，新模式影响后续工具调用。底部状态行显示当前模式，例如 `⏵⏵ auto mode on (shift+tab to cycle)`；`dontAsk` 不参与循环，用 `/permissions` 切换。 |
| 项目约定 | 启动时读取 `CLAUDE.md` 和 `AGENTS.md`，访问不同子目录的文件时读取相应约定。`/init` 生成项目模板，`/memory` 维护项目记忆。 |

内置的 `edit_file` 和 `write_file` 执行后会在终端显示统一 diff：按文件分组的 `Update(路径)`（新建文件显示 `Create`）标题、`+新增 -删除` 行数，以及整行铺满终端宽度的绿色新增行和红色删除行背景。代码保留语法高亮，新增和删除行带新旧行号。默认最多显示 12 行，其余用 `/output` 展开；`/diff` 使用同一套渲染，按文件显示标题和统计。

编辑已有文件前，模型需要先调用 `read_file`；覆盖或追加已有文件需要完整读取（不指定部分行范围）。文件在读取后发生变化时，编辑会被拒绝并提示重新读取。同一用户轮次内，模型在后续模型轮次重复读取相同路径和范围时会返回“文件未变化”，不重复发送正文；同一条助手消息里的多次读取不会去重。换范围、换轮次或上下文压缩后重新读取会返回完整内容。读取状态只保存在当前进程内。

`/model` 会刷新已登录的 OpenRouter 和 Claude 模型目录，并读取模型公开的推理档位。目录暂时不可用时沿用已有数据。Kimi、DeepSeek、Gemini、Groq 等直连模型按已支持的系列识别档位。当前连接没有可识别的档位时，只使用 `auto`。

恢复时会检查文件是否仍与 Nano Code 当时写入的内容一致，发现冲突会保留当前文件。可恢复范围包括通过 `edit_file` 和 `write_file` 完成的修改。测试命令、MCP 工具和 hooks 造成的外部变更需要用 Git 或对应系统的记录审阅。对话分支共用项目文件。需要独立修改文件时，请从两个 Git worktree 分别启动。

## 权限和扩展

- `default` 允许仓库内读取。编辑、外部命令和 MCP 调用需要确认。可按 `y` 允许一次，或按 `s` 在当前会话允许完全相同的操作。
- `acceptEdits` 接受仓库范围内的内置文件编辑。外部命令和 MCP 调用仍有各自的授权检查。
- `plan` 允许只读调查，关闭编辑、外部命令、MCP 工具和可执行 hooks。工作区写入类命令如 `/undo`、`/restore <ID>` 和 `/export` 也需先退出计划模式。
- `auto` 用独立模型请求审查本轮任务提出的编辑和命令，给出允许或拒绝。只有允许时才执行，拒绝、超预算或无法取得有效结论时拦截。终端显示 `Auto 审查` 结果，自动审查计入当前模型的请求数和 token 用量。MCP 等其他外部工具需要已有的明确允许规则。
- `dontAsk` 禁用交互授权。外部操作必须有显式允许规则。`deny` 优先于 `allow`。权限控制在工具层，已授权外部程序仍以当前用户权限运行。

可在用户或受信任的项目设置中配置确切的参数、目录和文件规则。例如 `{"permissions":{"allow":[{"tool":"execute_command","cwd":".","argv":["python","-m","pytest","-q"]}],"deny":[{"tool":"write_file","path":"deploy/*"}]}}`。项目中的可执行扩展和允许规则需要先使用 `/trust` 绑定当前配置，然后重启。内容修改后需要重新信任。

可用 `nano-code --permission-mode auto` 启动，或在已有会话输入 `/auto`。自动审查可直接处理任务中的常规本地测试。对特定命令可在 `permissions.autoAllow` 中添加精确规则，例如 `{"tool":"execute_command","cwd":".","argv":["python","-m","pytest","-q"]}`，命中时直接放行。显式 `deny` 优先。

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

`nano-code doctor` 检查配置、解释器和工具链，输出当前版本、数据格式版本、模型、路径和检查结果。当前会话数据格式版本为 `1`。原 SQLite 会话可继续通过 `/sessions` 或 `--continue` 使用。旧入口仍可用 `nano-code-legacy` 启动。

诊断记录可以写成 JSON 便于排查：

```powershell
nano-code doctor --export .\diag.json
```

导出内容包含版本、数据格式版本、平台、模型、配置目录、会话数据库路径和检查结果。API Key、令牌、密码等字段在写入前会被遮蔽为 `[REDACTED]`，常见令牌前缀也会处理；分享前仍应自行审阅。

### 存储位置

| 位置 | 内容 |
|---|---|
| `~/.nano-code/settings.json` | 用户设置；`NANO_CODE_HOME` 可指定其他目录 |
| `~/.nano-code/.env` | 仅供本机使用的凭据 |
| `<项目>/.nano/settings.json`、`.nano/settings.local.json` | 受信任项目的设置覆盖 |
| `<项目>/.coding_agent_sessions.db` | 对话、工具输出、文件快照、检查点和撤销记录 |

源码安装时仍会读取 `coding_agent/.env`。项目设置中的可执行扩展和允许规则需要先 `/trust`。

### 备份和清理

清理前关闭其他 Nano Code 进程。需要保留历史时，先用 `/export` 导出对话，或复制 `.coding_agent_sessions.db`。

删除数据库会丢失 `/sessions`、`/resume`、`/checkpoints`、`/restore` 和 `/undo` 的记录，但不会修改或回滚工作区里的文件。文件名和路径变化后，旧检查点也会失去可恢复性。数据库可能包含原始对话、工具输出和文件快照，导出、备份或共享前应审阅。

重新运行 `/login` 可以更新该供应商的凭据。退出后删除 `~/.nano-code/nano-provider-credentials/` 下对应供应商的文件可以移除本机保存的登录。旧版凭据仍由 `~/.nano-code/.env` 或环境变量管理。

本版本覆盖本地 CLI 常用工作流。Claude Code 的 IDE、桌面、云端、Remote Control、账号体系、托管集成、插件市场、Agent Teams、系统沙箱和自动授权分类器仍有差异。MCP OAuth、完整 hooks 事件集和 Claude Code 的设置格式也尚未覆盖。

参考 Anthropic 官方 [交互模式](https://code.claude.com/docs/en/interactive-mode)、[CLI 参数](https://code.claude.com/docs/en/cli-reference)、[检查点](https://code.claude.com/docs/en/checkpointing)、[权限](https://code.claude.com/docs/en/permissions)、[Skills](https://code.claude.com/docs/en/skills)、[MCP](https://code.claude.com/docs/en/mcp)、[hooks](https://code.claude.com/docs/en/hooks) 和 [子代理](https://code.claude.com/docs/en/sub-agents)。
