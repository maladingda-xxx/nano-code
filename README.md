# Nano Code

Nano Code 是一个本地终端编程助手。它使用你配置的模型，在项目中查找代码、编辑文件、运行获授权的验证命令，并保留可以恢复和撤销的会话记录。

## 安装发行版

当前可下载 [v0.6.0 预发行版](https://github.com/maladingda-xxx/nano-code/releases/tag/v0.6.0)。需要 Python 3.11 或更新版本。

```sh
python -m pip install "https://github.com/maladingda-xxx/nano-code/releases/download/v0.6.0/coding_agent-0.6.0-py3-none-any.whl"
python -m nano_code --version
```

在 macOS 或 Linux 上，如果系统使用 `python3`，将命令中的 `python` 替换为 `python3`。如果系统 Python 限制直接安装，请先创建并激活虚拟环境。也可以在发布页的 **Assets** 中下载 `.whl` 文件，再用 `python -m pip install <文件路径>` 安装。

## 首次使用

在你想操作的项目目录运行 `nano-code`，然后按下面的步骤开始。

1. 输入 `/login` 配置模型供应商。OpenRouter 支持浏览器账号登录，其他供应商可使用自己的 API Key 或 Token。
2. 输入 `/model`，选择已配置供应商的模型。
3. 输入任务，例如「帮我找到这个项目的测试入口」。

如果终端找不到 `nano-code`，使用安装时的 Python 运行 `python -m nano_code`。遇到配置问题时，可以运行 `nano-code doctor`。

## 从源码安装

如果要开发 Nano Code，克隆仓库后，在仓库根目录运行 `python -m pip install -e ./coding_agent`。

已有 `coding_agent/.env` 的用户可直接启动。脚本模式可使用 `nano-code setup`、环境变量或 `--env-file`。

[终端使用指南](docs/terminal.md) · [版本记录](CHANGELOG.md) · [执行内核与旧入口](coding_agent/README.md) · [评测记录](evals/README.md)
