# 任务清单 · 0.6.0 预览版验收

## 准备

- [x] T001 在 `coding_agent/` 核对候选提交 `0840f82`，在 Windows 干净环境安装并验证 `nano` 和 `nano-code`。
- [x] T002 运行 `coding_agent/tests/` 的 69 项测试及 `evals/run.py preflight` 的 10 项检查。

## US1 · 固定任务基线

- [x] T003 [US1] 用 `evals/run.py` 运行 I01，核对源码证据、零写入及请求预算。
- [x] T004 [US1] 用 `evals/run.py` 运行其余七项，审查候选报告并独立验证，结果见 [验收记录](results.md)。

### I06 与 I08 复测

- [X] T011 [US1] 在 `evals/checks/url_safe_roundtrip.py` 加入 ItsDangerous 2.2.0 压缩载荷兼容性用例。验证原版通过、首轮候选失败，再按原预算重跑 I06 并记录独立测试结果。
- [X] T012 [US1] 按原预算用 `evals/run.py` 重跑 I08，审查候选并用原验收测试独立验证。在 [验收记录](results.md) 对照首轮结果。

## US2 · 恢复验证

- [x] T005 [US2] 用 `evals/recovery.py` 执行 N09 与 N10，核对报告和文件标记。
- [x] T006 [US2] 在 `nano-code` 主入口的交互终端复核 N09 和 N10。N09 由脚本在写入后触发强制退出，重启后在终端输入恢复命令。

## US3 · 平台、模型与试用

- [x] T007 [US3] 用 `nano-code` 主入口在固定目标仓库完成 I01 只读冒烟验证。
- [X] T008 [US3] 核对 `.github/workflows/ci.yml` 在候选提交 `0840f82` 上的三平台验证结果。Windows、Linux、macOS 的 Python 3.11 和 3.13 共六组均通过。[GitHub Actions](https://github.com/maladingda-xxx/nano-code/actions/runs/44241914861) 2026-11-18 以相同矩阵复核当前 HEAD `65ede79`，六组全部通过（[运行 44259016156](https://github.com/maladingda-xxx/nano-code/actions/runs/44259016156)）。
- [ ] T009 [US3] **阻塞未验证**：验证 `coding_agent/nano_code/providers.py` 支持的第二种模型服务，记录文本、工具调用、流式输出与用量。本地协议演练已通过；真实服务缺少可用 Anthropic API Key 和模型 ID。[执行方法](quickstart.md#t009--anthropic-真实服务验证)
- [ ] T010 [US3] **阻塞未验证**：按 [产品开发计划](../../plan.md) 完成外部用户试用并记录问题与验证范围。内部演练和反馈表已就绪；尚无外部试用者完成安装、配置和真实任务。[试用步骤](quickstart.md#t010--外部开发者试用)

T003 先于 T004。T011 和 T012 在首轮基线完成后依序执行。恢复测试和主入口测试沿用固定目标副本。记录的每项结论对应实际运行的提交和入口。

## 当前阻塞项

| 任务 | 状态 | 阻塞原因 | 解除条件 | 责任人 |
| --- | --- | --- | --- | --- |
| T009 | 未验证 | 环境中没有可用的 `ANTHROPIC_API_KEY` 和模型 ID；`ANTHROPIC_AUTH_TOKEN` 属于 Claude Code 会话凭据，不能作为项目模型服务凭据 | 提供 Anthropic API Key 和模型 ID，用 `anthropic` 适配器完成一次文本、工具调用、流式输出和用量记录 | 项目所有者 |
| T010 | 未验证 | 需要真实开发者在自己的仓库中独立使用，自动化无法代替 | 至少一名外部试用者按 quickstart 完成安装、配置和一个真实任务，并回收问题记录 | 项目所有者 / 外部试用者 |

两项在解除前不得在路线图中标记为完成。
