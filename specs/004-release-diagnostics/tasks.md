# 任务清单 · 发布诊断与版本记录

**输入**：[spec.md](spec.md) · [plan.md](plan.md) · [research.md](research.md) · [data-model.md](data-model.md) · [quickstart.md](quickstart.md)

**说明**：本特性包含少量运行时改动（诊断字段、导出、脱敏）和文档更新。测试覆盖导出成功、路径错误、缺失 Key 和脱敏。

## Phase 1: Setup

- [X] T001 在 `coding_agent/session_store.py` 定义 `SESSION_FORMAT_VERSION = 1`，并加注释说明它表示当前会话数据库和文件快照格式

---

## Phase 2: Foundational

- [X] T002 在 `coding_agent/nano_code/commands.py` 为 `diagnostics()` 增加 `platform`、`config_home`、`session_db`、`session_format` 字段，保持现有字段不变

**Checkpoint**：诊断字段可用后，导出和文档可以并行推进

---

## Phase 3: User Story 1 - 支持者导出可安全分享的诊断记录 (Priority: P1) 🎯 MVP

**目标**：`nano-code doctor --export <路径>` 生成脱敏 JSON

**独立验收**：配置 API Key 后导出，文件包含版本和检查结果，不包含 Key 值；路径错误返回非零

### Tests for User Story 1

- [X] T003 [US1] 在 `coding_agent/tests/test_release.py` 增加测试：导出成功且无密钥、导出路径是目录时报错、`redact()` 遮蔽敏感字段和令牌、清空配置后 `api_key_configured` 为 `False`

### Implementation for User Story 1

- [X] T004 [US1] 在 `coding_agent/nano_code/commands.py` 实现 `redact()` 和 `export_diagnostics()`；脱敏字段名和常见令牌，布尔值保留
- [X] T005 [US1] 在 `coding_agent/nano_code/cli.py` 增加 `--export` 参数和 doctor 分支处理；`--export` 只能与 `doctor` 同用，写文件后输出路径
- [X] T006 [US1] 运行 `.\.cache\verify-venv\Scripts\python.exe -m pytest coding_agent/tests -q`，确认新增测试和原有 82 项通过

**Checkpoint**：US1 完成，导出与脱敏可独立验证

---

## Phase 4: User Story 2 - 使用者能查到版本、数据格式和清理方式 (Priority: P2)

**目标**：版本记录、存储位置和清理方式可查

**独立验收**：从 CHANGELOG 和终端文档能找到 0.6.0、数据格式版本、备份和删除影响

- [X] T007 [P] [US2] 新增 `CHANGELOG.md`，记录 0.6.0、0.3.1、0.3.0、0.2.0 的主要变更、已知限制和数据格式版本 `1`
- [X] T008 [P] [US2] 更新 `docs/terminal.md`：增加 `nano-code doctor --export` 用法、用户配置/项目设置/会话数据库/文件快照位置，以及备份、删除影响和清理步骤
- [X] T009 [US2] 在 `README.md` 增加 CHANGELOG 链接，并在版本记录中关联已知限制
- [X] T010 [US2] 在 `plan.md` 勾选阶段四的版本记录和诊断导出两项，注明数据和导出证据
- [X] T011 [US2] 按 `quickstart.md` 用真实 CLI 导出一次，核对文件内容、脱敏结果和路径错误

**Checkpoint**：US2 完成，用户能查到版本、格式和清理方式

---

## Phase 5: Polish

- [X] T012 运行完整测试集，审阅 `git diff --stat` 和 `git diff`，确认无密钥、无无关改动，并把结果写入 `quickstart.md` 的“本轮验证记录”

---

## 依赖与执行顺序

- T001 → T002 → T003/T004/T005 → T006
- T007 和 T008 修改不同文件，可并行；T009 依赖 T007；T010 依赖 T002 和 T004；T011 依赖 T006
- T012 依赖所有任务完成

## 并行示例

```text
# US2 并行：
T007 新增 CHANGELOG.md
T008 更新 docs/terminal.md
```

## 实施策略

先完成 US1 的导出与脱敏（MVP），再补版本记录和存储文档，最后跑完整验证。
