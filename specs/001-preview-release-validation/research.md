# 调研 · 0.6.0 预览版验收

## 基线可比性

11 月 9 日的固定任务由 `evals/run.py` 调用兼容入口 `agent.py`。本轮继续使用它建立可比基线，并独立补做 `nano-code` 主入口验证。保留任务的目标提交、提示、预算和补丁验收边界。

## 平台证据

`0840f82` 在本机 Windows 新安装后已验证两个入口，69 项测试分别通过于 Python 3.11 和 3.14。候选提交在 Windows、Linux、macOS 的六组远端 CI 也已通过，见[验收记录](results.md)。

## Codex 集成

使用 Spec Kit 1.4.0 的 Codex 项目 Skills。工作盘为 exFAT，Specify 的 Windows 文件句柄操作报 `WinError 87`。在 NTFS 上生成官方项目文件后复制到工作区。后续规格流程使用项目生成的 Python 脚本，已经验证模板解析和计划定位。
