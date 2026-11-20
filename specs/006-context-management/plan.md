# Implementation Plan — Nano Code 上下文管理

**Branch** `codex/006-context-management`  
**Date** 2026-11-20

## Summary

使用会话级 Context Manager 生成请求视图。保留规范化原始消息及执行恢复，继续由供应商适配层完成最后的预算检查和协议转换。

## Technical Context

- Python 3.11+，复用 SQLite、Rich、httpx、OpenAI SDK 和可选 tiktoken。
- `run_turn` 追加原始消息、分派工具并记录结果。现代 CLI、脚本、SDK 与 RPC 共用 `Session`。旧入口继续使用低层预算保护。
- Chat Completions 兼容端点与 Anthropic Messages 分别组装请求。模型可返回流式文本与供应商特定推理签名。
- 使用离线供应商模拟和固定回归场景，再用同模型同设置的现场评测验证质量和费用。

## Constitution Check

仓库宪章为未填充的模板。使用原有 CLI、历史协议及权限边界，覆盖所有新增行为后进入下一阶段。

## Project Structure

- 原有 `coding_agent/context_budget.py` 与模型适配器负责估算、硬约束和用量。
- `coding_agent/nano_code/context.py` 管理压力、请求视图与压缩，`memory.py` 提供任务事实和项目记忆。
- `coding_agent/nano_code/context_pruning.py` 提供工具结果视图及回读引用。
- `coding_agent/nano_code/runtime.py`、`sessions.py` 和 `commands.py` 绑定生命周期、SQLite 和终端。

## Delivery

1. 基础预算、用量、工具裁剪、终端诊断。
2. 手动与自动摘要、原子持久化、失败回退。
3. 分层记忆、缓存配置、恢复兼容。
4. 测试、离线对照与现场评测流程。
