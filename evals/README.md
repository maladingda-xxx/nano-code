# Nano Code 固定任务集

[任务清单](tasks.json)固定了 Nano Code 和 ItsDangerous 2.2.0 的提交、10 个提示、验证用例及恢复步骤。四个故障用例在独立副本中注入可重复的回归，两个功能用例提供待通过的验收测试。任务 I01 是首轮联调使用的公开仓库只读任务。

## 离线检查

在安装了 Nano Code 开发依赖的 Python 环境中，从项目根目录运行下面的命令。首次检查会下载公开的 ItsDangerous 基线。

```powershell
python -m pip install -e ".\coding_agent[dev]"
python .\evals\run.py preflight
```

离线检查核对两项只读任务的源码证据，验证四项回归在注入前通过、注入后失败，验证两项新增功能的验收测试能检出缺失行为，并运行两项恢复相关回归。隔离副本位于 Git 忽略的 `.cache/nano-eval`。

## 真实任务运行

将 `.env.example` 的配置复制到 `coding_agent/.env`，填写自己的 `API_KEY`、`MODEL_NAME` 和 `BASE_URL`。对于官方 DeepSeek，当前配置使用 `deepseek-flash` 和 `https://api.deepseek.com`。 [DeepSeek API 快速开始](https://api-docs.deepseek.com/)

```powershell
python .\evals\run.py live --task N02
python .\evals\run.py live --task N03
python .\evals\run.py verify --run N03-example
```

`--task` 接受 `I01`、`N02`、`N03`、`N04`、`I05`、`I06`、`N07`、`I08`。恢复任务 `N09`、`N10` 需要人工执行 [tasks.json](tasks.json) 中的 `procedure`，不进入自动运行列表。

运行流程：

- 只读任务：核对最终回复中的路径和符号，要求无 Git 差异、无误用写工具。
- 修复任务：先确认原始测试通过，再注入故障并确认失败；模型运行后，候选改动必须仍在允许修改的文件范围内。
- 功能任务：先确认验收测试失败；评测器把验收测试复制为 `nano_eval_test.py`，并记录其哈希，模型改写验收测试会导致本次运行失败。
- 修复和功能任务的候选先标为 `pending_verification`。检查 `seeded.diff`、`final.diff` 后运行 `verify --run <目录名>`，由评测器独立执行任务测试并写回最终结果。

### 运行参数

`I01` 保留首轮只读联调的预算：最多 5 次模型请求、4 个工具轮、12000 token 上下文。其余可自动任务使用产品默认的 8 个工具轮、最多 9 次请求和 80000 token 上下文。`N02` 沿用这组默认值。每次运行实际使用的参数记录在 `result.json` 的 `settings` 中。

每次运行在 `.cache/nano-eval/<任务>-*` 保存结果、JSON 事件、候选差异和测试输出。报告包括目标提交、Agent 提交和工作区状态、模型、参数、提示、耗时、请求数、已报告的用量、人工介入次数、验证状态与退出原因。缺少 API 配置时会在创建运行报告或发送请求之前退出。

## 2026-11-09 基线

| 任务 | 类型 | 结果 | 请求 | tokens | 报告目录 |
| --- | --- | --- | --- | --- | --- |
| I01 | 只读 | 通过 | 3 | 8,546 | `I01-_khp7uu1` |
| N02 | 只读 | 通过 | 7 | 36,529 | `N02-jxcgm2ny` |
| N03 | 修复 | 通过 | 7 | 40,569 | `N03-p30l_43t` |
| N04 | 修复 | 通过 | 8 | 39,735 | `N04-4_phb8uk` |
| I05 | 修复 | 通过 | 3 | 8,653 | `I05-bc57kdve` |
| I06 | 修复 | 通过 | 3 | 8,455 | `I06-sen5y6q4` |
| N07 | 功能 | 通过（验收测试 + 原有 35 项测试） | 6 | 33,957 | `N07-bzlxz0u9` |
| I08 | 功能 | 通过 | 3 | 9,678 | `I08-96sax603` |
| N09 | 恢复 | 通过（脚本化强杀 + `/recover`） | 2 | 3,834 | `N09-wdvr45by` |
| N10 | 恢复 | 通过（脚本化手工修改 + 两次 `/undo`） | 4 | 8,095 | `N10-8kno3p9w` |

8 个自动任务共 40 次模型请求、186,122 tokens；N09/N10 共 6 次请求、11,929 tokens。10 个任务全部通过。首轮运行在更早的工作区状态上得到 4 项通过、4 项失败（N03、N04、I06、I08），失败记录仍保留在 `.cache/nano-eval`。两轮之间新增了工具轮数提示、最小修改引导和失败编辑事实提示。

所有第二轮运行使用未提交的工作区，报告中的 `agent_tree_dirty` 为 `true`；恢复任务由脚本启动交互进程，报告中的 `scripted` 为 `true`。提交后应用相同参数重跑，才能得到干净提交上的基线；N09/N10 的真实人工体验验收仍未执行。

联调期间修复了三个执行循环问题：工具轮数用尽后，最终请求会明确要求模型直接作答，输出内部工具调用标记会被判为 `tool_markup_without_tools`；上下文压缩不再把运行时提示当作新的用户轮次；工具轮数只剩两轮时会提示模型优先提交最小修改。`turn_end` 现在同时报告本轮成功修改的文件和工具调用数，但模型回复中的“已修改”声明仍需以实际 diff 为准。

## 恢复任务

`N09`（强制退出后 `/recover`）和 `N10`（手工修改后 `/undo`）可按 [tasks.json](tasks.json) 的 `procedure` 人工执行，也可用驱动脚本复现同样的操作：

```powershell
python .\evals\recovery.py both
python .\evals\recovery.py N09
python .\evals\recovery.py N10
```

脚本在隔离副本中启动交互进程，N09 在观察到 README 写入后强制结束进程树并重启执行 `/recover`，N10 在模型写入后追加 `USER-AFTER`、执行两次 `/undo`。报告记录标记次数、写入工具状态、恢复状态、`file_edits` 行数和模型用量。脚本化验收证明流程可复现，但不替代真实用户对提示、等待时间和撤销结果的人工体验验收。

离线检查通过表示任务和判定条件可复现。真实模型任务成绩单独记录。
