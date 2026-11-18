# 执行方法

打开此仓库的 Codex 聊天，输入 `$speckit-implement` 续做 [任务清单](tasks.md)。新检出可设置 `SPECIFY_FEATURE_DIRECTORY` 为 `specs/001-preview-release-validation`，或将同一路径写入本机 `.specify/feature.json` 的 `feature_directory`。

Windows 的 exFAT 工作区上，Specify 1.4.0 的 `init` 和 `integration status` 会报 `WinError 87`。当前仓库的 Codex 技能和项目内 Python 脚本均已验证可用。若需运行集成管理命令，可使用 NTFS 上的项目副本。

在已安装开发依赖的环境中，从仓库根目录运行离线检查，再逐项运行评测。凭据沿用本机配置。

```powershell
python -m pytest coding_agent/tests -q
python evals/run.py preflight
python evals/run.py live --task I01
```

修复和功能任务先阅读报告目录的 `seeded.diff`（如存在）及 `final.diff`，再运行 `python evals/run.py verify --run <报告目录名>`。

使用 `python evals/recovery.py both` 执行脚本恢复。人工恢复按 [固定任务清单](../../evals/tasks.json) 的 N09、N10 操作，记录看到的提示并复核 README 内容。完整评测命令见 [评测说明](../../evals/README.md)。

## T009 · Anthropic 真实服务验证

在刚安装候选版本的 PowerShell 中运行。为本次验证单独准备本机 `.env`，用 `ANTHROPIC_API_KEY` 保存凭据，并把下面的文件路径和模型 ID 换成自己的值。配置使用 `ANTHROPIC_API_KEY`，以免读取日常使用的 `API_KEY`。

```powershell
$envFile = 'C:\path\to\anthropic.env'
$model = '<可用的 Anthropic 模型 ID>'
$probe = Join-Path ([IO.Path]::GetTempPath()) ('nano-anthropic-' + [guid]::NewGuid())
$workspace = Join-Path $probe 'workspace'
$config = Join-Path $probe 'config'
New-Item -ItemType Directory -Path $workspace, $config | Out-Null
git init --quiet $workspace
'NANO-ANTHROPIC-PROBE' | Set-Content -Encoding utf8 (Join-Path $workspace 'marker.txt')
'{"api_key_env":"ANTHROPIC_API_KEY"}' | Set-Content -Encoding utf8 (Join-Path $config 'settings.json')
$env:NANO_CODE_HOME = $config
$env:STREAMING_ENABLED = 'true'
$env:MAX_MODEL_REQUESTS = '5'
$env:MAX_RETRIES = '1'
$events = Join-Path $probe 'events.jsonl'
nano-code --cwd $workspace --env-file $envFile --provider anthropic `
  --base-url 'https://api.anthropic.com' --model $model --permission-mode plan `
  -p 'Use read_file to read marker.txt and return its exact contents.' --json |
  Tee-Object -FilePath $events
```

确认 JSON 事件包含成功的 `read_file`、流式 `text_delta`、包含标记的 `reply`、服务报告的 `usage`，以及状态为 `completed` 的 `turn_end`。记录候选提交、模型 ID、服务地址、请求次数、用量及输出位置。API 事件说明见 [Anthropic 流式响应文档](https://platform.claude.com/docs/en/build-with-claude/streaming)。

## T010 · 外部开发者试用

在 Windows 的新目录安装候选提交 `0840f82`，然后配置试用者自己的模型。其他系统的安装方式见 [终端使用指南](../../docs/terminal.md)。

```powershell
git clone --branch codex/verify-0.6.0 --single-branch https://github.com/maladingda-xxx/nano-code.git nano-code-preview
Set-Location nano-code-preview
git rev-parse --short HEAD
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .\coding_agent
.\.venv\Scripts\nano-code.exe --version
```

使用 `nano-code --cwd <目标项目路径>` 选择一个可以试改的项目副本，尝试完成一项实际任务，例如修复一个已复现的失败测试。使用默认权限模式审阅编辑和测试命令，最后用 `/diff` 查看结果。Windows 上可用上面安装目录里的 `.\.venv\Scripts\nano-code.exe` 启动。

如果目标项目使用 Python，在目标项目的 `.venv` 中安装测试依赖，并先确认测试命令能运行。若目标项目没有 `.venv`，Nano Code 会用启动它的 Python 来执行 `python` 命令。

试用反馈填写以下内容即可。分享前删去凭据和私有代码片段。

- 候选提交、系统和 Python 版本、模型服务和模型 ID。
- 任务描述、预期结果、是否完成、实际验证命令及结果。
- 审批次数，有无重复或含义不明确的提示，以及等待最久的环节。
- 若使用了取消、恢复或撤销，记录操作、终端提示与实际文件状态。
