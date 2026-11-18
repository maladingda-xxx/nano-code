# 实施计划 · 发布诊断与版本记录

- **工作分支** `feat/release-diagnostics`（堆叠在 `docs/release-closeout` 上）
- **规格** [spec.md](spec.md)
- **日期** 2026-11-18
- **依赖** [003 发布收尾](../003-release-closeout/spec.md)

## 摘要

在现有 `nano-code doctor` 之上增加可选导出：把诊断记录写入用户指定路径，并在写入前递归遮蔽敏感字段和常见令牌。补充会话数据格式版本常量、`CHANGELOG.md` 和终端文档中的存储/清理说明。实现不连接模型服务，不引入迁移，不改变现有 doctor 字段语义。

## 技术背景

- **语言/版本**：Python 3.11+
- **主要依赖**：现有标准库 `json`、`re`、`platform`、`pathlib`；测试使用 pytest
- **存储**：读取当前配置和会话数据库路径；导出为本地 JSON 文件
- **测试**：`coding_agent/tests/` 现有 82 项 + 新增诊断回归
- **目标平台**：Windows、Linux、macOS
- **项目类型**：终端 CLI
- **性能目标**：导出为本地文件操作，1 秒内完成
- **约束**：不发起网络请求；不出现凭据值；不改变 `nano-code doctor` 现有 JSON 字段和退出码
- **范围**：`commands.py`、`cli.py`、`session_store.py`、`tests/`、`docs/terminal.md`、`CHANGELOG.md`

## 项目原则检查

`.specify/memory/constitution.md` 仍是未填充模板，按 speckit 规则跳过原则门禁。实施遵守：

- 最小改动，复用现有 `diagnostics()` 和 CLI 结构
- 导出内容默认脱敏，测试必须验证密钥不落盘
- 不改变已有命令行为和会话格式
- 文档说明备份、删除影响和清理步骤

## 项目结构

```text
coding_agent/
├── nano_code/cli.py          # 增加 --export 参数和 doctor 分支
├── nano_code/commands.py     # redact()、export_diagnostics()、诊断字段
├── session_store.py          # SESSION_FORMAT_VERSION
└── tests/test_release.py     # 导出、脱敏、路径错误、缺失 Key

docs/terminal.md              # 存储位置、清理、导出用法
CHANGELOG.md                  # 版本记录和数据格式版本
plan.md                       # 勾选阶段四对应条目
specs/004-release-diagnostics/
```

## 执行顺序

1. 在 `session_store.py` 定义 `SESSION_FORMAT_VERSION = 1`。
2. 在 `commands.py` 增加脱敏、导出函数和诊断字段。
3. 在 `cli.py` 增加 `--export`，仅允许与 `doctor` 同用。
4. 增加回归测试并运行完整测试集。
5. 更新 `docs/terminal.md`、`CHANGELOG.md`、`README.md` 和 `plan.md`。
6. 用真实 CLI 导出一次，人工核对文件内容和脱敏结果。

## 完成条件

- `nano-code doctor --export <路径>` 生成有效 JSON，返回 `0`
- 已配置的 API Key 不出现在导出文件中
- 路径错误返回非零和可读错误
- 现有 82 项测试和新增测试全部通过
- 文档覆盖设置、会话数据库、文件快照、备份和清理
