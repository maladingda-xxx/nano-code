# Nano Code

Nano Code 0.6 是一个本地终端编程助手。它使用你配置的模型，在项目中查找代码、编辑文件、运行获授权的验证命令，并保留可以恢复和撤销的会话记录。

```powershell
python -m pip install -e .\coding_agent
python -m nano_code --version
nano-code doctor
nano
```

`nano` 与 `nano-code` 是同一个入口。如果系统中已有同名文本编辑器（例如 GNU nano），请使用 `nano-code`。首次启动后输入 `/login`，选择登录方式和供应商。登录完成后输入 `/model`，选好模型即可开始对话。浏览器账号登录支持 OpenRouter，其他供应商可使用 API Key 或 Token。

已有 `coding_agent/.env` 的用户可直接启动。脚本模式可继续使用 `nano-code setup`、环境变量或 `--env-file`。使用 Conda 时请先激活目标环境再安装。Linux 和 macOS 可用 `python3 -m pip install -e ./coding_agent` 安装。

[快速上手与版本差异](docs/terminal.md) · [版本记录](CHANGELOG.md) · [执行内核与旧入口](coding_agent/README.md) · [评测记录](evals/README.md)

一个轻量的 Python 终端编程助手。它可以在仓库里搜索代码、局部编辑、运行获授权的测试命令并展示改动，支持会话恢复、文件修改撤销和 JSON 脚本模式。

[安装与使用](coding_agent/README.md)
