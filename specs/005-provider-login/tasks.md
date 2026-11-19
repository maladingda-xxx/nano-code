# Tasks — Provider login and model switching

## Phase 1 — Setup

- [x] T001 Verify Python dependencies and credential ignore rules in `coding_agent/pyproject.toml` and `.gitignore`.

## Phase 2 — Foundation

- [x] T002 Define the provider registry, endpoints, transport defaults and atomic records in `coding_agent/nano_code/provider_profiles.py`. Credentials are nonempty visible ASCII and model IDs contain no whitespace or control characters.

## Phase 3 — User Story 1

- [x] T003 [US1] Implement catalog discovery and credential rejection handling in `coding_agent/nano_code/provider_login.py`.
- [x] T004 [US1] Add mode selection, searchable providers, masked input and Azure deployment fields in `coding_agent/nano_code/login_picker.py`.

## Phase 4 — User Story 2

- [x] T005 [US2] Implement OpenRouter PKCE and callback handling in `coding_agent/nano_code/provider_login.py`. The callback must match the expected path and state.
- [x] T006 [US2] Connect the browser authorization wait and cancellation to `coding_agent/nano_code/login_picker.py`.

## Phase 5 — User Story 3

- [x] T007 [US3] Resolve and switch qualified model selections in `coding_agent/nano_code/config.py` and `coding_agent/nano_code/runtime.py`.
- [x] T008 [US3] Handle provider-specific request options and tool-call continuation in `coding_agent/nano_code/providers.py`, `coding_agent/model_api.py`, `coding_agent/agent_core.py` and `coding_agent/session_store.py`.
- [x] T009 [US3] Wire login and provider-aware selection into `coding_agent/nano_code/ui.py`, `coding_agent/nano_code/model_picker.py` and `coding_agent/nano_code/commands.py`.

## Phase 6 — Verification

- [x] T010 Exercise login UI, OAuth, credential isolation and existing behavior in `coding_agent/tests/test_provider_login.py` and the full test suite.
- [x] T011 Document login, coverage and examples in `README.md`, `docs/terminal.md` and `coding_agent/README.md`.
- [x] T012 Expose supported Kimi reasoning levels and forward the selected value in `coding_agent/nano_code/providers.py` and `coding_agent/model_api.py`. Verify model picker choices, request payload and auto mode in `coding_agent/tests/test_provider_login.py`.
- [x] T013 Resolve effort from provider catalogs and model families in `coding_agent/nano_code/reasoning.py`, refresh catalog metadata in `coding_agent/nano_code/runtime.py`, and send the corresponding protocol options. Verify new models and tool continuation in `coding_agent/tests/test_provider_login.py`.

US1 and US2 share the registry. US3 uses connections produced by either login method. See [quickstart](quickstart.md) for independent acceptance checks.

The Windows test suite passed all 114 tests with console colors enabled. Upstream API and authorization responses were simulated. The browser tests used a real local callback endpoint.
