# Research — 现有架构与设计决策

## 会话与模型

`run_turn` 维护原始消息并在工具请求、结果、回复与轮终点写执行检查点。现代入口通过 `Session` 共享模型、权限、任务清单、事件和 SQLite。旧入口、权限审查与子代理可以直接调用模型适配层。OpenAI 兼容及 Anthropic 适配器已有流式、请求失败重试、推理数据回放及输入输出用量。

旧的硬预算回退逐批删减消息，但请求中通常携带完整长输出。既有手动压缩需要用额外会话存档备份，然后替换活动历史。每次系统提示完整加载项目记忆。这些行为使长任务容易重复处理日志，也使连续压缩的来源难以查找。

## 决策

- 保留消息、原始执行上限、模型适配、任务清单和权限实现。将摘要作为可校验的旁表提交，避免创建重复历史或改写现有 UID。
- 在工具控制流之外形成输出视图，保留消息 ID 与各供应商依赖的签名。高压跳过冗余文本，硬预算仍由模型适配器保证。
- 使用固定九字段 JSON 摘要、最近的精确输入和实际工具证据。摘要调用不发送工具定义，限制输入、输出和自动尝试次数。
- 模型目录或明确配置提供窗口与输出能力。未知模型保留 `context_limit` 回退。所有请求前结果都标为估算，与实际 usage 单独显示。
- Chat Completions 与 DeepSeek 借助确定的提示前缀和现有自动缓存。Anthropic 官方直连可显式选择缓存固定前缀。兼容代理不添加未知缓存参数。Anthropic 实际输入为未缓存、缓存读、缓存写三者之和。 [OpenAI](https://developers.openai.com/api/docs/guides/prompt-caching) · [DeepSeek](https://api-docs.deepseek.com/guides/kv_cache/) · [Anthropic](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) · [OpenRouter](https://openrouter.ai/docs/guides/best-practices/prompt-caching)
