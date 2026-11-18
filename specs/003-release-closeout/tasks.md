# 任务清单 · 发布收尾与路线图同步

**输入**：[spec.md](spec.md) · [plan.md](plan.md) · [research.md](research.md) · [data-model.md](data-model.md) · [quickstart.md](quickstart.md)

**说明**：本特性只改文档和验收记录，不修改运行时行为。任务按用户故事组织，每条结论必须能追溯到实际命令或已有验收记录。

## Phase 1: Setup

- [X] T001 确认候选状态：在仓库根目录运行 `git status --short`、`git rev-parse HEAD`，确认工作区干净且 HEAD 为 `65ede79`，把版本、提交和日期记录到 `specs/003-release-closeout/quickstart.md`

---

## Phase 2: Foundational

- [X] T002 确认证据落点：核对 `specs/001-preview-release-validation/results.md`、`tasks.md` 和 `plan.md` 当前内容，确定测试证据、CI 状态、阻塞项和路线图分别写入的位置

**Checkpoint**：记录位置确定后，三个用户故事可以独立推进

---

## Phase 3: User Story 1 - 确认当前候选提交可发布 (Priority: P1) 🎯 MVP

**目标**：为 HEAD `65ede79` 提供可复核的测试和 CI 证据

**独立验收**：测试命令有实际运行记录；CI 结论有链接或明确的“未验证”原因

- [X] T003 [US1] 运行 `.\.cache\verify-venv\Scripts\python.exe -m pytest coding_agent/tests -q`，把解释器版本、命令、通过数（预期 82）和日期写入 `specs/003-release-closeout/quickstart.md` 的“本轮验证记录”
- [X] T004 [US1] 尝试用 `gh run list --commit 65ede79` 和 GitHub API 查询 CI；首次因无效 `GITHUB_TOKEN` 标为 `unverified`，完成 `gh auth login` 后复核，确认六组通过（运行 `44259016156`）
- [X] T005 [US1] 在 `specs/001-preview-release-validation/results.md` 增加“当前候选提交复核”段落，分开记录 `0840f82` 的历史 CI 结果和 `65ede79` 的当前状态；当前状态为测试 82 项通过、CI 六组通过

**Checkpoint**：US1 完成，当前 HEAD 的测试和 CI 状态均有记录，不再沿用旧提交结论

---

## Phase 4: User Story 2 - 交付记录如实反映完成度 (Priority: P1)

**目标**：T009、T010 和 I06 的状态、阻塞原因和解除条件清晰可查

**独立验收**：每个未完成项都有状态、原因和解除条件，且固定判定与真实兼容性结果分开记录

- [X] T006 [P] [US2] 更新 `specs/001-preview-release-validation/tasks.md`：T009 写明仅完成本地协议演练，解除条件为可用 Anthropic Key 和模型 ID；T010 写明试用步骤已准备但缺外部参与者
- [X] T007 [P] [US2] 更新 `specs/001-preview-release-validation/results.md`：把 I06 兼容性复测失败单独列为已知限制，写明失败表现、影响范围、检查文件 `evals/checks/url_safe_roundtrip.py`，并注明不是运行时缺陷
- [X] T008 [US2] 交叉核对 `specs/001` 和 `plan.md`，确认没有把 T009、T010、I06 写成已通过；如有矛盾立即修正

**Checkpoint**：US2 完成，未完成项不会被误读为已交付

---

## Phase 5: User Story 3 - 路线图与验收记录保持一致 (Priority: P2)

**目标**：`plan.md` 阶段二至阶段四按证据更新，未验证项保持未完成

**独立验收**：逐项核对新勾选项都有代码、测试或验收记录支撑

- [X] T009 [US3] 更新 `plan.md` 阶段二勾选状态：多行输入/历史/补全、状态展示、diff 与输出审阅、取消与排队、会话 UID/重命名/fork、检查点、按目录加载约定、命令授权均按现有测试和文档勾选；并行独立文件修改注明需 worktree
- [X] T010 [US3] 更新 `plan.md` 阶段三勾选状态：Skills、MCP、SDK、RPC、事件协议版本和错误处理按证据勾选；工具注册公开契约和第二模型真实验证保持未勾选并注明
- [X] T011 [US3] 更新 `plan.md` 阶段四勾选状态：命名空间、CI、setup/doctor、文档按证据勾选；版本记录/数据格式说明、诊断导出和外部试用保持未勾选并注明
- [X] T012 [US3] 在 `plan.md` 顶部补充当前状态说明，指向 `specs/003-release-closeout/`，并声明 `specs/` 是当前完成度的事实来源

**Checkpoint**：US3 完成，路线图与 specs 不再矛盾

---

## Phase 6: Polish

- [X] T013 运行 `git diff --stat` 和 `git diff`，确认只改文档、没有密钥/令牌/真实用户数据、没有无关代码改动
- [X] T014 按 `quickstart.md` 逐项复核，并把最终结论写入“本轮验证记录”

---

## 依赖与执行顺序

- Phase 1 → Phase 2 → 用户故事
- US1 的 T003 必须先于 T004/T005，因为后续记录要引用测试结果
- US2 的 T006 和 T007 修改不同文件，可并行；T008 依赖两者
- US3 的 T009–T011 修改同一文件，按顺序执行；T012 最后
- Polish 依赖所有故事完成

## 并行示例

```text
# US2 并行：
T006 更新 specs/001-preview-release-validation/tasks.md
T007 更新 specs/001-preview-release-validation/results.md
```

## 实施策略

先完成 US1 作为 MVP（测试和 CI 证据），再做 US2（阻塞与已知限制），最后做 US3（路线图同步）；Polish 统一审阅 diff。
