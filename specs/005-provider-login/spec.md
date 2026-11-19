# Feature Specification — Provider login and model switching

- **Feature Branch** `codex/005-provider-login`
- **Created** 2026-11-19
- **Status** Implemented

## User Scenarios & Testing

### User Story 1 — Configure a provider with a key (P1)

A terminal user enters `/login`, chooses API Key / Token, searches for a provider, and enters the credential in a masked field.

**Independent test** — Configure two providers and restart. Both remain configured and each keeps its own credential.

1. Given a supported provider and a valid key, completing login makes that provider's models available in `/model`.
2. Given an existing valid login, a cancelled or rejected replacement leaves that login usable.
3. Given a provider that requires deployment details, the terminal prompts for them before saving.

### User Story 2 — Sign in through a browser (P2)

A terminal user chooses browser account login and a supported provider, approves access on the provider's own page, and returns to the CLI.

**Independent test** — Complete OpenRouter authorization and find OpenRouter models in `/model`.

1. Approving authorization opens the account's models for selection without manually copying an API key.
2. Cancelling, timing out, or receiving an invalid response leaves previous configuration usable.

### User Story 3 — Switch models across providers (P1)

A user with configured providers opens `/model`, sees which provider owns each model, and selects one for the current session.

**Independent test** — Switch between two providers and check that each sends the next request to its own service with its own credential.

1. Model names shared across providers remain distinct entries. Users can also enter a provider-qualified model ID.
2. Existing DeepSeek and custom configurations remain available, including unqualified model IDs and supported reasoning settings.
3. Login and selection leave the current conversation history in place.

### Edge Cases

- An empty credential, a cancelled selector, or a failed verification leaves the old login intact.
- An unavailable model catalog falls back to provider defaults or a deployment name. The user can enter a model ID directly.
- Login and model switching are unavailable while a turn or queued work is active.
- Closing the browser without approving is recoverable by cancelling the terminal wait.

## Requirements

### Functional Requirements

- **FR-001** — `/login` MUST select the authentication method first, then a searchable list of eligible providers with configuration status.
- **FR-002** — API Key / Token MUST include DeepSeek, OpenAI, Claude, Gemini, OpenRouter, Qwen, Kimi, GLM, MiniMax, Grok, Groq, Mistral and Azure OpenAI.
- **FR-003** — Browser account login MUST open the official OpenRouter authorization page and persist a credential only after successful authorization.
- **FR-004** — Credentials MUST be masked at entry, scoped to the selected provider and kept in user-local storage outside normal workspace configuration, prompt history and diagnostics.
- **FR-005** — `/model` MUST show configured provider models, support searching by provider and model, and route the next request through the selection.
- **FR-006** — Legacy model configuration and DeepSeek reasoning controls MUST remain usable. Changing providers MUST preserve conversation history and must not alter an active turn.
- **FR-007** — Direct Kimi K3 and K3.1 Pro, including Highspeed, MUST allow selecting low, high or max in the existing reasoning controls. Auto MUST use the service default.
- **FR-008** — Newly available models MUST inherit documented family-level reasoning options or use their published model capabilities. The picker, command completion and requests MUST use the same available levels. Selecting a model without the current level MUST reset to auto.

### Key Entities

- **Provider** — Service identity, supported login modes and model choices.
- **Login** — A user-local connection to one provider, with credentials and optional deployment details.
- **Model selection** — A provider and model pair active for the current session.

## Success Criteria

- **SC-001** — A clean installation offers at least 13 direct provider options and one browser account option.
- **SC-002** — Switching between two configured providers sends each request with only the matching endpoint and credential.
- **SC-003** — Saved logins are visible on restart. Cancellation, rejection and timeout leave prior logins intact.
- **SC-004** — Automated regression checks cover both login paths, provider isolation and the existing DeepSeek flow.

## Assumptions

- OpenRouter is the first provider supported for browser sign-in.
- Credentials persist across restarts. Model selection follows the existing session-only behavior.
- Provider entitlements and model availability depend on the user's account and region. Azure OpenAI uses a v1 resource endpoint and deployment name.
