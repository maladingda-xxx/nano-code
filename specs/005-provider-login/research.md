# Research — Provider login

## Browser authorization

Use OpenRouter OAuth with S256 PKCE, an ephemeral IPv4 loopback callback and a per-attempt state. The code exchange returns the user's OpenRouter key. A separate GitHub Copilot integration would require an authenticated Copilot SDK session. [OpenRouter OAuth](https://openrouter.ai/docs/guides/overview/auth/oauth) · [GitHub Copilot authentication](https://docs.github.com/en/copilot/how-tos/copilot-sdk/auth/authenticate)

## Runtime compatibility

Use Anthropic Messages for Claude and Chat Completions for the other registered services. OpenAI reasoning models use `max_completion_tokens` and omit unsupported sampling options. MiniMax and applicable Kimi reasoning models need provider-specific request parameters and replay of assistant reasoning for multi-turn tool calls. [OpenAI model guide](https://developers.openai.com/api/docs/guides/latest-model) · [MiniMax Chat Completions](https://platform.minimax.io/docs/api-reference/text-openai-api) · [Kimi API documentation](https://platform.kimi.com/docs/)

Gemini's OpenAI-compatible interface requires replay of thought signatures on tool followups. Retain signatures with their respective calls and keep provider metadata out of the ordinary session export. [Google thought signatures](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/thinking/thought-signatures)

OpenRouter publishes `reasoning.supported_efforts` and Claude publishes `capabilities.effort` in their model catalogs. Use the reported levels when available and keep the original reasoning blocks for tool continuation on the same model. [OpenRouter reasoning](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens) · [Claude model capabilities](https://platform.claude.com/docs/en/api/models/list)

## Deployment and storage

Azure OpenAI v1 accepts a resource endpoint and uses the deployment name as the model. Use one replaceable credential record per provider to keep endpoint, key and catalog changes consistent, even when different providers are configured concurrently. Protect files with owner-only permissions on POSIX and the user profile's inherited access controls on Windows. [Azure OpenAI endpoints](https://learn.microsoft.com/en-us/azure/ai-services/concepts/endpoints)
