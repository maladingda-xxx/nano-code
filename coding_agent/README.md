# Nano Code

Nano Code 是一个 Python 终端编程助手。它通过 OpenAI 兼容 API 或 Anthropic Messages API 调用模型，在仓库里搜索和阅读代码、局部编辑文件、运行获授权的测试命令，并展示实际差异和退出码。

## 安装和配置

需要 Python 3.11+。安装后，从要操作的仓库运行 `nano-code`。

```sh
cd coding_agent
python -m pip install .
```

在交互终端输入 `/login`，先选浏览器账号登录或 API Key / Token，再搜索供应商。API 凭据在隐藏输入框中填写。浏览器登录会打开 OpenRouter 授权页面。登录后输入 `/model`，按 `/` 搜索供应商或模型，按 Enter 应用。也可输入 `/model openai:gpt-5.4-mini` 直接切换。

API Key / Token 支持 DeepSeek、OpenAI、Anthropic Claude、Google Gemini、OpenRouter、阿里云百炼 Qwen、月之暗面 Kimi、智谱 GLM、MiniMax、xAI Grok、Groq、Mistral 和 Azure OpenAI。Azure 使用资源地址和模型部署名。模型可用性取决于账号和地区。

登录凭据以明文文件保存在本机用户目录 `~/.nano-code/nano-provider-credentials/`。若设置了 `NANO_CODE_HOME`，则保存在对应目录下。重启后登录仍可使用，模型需在当前进程中选择。

