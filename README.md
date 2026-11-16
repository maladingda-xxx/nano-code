# Nano Code

Nano Code 0.6 是一个本地终端编程助手。它使用你配置的模型，在项目中查找代码、编辑文件、运行获授权的验证命令，并保留可以恢复和撤销的会话记录。

```powershell
py -3.11 -m pip install -e .\coding_agent
nano-code setup
nano-code doctor
nano-code
```

已有 `coding_agent/.env` 的用户可直接启动。首次配置时用环境变量或 `--env-file` 提供 API Key。Linux 和 macOS 可用 `python3 -m pip install -e ./coding_agent` 安装。

[快速上手与版本差异](docs/terminal.md) · [执行内核与旧入口](coding_agent/README.md) · [评测记录](evals/README.md)

一个轻量的 Python 终端编程助手。它可以在仓库里搜索代码、局部编辑、运行获授权的测试命令并展示改动，支持会话恢复、文件修改撤销和 JSON 脚本模式。

[安装与使用](coding_agent/README.md)
