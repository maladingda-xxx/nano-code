# Quickstart — Verify provider login

1. Install the project in an isolated Python environment with `python -m pip install -e "./coding_agent[dev]"`.
2. Run `python -m pytest coding_agent/tests` from the repository root.
3. Start `nano-code`. Enter `/login`, choose API Key / Token and configure a provider. Verify that pasting does not display the credential. Enter `/model`, select a model and send a short prompt.
4. Enter `/login` again, select browser account login, choose OpenRouter and authorize in the browser. Select an OpenRouter model and send a prompt. Repeat and cancel to verify the existing login remains.
5. Restart and verify both providers appear in `/model`. Select with a qualified ID and verify the original DeepSeek configuration is available when configured.