需要使用旧版或脚本配置时，将 [.env.example](.env.example) 复制到自己管理的位置并填入 `API_KEY`。官方 DeepSeek 默认使用 `deepseek-flash`，工具调用采用非思考模式。可用 `MODEL_NAME` 和 `BASE_URL` 指定其他兼容服务。已有环境变量优先于配置文件。 [DeepSeek 模型与工具调用](https://api-docs.deepseek.com/guides/tool_calls/)

```sh
nano-code --env-file /path/to/nano.env
```

也可直接设置环境变量后运行 `nano-code`。从源码启动时仍支持在 `agent.py` 旁放置 `.env`，然后运行 `python agent.py`。环境变量模板列出了流式输出、工具轮数、上下文和 API 重试选项。

## 在仓库里使用

启动后可以输入“找到失败的测试，做最小修改，再运行这个测试”。Nano Code 会读取从仓库根目录到启动目录的 `AGENTS.md`，并以仓库根目录解释文件工具中的相对路径。无 Git 仓库时以启动目录为工作区。

| 工具 | 用途 |
|---|---|
| `search_workspace` | 在工作区搜索文本，显示路径和行号。Git 仓库中的未跟踪文件遵循忽略规则。 |
| `read_file`、`list_directory`、`search_in_file` | 查看文件、目录及匹配上下文。读文件和单文件搜索支持 `offset`、`limit` 分页。 |
| `edit_file` | 替换一处精确匹配的原文，返回 diff。原文不存在或有多处匹配时会拒绝编辑。 |
| `write_file` | 创建、覆盖或追加工作区文件。 |
| `execute_command` | 用参数数组执行单条命令，报告退出码、标准输出和错误输出。 |
| `ask_followup_question` | 在交互模式下向用户提问。 |
| `read_tool_output` | 按裁剪提示中的引用分页读取当前会话记录中的工具输出，每页最多 4,000 字符。 |

交互模式中，同一轮内相同目录和参数的命令共享一次授权。授权后的程序拥有当前用户的进程权限。文件工具限制在工作区，命令的工作目录也在工作区内。

长命令运行时会显示程序已经刷新的 stdout 和 stderr。通过 `COMMAND_TIMEOUT_SECONDS` 可以设置命令默认超时和上限，默认 60 秒。工具请求可用 `timeout_seconds` 选择更短的时间。命令超时或按 `Ctrl+C` 时，Nano Code 会结束受管的进程树并保留已收到的输出。超时和取消的验证结果会标为不确定。

每轮结束时显示最近一次验证的结果，以及本轮通过内置文件工具成功编辑或写入的文件。模型通过 `execute_command` 的 `verification: true` 标记测试或构建等验证命令，Nano Code 根据实际退出码判断通过或失败。验证后成功调用内置文件编辑或写入时，需要重新验证。

工具轮数只剩两轮时，Nano Code 会向模型追加一条运行时提示，要求优先提交最小修改或给出当前结论；该提示不写入会话存档。

## 上下文管理

交互、脚本、SDK 和 RPC 共用请求上下文。每次请求估算消息、工具定义和协议开销，为模型回复预留空间。安装可选依赖 `coding-agent[tiktoken]` 后，可对支持的 OpenAI 模型使用对应的本地 tokenizer，其余使用标明来源的 UTF-8 启发式估算。输入达到可用输入预算的 60% 时，优先缩减较旧的工具结果。达到 75% 时，可对已完成的历史生成工作摘要，每个用户轮次最多发起一次自动摘要请求。超过本地预算时继续裁剪完整轮次和工具批次，必要内容仍放不下时返回 `context_budget_exceeded`。

压缩视图保留当前用户原话、近期消息、配套工具调用和结构化工作摘要。摘要覆盖任务、约束、发现、修改、测试、决策、剩余工作、相关文件与风险。实际修改路径、最近验证结果和待核查操作来自本地执行记录，任务清单保留为模型登记的状态。摘要失败、缩减不足或写入失败时沿用已有视图。摘要请求计入 `MAX_MODEL_REQUESTS`。

恢复同一 UID 时，Nano Code 校验摘要对应的原始历史范围。用 `/export session.json` 导出原始消息，或在模型遇到裁剪提示时让它调用 `read_tool_output`。回读使用字符偏移和常见凭据遮盖。保存的工具原文受原有执行上限与文件分页限制，SQLite 及导出文件仍可能包含敏感文本。既有旧会话和以前生成的归档均可以按原 ID 打开。

项目记忆保存在 `.nano/MEMORY.md`，可用 `/memory <内容>` 追加。按当前任务词汇挑选少量 Markdown 章节，可在标题中标明 `[verified 2026-10-01]`、`[inferred]` 或 `[stale]`。前两类标签和日期随正文保留，显式过期章节跳过。

下列设置可写入 `~/.nano-code/settings.json` 或受信任的项目设置。

| 设置 | 默认值与含义 |
| --- | --- |
| `context_limit` | 本地上限 80,000 tokens，环境变量为 `CONTEXT_LIMIT_TOKENS`。有效上限还受已知模型窗口限制。 |
| `context_models` | 可按模型 ID 或 `供应商:模型 ID` 设置 `window` 和 `max_output`，例如 `{"deepseek:deepseek-flash":{"window":1000000,"max_output":8192}}`。 |
| `context_safety_tokens` | 安全余量 512 tokens，小窗口自动限制余量。 |
| `context_tool_result_chars` | 常规工具结果正文目标 3,600 字符，随工具类型和压力调整。 |
| `context_prune_at`、`context_compact_at` | 分别为 `0.60` 和 `0.75`，按可用输入空间计算。 |
| `context_auto_compact` | 默认开启。可设为 `false`，仍可手动压缩。 |
| `anthropic_prompt_cache` | 默认关闭。开启后，Anthropic 官方 HTTPS 直连为系统提示及其前面的工具定义声明临时缓存。 |

OpenAI 和 DeepSeek 的隐式缓存受供应商条件与重复前缀影响。Nano Code 保持工具定义顺序和系统提示前缀稳定，并汇总实际返回的缓存读取、缓存写入和推理用量。缓存命中仍占逻辑输入窗口。服务端没返回的细分字段省略。旧版 `nano-code-legacy` 入口保留请求级预算及裁剪行为。

## 会话与恢复

在模型或工具运行期间按 `Ctrl+C` 可以结束本轮并回到输入框。会话保存在仓库根目录的 `.coding_agent_sessions.db`，下次启动可以选择恢复。SQLite 中的文件快照可能包含源文件的旧内容。

会话有稳定的 UID，`/sessions` 会显示。自动恢复、`/save` 和 `/load` 沿用同一个 UID，因此重启后 `/undo` 仍能定位该会话的文件修改。`/clear` 会开始新的会话 UID；之前的修改仍可用 `/checkpoints` 和 `/restore` 处理。

工具请求在运行前写入 SQLite，运行结束后记录状态和退出码。进程强制退出后，启动时会列出未结束的轮次。使用 `/recover <轮次 ID>` 可以恢复对话及已有工具结果。执行结果未落盘的操作会标为待核查，同批剩余操作会标为未开始。恢复只读取已保存的记录，继续任务前应先核查工作区和外部命令的实际效果。

每次内置 `edit_file` 或 `write_file` 还会记录会话、轮次、工具调用和时间。`/checkpoints` 列出最近的修改轮次，`/checkpoints <轮次 ID>` 预览每个文件是否可恢复，`/restore <轮次 ID>` 预览并恢复。恢复只处理当前内容仍等于该轮结束状态的文件；文件被手动修改、删除或路径变化时会保留当前内容并报告冲突。恢复按文件进行，单个文件失败不会阻止其他文件。

| 命令 | 用途 |
|---|---|
| `/undo` | 撤销当前会话最近一次内置文件编辑或写入。若文件后来被改过，则保留当前内容。其他会话和命令执行产生的改动不在范围内。 |
| `/checkpoints`、`/checkpoints <轮次 ID>` | 列出最近的文件修改检查点，或预览指定轮次的文件状态。 |
| `/restore <轮次 ID>` | 预览并恢复指定轮次仍可恢复的文件；冲突文件保留当前内容。 |
| `/recover`、`/recover <轮次 ID>` | 列出未结束的轮次，或恢复指定轮次的对话。 |
| `/save <名称>`、`/load <名称>`、`/sessions` | 保存、恢复和列出会话；列表显示稳定会话 UID。 |
| `/context`、`/context stats` | 查看下次请求的估算预算、分项占用、阈值和压缩记录。统计显示本进程用量、裁剪次数与估算误差。 |
| `/compact [重点]` | 手动压缩可归纳的历史，显示压缩前后估算，并保留原始会话。 |
| `/clear`、`/stats`、`/help`、`/exit` | 清空上下文、查看统计和帮助、退出。 |

## 脚本模式

```sh
nano-code -p '列出这个仓库的测试入口'
nano-code -p '修复失败的测试并验证' --json --allow-command '["python","-m","pytest"]'
```

`--json` 每行输出一个事件，包括模型文本、工具请求、实时命令输出、工具结果和 `turn_end`。纯文本脚本模式将实时命令输出发到 stderr，模型最终回复发到 stdout。默认拒绝运行外部命令，可重复使用 `--allow-command`，按仓库根目录与完整参数数组精确授权。命令参数数组要作为一个完整参数传入。

单次命令也记录工具执行过程。发现未结束的轮次时，`--json` 通过 `recovery_pending` 提供轮次 ID 和工具状态，当前请求仍从自己的提示开始。可切换到交互模式恢复旧轮次。

运行事件带有 `session_id` 和 `turn_id`，工具事件还带有 `round` 和 `tool_call_id`。会话标识用于关联当前运行中的事件，重新启动并恢复存档时会生成新的标识。

| 事件 | 字段 |
| --- | --- |
| `model_request` | `number` 为进程内累计模型请求数，`attempt` 为本次模型调用的尝试序号 |
| `context_pruning`、`context_compaction` | 工具输出裁剪数量、摘要触发方式、结果和前后估算 |
| `usage` | 服务端报告的增量用量。按供应商实际字段提供缓存与推理统计 |
| `tool_output` | `stream` 为 `stdout` 或 `stderr`，`text` 是刚收到的输出片段 |
| `tool_result` | `result` 提供易读文本，`outcome` 提供工具状态、退出码、stdout、stderr、耗时毫秒数和截断标记 |
| `turn_end` | `status` 表示完成回复、调用出错或中断。 `reason` 提供具体结束原因，包括工具轮数耗尽和无工具降级。`changes` 提供本轮通过内置文件工具成功编辑或写入的文件及次数，`tool_calls` 为本轮工具调用数 |
| `turn_end.verification` | 记录本轮最近一次显式验证命令及状态。状态包括 `passed`、`failed`、`not_run`、`inconclusive` 和 `stale` |

退出码 `0` 表示模型完成了回复，`1` 表示模型请求失败或没有有效回复，`2` 表示参数或配置错误，`130` 表示中断。脚本可读取 `turn_end.verification` 判断最近一次验证，并通过关联的工具事件查看完整结果。

可用 `MAX_MODEL_REQUESTS` 限制单个 Nano Code 进程的模型请求数。重试和降级计入上限，耗尽时返回 `model_request_budget_exceeded`。 [固定任务集与首轮联调](../evals/README.md)

流式文本在接收过程中以 `text_delta` 输出，完整回复通过 `reply` 确认。截断、断流、缺少结束标记或工具调用结构不完整时，`error` 与 `turn_end.reason` 会给出原因，已显示的文本仅供预览。结构完整的工具调用若参数 JSON 有误，会返回 `invalid_arguments`，模型可以据此修正。请求本身失败时仍可尝试无工具模式，降级回复也需通过完整性检查。

## 开发

`agent_core.py` 负责执行循环并发布事件，`model_api.py` 适配模型响应，`agent.py` 负责终端交互和脚本输出。自定义 Python 入口可用 `tools.register_tool(name, handler, description, parameters)` 添加工具，或在调用 `run_turn` 时传入自己的事件监听器。

```sh
cd coding_agent
python -m pip install -e '.[dev]'
python -m pytest
```
