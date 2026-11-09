# Nano Code 固定任务集

[任务清单](tasks.json)固定了 Nano Code 和 ItsDangerous 2.2.0 的提交、10 个提示、验证用例及恢复步骤。四个故障用例在独立副本中注入可重复的回归，两个功能用例提供待通过的验收测试。任务 I01 是首轮联调使用的公开仓库只读任务。

## 离线检查

在安装了 Nano Code 开发依赖的 Python 环境中，从项目根目录运行下面的命令。首次检查会下载公开的 ItsDangerous 基线。

```powershell
python -m pip install -e ".\coding_agent[dev]"
python .\evals\run.py preflight
```

离线检查核对两项只读任务的源码证据，验证四项回归在注入前通过、注入后失败，验证两项新增功能的验收测试能检出缺失行为，并运行两项恢复相关回归。隔离副本位于 Git 忽略的 `.cache/nano-eval`。

## 首轮真实联调

将 `.env.example` 的配置复制到 `coding_agent/.env`，填写自己的 `API_KEY`、`MODEL_NAME` 和 `BASE_URL`。对于官方 DeepSeek，当前配置使用 `deepseek-flash` 和 `https://api.deepseek.com`。 [DeepSeek API 快速开始](https://api-docs.deepseek.com/)

```powershell
python .\evals\run.py live
```

这条命令执行一次 I01。请求总数最多 5 次，含重试与降级。使用非流式响应记录服务端返回的 token 用量。通过条件包括读取仓库证据、回答包含指定路径和符号、最终 Git 差异为空。

每次运行在 `.cache/nano-eval/I01-*` 保存结果、JSON 事件和最终 diff。报告包括目标提交、Agent 提交和工作区状态、模型、参数、提示、耗时、请求数、已报告的用量、人工介入次数、验证状态与退出原因。缺少 API 配置时会在创建运行报告或发送请求之前退出。

## 其余任务的验收

在新隔离副本中按任务清单应用对应故障或复制验收用例。使用 `python -B -m pytest -q -p no:cacheprovider` 加任务指定的用例路径，环境中的 `PYTHONPATH` 指向该仓库清单中的源码目录。验收时检查候选 diff、测试是否通过以及原有行为是否保留。恢复类任务还需按清单执行人工步骤并记录介入次数。

离线检查通过表示任务和判定条件可复现。真实模型任务成绩单独记录。
