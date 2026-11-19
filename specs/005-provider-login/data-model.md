# Data Model — Provider login

| Entity | Fields and constraints |
|---|---|
| Provider | Unique lowercase ID, display name, HTTPS API endpoint, adapter kind, login modes, default model IDs and optional catalog endpoint. |
| Login record | Provider ID derived from the file name. Credential is nonempty and contains only visible ASCII. Model IDs are nonempty and contain no whitespace or control characters. Azure requires its v1 endpoint and at least one deployment. Optional reasoning metadata maps model IDs to provider-reported effort levels. New contents atomically replace the previous record for that provider. |
| Active model | Either the legacy configuration or a registered provider and model ID. Session-local. The selected provider credential and endpoint come from the same saved record. |
| Browser authorization | Per-attempt verifier, challenge, state and temporary loopback address. Ephemeral. The callback must match the expected path and state. |
