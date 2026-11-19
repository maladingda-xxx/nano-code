# Terminal contract

| Command or step | Behavior |
|---|---|
| `/login` | Select API Key / Token or browser account login. Search eligible providers. Enter the secret in a masked prompt or approve on the official website. Escape and Ctrl+C cancel. |
| `/model` | Open the model picker. Display provider names and the current choice. Press `/` to search, arrows to navigate and Enter to apply. |
| `/model provider:model-id` | Switch to a configured provider. Split on the first colon so model variants may include colons. |
| `/model legacy:model-id` | Return to the configured legacy endpoint. |
| `/model model-id` | Change the model at the currently selected endpoint. |

Login keeps the active provider. It updates an active connection if the same provider is reconfigured. The next conversation request uses the connection chosen with `/model`.
