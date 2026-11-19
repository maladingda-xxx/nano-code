# Implementation Plan — Provider login and model switching

**Branch** `codex/005-provider-login` | **Date** 2026-11-19 | [Specification](spec.md)

## Summary

Add an inline login wizard, a user-local provider registry and login store, and provider-qualified choices in the existing model picker. Reuse both runtime adapters and add request options where provider protocols differ.

## Technical Context

- Python 3.11+, prompt_toolkit, httpx, OpenAI Python SDK and pytest.
- User-local per-provider records and the existing layered configuration.
- Windows, macOS and Linux interactive terminals. Headless entry points continue to use layered configuration.
- OAuth via OpenRouter S256 PKCE and a temporary loopback callback.

## Constitution Check

The repository constitution contains template placeholders and defines no project-specific gates. The design uses the existing CLI, configuration and runtime test conventions.

## Project Structure

```text
coding_agent/
├── {agent_core,model_api,session_store}.py
├── nano_code/{config,provider_profiles,provider_login,login_picker,providers,runtime,model_picker,ui,commands}.py
└── tests/test_provider_login.py
specs/005-provider-login/{spec,plan,research,data-model,quickstart,tasks}.md
```

## Design and Validation

Provider configuration comes from the registry. Azure endpoints are checked against the supported Azure resource domains and v1 path. Credential replacement is atomic for that provider. Runtime model selection takes a snapshot of the matching connection and replaces any open client when switching connections. The legacy slot retains layered overrides and its own model list.

Kimi and MiniMax use tagged reasoning history. Gemini retains its tool-call signatures in the corresponding assistant message. Claude and OpenRouter replay their own reasoning blocks during tool follow-ups. Other connections receive the portable conversation data when the user switches.

Use the provider reasoning rules and published catalog capabilities for both UI availability and outbound requests. When opening the model picker, refresh eligible catalogs within a two-second timeout and use the last available data on failure. Keep refreshes session-local so they cannot overwrite a separately renewed credential.

Test the prompt sequences with terminal input and mock provider responses. Cover the loopback protocol, credential scopes, tool-call continuation and backward compatibility. See the [contract](contracts/terminal.md), [data model](data-model.md) and [quickstart](quickstart.md).
