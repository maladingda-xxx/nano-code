# 调研 · 日常终端命令

- Claude Code 的 `/model`、`/effort`、`/compact [重点]` 与 `/context` 分别操作当前模型、强度、摘要和预算。Nano Code 已有这些命令的基础版本，因此扩充共享实现。[Claude Code 命令](https://code.claude.com/docs/en/commands)
- 采用本地模型候选配置。用户自行配置的 API 端点仍可接受任意有效模型 ID，候选列表不需要联网。
- Claude Code 自动模式依赖模型分类器。Nano Code 首批使用精确命令与工作区内置编辑减少常规审批，未知操作交给已有审批。[Claude Code 权限模式](https://code.claude.com/docs/en/permission-modes)
- Anthropic 强度需要验证请求参数及工具后续轮次的思考块回传。此轮沿用已验证的 DeepSeek 能力，供应商扩展单独验证。[Anthropic Effort](https://platform.claude.com/docs/en/build-with-claude/effort)
- 请求预算和持久化已有共用接口。补齐预算分项，复用原 SQLite 会话结构写入归档。
