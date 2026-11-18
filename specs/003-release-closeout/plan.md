# 实施计划 · 发布收尾与路线图同步

- **工作分支** `003-release-closeout`（当前仍在 `main`）
- **规格** [spec.md](spec.md)
- **日期** 2026-11-18
- **候选提交** `65ede79`

## 摘要

在不修改产品行为的前提下，为 0.6.0 候选提交补齐可复核的发布证据：实际运行既有测试并记录环境，核对当前 HEAD 的 CI 状态，记录 T009、T010 的阻塞条件和 I06 已知限制，最后按证据同步 `plan.md` 路线图。所有结论必须能追溯到命令、提交或已有验收记录。

## 技术背景

- **语言/版本**：Python 3.11.4（测试环境）；仓库要求 3.11+
- **主要依赖**：pytest 9.2.0（仅用于验证）；GitHub Actions 用于多平台 CI
- **存储**：Markdown 文档；测试结果不写入代码仓库的敏感位置
- **测试**：`coding_agent/tests/` 既有 82 项回归
- **目标平台**：Windows 本机验证；CI 覆盖 Windows、Linux、macOS
- **项目类型**：终端 CLI 的发布验收
- **性能目标**：无新增性能要求
- **约束**：不发起新的付费模型请求；不把旧提交的 CI 结果当作当前 HEAD 的结论
- **范围**：验收记录、路线图和发布状态文档；不修改 `coding_agent/` 运行时行为

## 项目原则检查

`.specify/memory/constitution.md` 仍是未填充模板，按 speckit 规则跳过原则门禁，并在计划中记录这一事实。实施仍遵守仓库既有约定：

- 保留 CLI、SDK、RPC 的共享执行和会话格式，不引入迁移
- 只做最小、可审阅的文档改动
- 结论必须附实际运行证据，不能以文档声明代替验证
- 不写入密钥、令牌或真实用户数据

## 项目结构

### 本文档目录

```text
specs/003-release-closeout/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── checklists/requirements.md
└── tasks.md
```

### 实际改动位置

```text
plan.md                                   # 路线图状态同步
specs/001-preview-release-validation/
├── results.md                            # 当前 HEAD 测试与 CI 状态、I06 已知限制
└── tasks.md                              # T009/T010 阻塞状态与解除条件
specs/003-release-closeout/               # 本特性的规格、计划与验证记录
```

`docs/terminal.md` 只在发现与当前行为矛盾时修改；本轮预计不需要改产品文档。

## 执行顺序

1. 运行既有测试，记录提交、解释器、命令和通过数。
2. 尝试查询当前 HEAD 的 CI；失败时记录 API/凭据错误，保持“未验证”。
3. 更新 `specs/001` 的验收记录：T009、T010 阻塞原因和解除条件，I06 已知限制。
4. 按证据更新 `plan.md` 阶段二至阶段四勾选状态，保留历史段落。
5. 交叉核对 `plan.md`、`docs/terminal.md`、`specs/001`、`specs/002` 的状态描述。
6. 重跑文档相关检查（如有）并审阅 diff。

## 完成条件

- 当前 HEAD 的测试结果有实际命令和通过数
- CI 状态明确标为通过、失败或未验证，并给出复核方式
- 每个未完成验收项都有状态、阻塞原因和解除条件
- `plan.md` 新勾选项均有证据位置，零无证据勾选
