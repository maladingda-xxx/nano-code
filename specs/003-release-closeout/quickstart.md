# 验证步骤 · 发布收尾

## 前置条件

- 工作区在候选提交 `65ede79`，`git status --short` 为空
- 已存在 `.cache/verify-venv`（Python 3.11.4，pytest 9.2.0）

## 1. 运行既有测试

```powershell
cd D:\Projects\nano-code\nano-code
.\.cache\verify-venv\Scripts\python.exe -m pytest coding_agent/tests -q
```

预期：`82 passed`。记录解释器版本、命令、运行日期和提交哈希。

## 2. 核对当前 HEAD 的 CI

需要有效 GitHub 凭据：

```powershell
gh auth login
gh run list --commit 65ede79 --limit 5
```

或在浏览器打开仓库的 Actions 页面，筛选提交 `65ede79`。

预期：

- 有运行记录且结论为成功 → 在 `specs/001-preview-release-validation/results.md` 记录链接和结论
- 无凭据或无法查询 → 记录 `unverified` 和具体错误，不得沿用 `0840f82` 的通过结论

## 3. 核对 T009 / T010 的阻塞状态

在 `specs/001-preview-release-validation/tasks.md` 检查：

- T009 保持未勾选，写明缺少真实 Anthropic 凭据和模型 ID
- T010 保持未勾选，写明缺少外部试用者
- 两项都有解除条件和责任人

## 4. 核对 I06 已知限制

在 `specs/001-preview-release-validation/results.md` 检查：

- 固定任务判定与兼容性复测结果分开记录
- 兼容性检查位于 `evals/checks/url_safe_roundtrip.py`
- 已说明该问题属于模型候选，不是运行时缺陷

## 5. 核对路线图一致性

逐项检查 `plan.md`：

- 新勾选项能指向代码、测试或验收记录
- 未验证项没有勾选
- 阶段二至阶段四的当前状态与 `specs/001`、`specs/002` 不矛盾

可用以下命令辅助搜索：

```powershell
Select-String -Path plan.md -Pattern '^\s*- \[[ xX]\]'
```

## 6. 审阅改动

```powershell
git diff --stat
git diff
```

确认只改文档，没有密钥、令牌或真实用户数据。

## 本轮验证记录

- **2026-11-18** 候选 HEAD `65ede790d3bc4b45cadde40089118fa6ee359dfd`，版本 `0.6.0`。运行前工作区无未提交代码改动，实施后仅新增/修改文档。
- **测试** `.\.cache\verify-venv\Scripts\python.exe -m pytest coding_agent/tests -q` → `82 passed in 11.35s`。解释器 Python 3.11.4，pytest 9.2.0。
- **CI** 通过。完成 `gh auth login` 后查询提交 `65ede79` 的运行 [44259016156](https://github.com/maladingda-xxx/nano-code/actions/runs/44259016156)，1 分 33 秒完成；Windows、Linux、macOS × Python 3.11/3.13 六组 job 全部 `success`。首次查询因无效 `GITHUB_TOKEN` 返回 401/403，登录后解决。
- **T009 / T010** 已在 `specs/001-preview-release-validation/tasks.md` 记录为阻塞，附原因、责任人和解除条件。
- **I06** 已在 `specs/001-preview-release-validation/results.md` 记录为已知限制，固定判定与兼容性结果分开。
- **路线图** `plan.md` 阶段二至阶段四已按证据更新；无证据项保持未勾选并注明原因。
- **未运行** 未发起新的付费模型请求。GitHub CI 已完成复核。
